#!/usr/bin/env python3
"""Run factory's behaviour evals with-skill AND baseline, offline, in disposable sandboxes.

usage: python3 factory/evals/run_evals.py <workspace> --model M --provider P
                                         [--only NAME ...] [--configs with_skill without_skill]
                                         [--runs 1] [--jobs 4] [--timeout 420]

Layout (skill-author's workspace layout, so aggregate_benchmark / generate_review work):
  <workspace>/eval-<id>-<name>/<config>/run-<n>/
      <eval-name>/       the sandbox: fixture (make_fixtures.py), HOME + workspace + fake CLIs
      outputs/final.md   the agent's final text
      outputs/tool_calls.json   every tool call with arguments (the transcript)
      transcript.md      readable transcript for the viewer
      timing.json        wall-clock seconds (tokens are not exposed by forge_client)
  <workspace>/eval-<id>-<name>/eval_metadata.json

Drives forge3 through skill-author's `scripts/forge_client.py` (the "drive the
runner directly" path from skill-author's SKILL.md). Each run's environment:
  * HOME = <sandbox>/home  -> no real gh config, kubeconfig, ~/.agents skills
  * FORGE_DATA_DIR = <sandbox>/data -> no real memories/projects/conversations,
    and projects the agent creates land in the sandbox, not the user's account
  * FORGE_MACHINE_ID removed -> automation_* refuses ("not a cloud machine")
  * PATH starts with fake gh/git/kubectl/aws/terraform/helm/flyctl
  * with_skill: factory copied to <sandbox>/home/.forge/skills/factory
    without_skill: no skill at all (home has no skills; the sandbox cwd has none)
Global skills in ~/.forge/tailcall-skills are hidden by the HOME override, so
baselines are not contaminated by them; `skills_loaded` is still recorded.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
REPO = SKILL.parent
sys.path.insert(0, str(REPO / "skill-author"))
sys.path.insert(0, str(HERE))
from scripts.forge_client import ForgeError, run_prompt  # noqa: E402
import make_fixtures as MF  # noqa: E402

SIBLINGS = ("repo-setup", "project-board", "verify")  # skills factory delegates to


def load_evals():
    return json.loads((HERE / "evals.json").read_text())["evals"]


def sandbox_for(ev, run_dir: Path, config: str) -> tuple[Path, dict]:
    # Built in place: fixtures bake absolute paths (state, KB remotes in .git/config).
    MF.SCENARIOS[ev["name"]](run_dir)
    box = run_dir / ev["name"]
    env_over = json.loads((box / "env.json").read_text())
    if config == "with_skill":
        dst = box / "home" / ".forge" / "skills"
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copytree(SKILL, dst / "factory", ignore=shutil.ignore_patterns("evals", "__pycache__"))
        for s in SIBLINGS:
            shutil.copytree(REPO / s, dst / s, ignore=shutil.ignore_patterns("evals", "__pycache__"))
    env = {k: v for k, v in os.environ.items()
           if k.startswith("FORGE_") or not any(t in k.upper() for t in
                                                ("TOKEN", "SECRET", "PASSWORD", "KEY", "KUBECONFIG", "AWS_", "GH_", "GITHUB"))}
    env.pop("FORGE_MACHINE_ID", None)
    env.update(env_over)
    # fakebin first, then the real PATH (the provider extension needs it); the
    # fakes shadow every CLI that could reach an account.
    fake = env_over["PATH"].split(os.pathsep, 1)[0]
    env["PATH"] = fake + os.pathsep + os.environ["PATH"]
    if ev["name"] in ("infra-write-credential", "infra-readonly", "infra-drift"):
        env["KUBECONFIG"] = str(box / "home" / ".kube" / "config")
    if ev["name"] == "infra-destructive":
        env["AWS_WRITE_STAGING"] = "eval-staging-writer"
        env["AWS_WRITE_PROD"] = "eval-prod-writer"
    return box, env


def db_tool_calls(box: Path):
    """Tool calls from the sandbox's own conversation store (FORGE_DATA_DIR).

    The streamed `tool_call` updates forge_client records can miss calls (seen:
    a `project_update` absent from the stream but present here), so the stored
    conversation is the transcript of record; the stream is the fallback."""
    import sqlite3
    db = box / "data" / "conversations" / "conversations.db"
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        # The first conversation is the orchestrator; later ones are Task /
        # workflow sub-agents that started before the stop killed the process.
        rows = [r[0] for r in con.execute("select data from conversations order by created_at limit 1")]
    except sqlite3.Error:
        return None
    calls = []
    for raw in rows:
        d = json.loads(raw if isinstance(raw, str) else raw.decode())
        for m in (d.get("context") or {}).get("messages", []):
            for part in (m.get("content") or {}).get("assistant", {}).get("content", []) or []:
                if isinstance(part, dict) and part.get("type") == "tool_call":
                    calls.append({"name": part.get("name"), "arguments": part.get("arguments") or {}})
    return calls


def _stop(call, calls):
    """Stop at the first fan-out: the graded decision is the task/workflow call
    itself (route, tier, repo set). Running the sub-agents would only add cost
    and time, so the eval never pays for them."""
    name = call.get("name")
    if name == "use_deferred_tool" and isinstance(call.get("arguments"), dict):
        name = call["arguments"].get("name")  # workflow is usually a deferred tool
    return name in ("task", "Task", "workflow")


def transcript_md(ev, res) -> str:
    lines = [f"# {ev['name']}", "", "## Eval Prompt", "", ev["prompt"], "", "## Tool calls", ""]
    for i, c in enumerate(res["tool_calls"], 1):
        lines.append(f"{i}. `{c['name']}` {json.dumps(c['arguments'])[:1500]}")
    lines += ["", "## Final text", "", res["text"]]
    return "\n".join(lines)


def one(ev, config, run_n, ws: Path, args):
    run_dir = ws / f"eval-{ev['id']}-{ev['name']}" / config / f"run-{run_n}"
    (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
    box, env = sandbox_for(ev, run_dir, config)
    cwd = box / MF.WS
    t0 = time.time()
    try:
        res = run_prompt(ev["prompt"] + "\n\n(Eval sandbox: work only under ~ ; everything is local.)",
                         cwd=str(cwd), model=args.model, provider=args.provider, timeout=args.timeout, env=env,
                         stop_when=_stop)
        err = None
    except ForgeError as e:
        res, err = {"text": "", "tool_calls": [], "skills_loaded": [], "timed_out": False}, str(e)
    dt = time.time() - t0
    stored = db_tool_calls(box)
    streamed = len(res["tool_calls"])
    if stored is not None:
        # stored order is authoritative; a call announced but never persisted
        # (the one the stop fired on) is appended from the stream
        res["tool_calls"] = stored + [c for c in res["tool_calls"] if c not in stored]
    (run_dir / "outputs" / "final.md").write_text(res["text"] or "")
    (run_dir / "outputs" / "tool_calls.json").write_text(json.dumps(res["tool_calls"], indent=1))
    (run_dir / "transcript.md").write_text(transcript_md(ev, res))
    (run_dir / "timing.json").write_text(json.dumps({"total_duration_seconds": round(dt, 1),
                                                     "timed_out": res.get("timed_out"), "error": err,
                                                     "tool_calls_streamed": streamed,
                                                     "tool_calls_stored": None if stored is None else len(stored)}))
    cand = "factory" if config == "with_skill" else None
    contam = [s for s in res.get("skills_loaded", []) if cand is None or s not in (cand, *SIBLINGS)]
    (run_dir / "contamination.json").write_text(json.dumps({"contaminating_skills": contam,
                                                            "skills_loaded": res.get("skills_loaded", [])}))
    print(f"{ev['name']:24} {config:13} run-{run_n} {dt:6.1f}s calls={len(res['tool_calls'])}"
          f"{' TIMEOUT' if res.get('timed_out') else ''}{' ERR ' + err if err else ''}", flush=True)
    return run_dir


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workspace")
    ap.add_argument("--model", required=True)
    ap.add_argument("--provider", required=True)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--configs", nargs="*", default=["with_skill", "without_skill"])
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=420)
    a = ap.parse_args()
    ws = Path(a.workspace).resolve()
    ws.mkdir(parents=True, exist_ok=True)
    evs = [e for e in load_evals() if not a.only or e["name"] in a.only]
    for e in evs:
        d = ws / f"eval-{e['id']}-{e['name']}"
        d.mkdir(exist_ok=True)
        (d / "eval_metadata.json").write_text(json.dumps({"eval_id": e["id"], "eval_name": e["name"],
                                                          "prompt": e["prompt"], "assertions": e["expectations"]}, indent=1))
    jobs = [(e, c, n) for e in evs for c in a.configs for n in range(1, a.runs + 1)]
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        list(ex.map(lambda j: one(*j, ws, a), jobs))


if __name__ == "__main__":
    main()
