#!/usr/bin/env python3
"""Orchestrator side of factory's per-repo fan-out (phases 2 and 3).

The fan-out itself runs in `workflows/factory-fanout.js` (>= 4 repos) or a
single `Task` batch (< 4 repos). Neither can touch the filesystem reliably
enough to own state, so the orchestrator brackets every fan-out with this
script, which only talks to `.agents/factory-state.json` through `state.py`:

  fanout.py args   --stage S --workspace W --kb P [--verified D] [--jobs N] [--mark]
      JSON `args` for the workflow (or the Task batch): repos from
      `state.py pending-repos <stage> --ready`, each with the stages still to do
      and prior results of stages already done (so nothing is recomputed).
  fanout.py record --stage S (--result FILE | --log FILE) [--dry-run]
      Record per-repo stage results: writes artifacts under
      `.agents/factory-fanout/` and calls `state.set_repo` (done / blocked).
      `--result` takes the merged report; `--log` takes anything containing
      `FANOUT {...}` progress lines (workflow_status output or record.json of a
      killed run) and records every stage that finished before the kill.
  fanout.py merge  --stage S FILE...      merge Task-batch RepoResult objects exactly like the workflow
  fanout.py plan   --repos N [--remaining R] [--per-repo C]   gh rate-limit chunking
  fanout.py route  --repos N              which fan-out path to use (Task batch vs workflow)

Stdlib only. Exit 0 ok, 1 refused / invalid input, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import state as S  # noqa: E402

RUN_STAGES = {"bootstrap": ("clone",), "research": ("survey", "graph", "draft")}
WORKFLOW_MIN_REPOS = 4          # < 4 -> one Task call; >= 4 -> workflow
DEFAULT_JOBS = 8                # --jobs contract for repo_graph.py / detect_*.sh
PER_AGENT_TOKENS = 6000         # output-token estimate per fast agent() call
CONNECTIONS_TOKENS = 20000      # the one intelligent call per research run
# gh api core calls per repo (ceilings, see references/parallelism.md)
GH_CALLS = {"clone": 1, "graph": 15, "tracker": 4, "monitoring": 4}
GH_CORE_PER_HOUR = 5000
GH_RESERVE = 500                # never plan to spend the last 500 calls
FANOUT_RE = re.compile(r"FANOUT (\{.*\})\s*$")


class FanoutError(Exception):
    pass


def _art_dir(state_file: Path) -> Path:
    return state_file.parent / "factory-fanout"


def _artifact(state_file: Path, repo: str, stage: str) -> Path:
    return _art_dir(state_file) / f"{repo}.{stage}.json"


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------ args

def build_args(state_file: Path, stage: str, workspace: str, kb: str, verified: str | None,
               jobs: int = DEFAULT_JOBS, skill_dir: str | None = None, mark: bool = False) -> dict:
    if stage not in RUN_STAGES:
        raise FanoutError(f"--stage must be one of {', '.join(RUN_STAGES)}")
    with S.locked(state_file):
        st = S.load(state_file)
    if st is None:
        raise FanoutError(f"no state at {state_file}; run state.py init first")
    stages = RUN_STAGES[stage]
    # A repo is in this run when its first unfinished stage of the run is ready.
    names = sorted({n for s in stages for n in S.pending_repos(st, s, ready=True)})
    repos = []
    for name in names:
        r = st["repos"][name]
        todo = [s for s in stages if r["stages"].get(s, "pending") not in S.FINISHED]
        prior = {s: _read(_artifact(state_file, name, s)) for s in stages if s not in todo}
        repos.append({"name": name, "path": r.get("path"), "url": r.get("url"), "todo": todo,
                      "prior": {k: v for k, v in prior.items() if v is not None}})
        if not r.get("path") or not r.get("url"):
            raise FanoutError(f"repo {name} has no path/url in state; set-repo it with --path/--url first")
    context = []
    if stage == "research":
        for name in sorted(set(st["repos"]) - set(names)):
            d = _read(_artifact(state_file, name, "draft"))
            if d is not None and st["repos"][name]["stages"].get("draft") == "done":
                context.append(d)
    if mark:
        for r in repos:
            for s in r["todo"]:
                S.set_repo(state_file, r["name"], s, "in_progress")
    n_agents = sum(len(r["todo"]) for r in repos)
    est = n_agents * PER_AGENT_TOKENS + (CONNECTIONS_TOKENS if stage == "research" and (repos or context) else 0)
    return {
        "stage": stage,
        "workspace": workspace,
        "kbPath": kb,
        "skillDir": skill_dir or str(Path(__file__).resolve().parents[1]),
        "verified": verified or S.now()[:10],
        "jobs": jobs,
        "perAgentTokens": PER_AGENT_TOKENS,
        "route": route(len(repos)),
        "budget": f"+{max(50, -(-est * 3 // 2 // 1000))}k",  # 1.5x estimate, pass as the workflow `budget`
        "repos": repos,
        "contextDrafts": context,
    }


def route(n: int) -> str:
    if n == 0:
        return "none"
    return "task-batch" if n < WORKFLOW_MIN_REPOS else "workflow"


# ------------------------------------------------------------------ merge (mirror of the JS merge())

def merge(stage: str, per_repo: list, connections=None) -> dict:
    repos = {}
    for r in sorted(per_repo, key=lambda x: x["name"]):
        repos[r["name"]] = {"status": r["status"], "results": r["results"],
                            "failed_stage": r.get("failed_stage") or None}
    names = list(repos)
    return {
        "stage": stage,
        "repos": repos,
        "connections": connections if connections else None,
        "counts": {
            "repos": len(names),
            "ok": sum(1 for n in names if repos[n]["status"] == "ok"),
            "failed": sum(1 for n in names if repos[n]["status"] != "ok"),
        },
    }


def check_repo_result(r) -> None:
    if not isinstance(r, dict) or not isinstance(r.get("name"), str) or not isinstance(r.get("results"), dict) \
            or r.get("status") not in ("ok", "failed", "budget"):
        raise FanoutError(f"not a RepoResult: {json.dumps(r)[:200]}")


# ------------------------------------------------------------------ record

def _stage_ok(stage: str, res) -> bool:
    return res is not None and not (stage == "clone" and isinstance(res, dict) and res.get("status") == "failed")


def events_from_report(report: dict) -> list[dict]:
    ev = []
    stages = RUN_STAGES[report["stage"]]
    for name, r in report["repos"].items():
        for s in stages:
            if s in r["results"] and r.get("failed_stage") != s:
                res = r["results"][s]
                ev.append({"repo": name, "stage": s, "status": "done" if _stage_ok(s, res) else "failed", "result": res})
            elif r.get("failed_stage") == s:
                ev.append({"repo": name, "stage": s, "status": "failed" if r["status"] == "failed" else "skipped-budget",
                           "result": r["results"].get(s)})
    return ev


def events_from_log(text: str) -> list[dict]:
    """Parse `FANOUT {...}` lines from workflow_status markdown, record.json, or raw logs."""
    lines = []
    try:
        rec = json.loads(text)
        if isinstance(rec, dict) and isinstance(rec.get("progress"), list):
            lines = [p.get("message", "") for p in rec["progress"]]
    except ValueError:
        pass
    if not lines:
        lines = text.splitlines()
    out = []
    for ln in lines:
        m = FANOUT_RE.search(ln.strip())
        if m:
            try:
                out.append(json.loads(m.group(1)))
            except ValueError:
                continue
    return out


def record(state_file: Path, stage: str, events: list[dict], connections=None, dry_run: bool = False) -> dict:
    if stage not in RUN_STAGES:
        raise FanoutError(f"--stage must be one of {', '.join(RUN_STAGES)}")
    with S.locked(state_file):
        st = S.load(state_file)
    if st is None:
        raise FanoutError(f"no state at {state_file}")
    done, failed, ignored = [], [], []
    for e in events:
        repo, s, status = e.get("repo"), e.get("stage"), e.get("status")
        if s not in RUN_STAGES[stage] or repo not in st["repos"]:
            ignored.append(f"{repo}:{s}")
            continue
        if st["repos"][repo]["stages"].get(s) == "done":
            ignored.append(f"{repo}:{s} (already done)")
            continue
        if status == "done":
            if not dry_run:
                p = _artifact(state_file, repo, s)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(e.get("result"), indent=2, sort_keys=True) + "\n", encoding="utf-8")
                S.set_repo(state_file, repo, s, "done")
            done.append(f"{repo}:{s}")
        elif status == "failed":
            if not dry_run:
                S.set_repo(state_file, repo, s, "blocked")
            failed.append(f"{repo}:{s}")
        else:  # skipped-budget: leave pending so the next run picks it up
            if not dry_run and st["repos"][repo]["stages"].get(s) == "in_progress":
                S.set_repo(state_file, repo, s, "pending")
            ignored.append(f"{repo}:{s} ({status})")
    # A killed run leaves stages marked in_progress by `args --mark`: return them to pending.
    if not dry_run:
        with S.locked(state_file):
            st = S.load(state_file)
        seen = {(e.get("repo"), e.get("stage")) for e in events}
        for name, r in st["repos"].items():
            for s in RUN_STAGES[stage]:
                if r["stages"].get(s) == "in_progress" and (name, s) not in seen:
                    S.set_repo(state_file, name, s, "pending")
    if connections is not None and not dry_run:
        p = _art_dir(state_file) / "connections.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(connections, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"done": done, "failed": failed, "ignored": ignored,
            "connections": connections is not None}


# ------------------------------------------------------------------ plan

def plan(n_repos: int, remaining: int | None = None, per_repo: int | None = None) -> dict:
    per = per_repo or sum(GH_CALLS.values())
    avail = (GH_CORE_PER_HOUR if remaining is None else remaining) - GH_RESERVE
    if avail <= 0:
        return {"per_repo_calls": per, "total_calls": per * n_repos, "fits": False, "chunk_size": 0, "chunks": [],
                "advice": "rate limit nearly exhausted; wait for reset (gh api rate_limit .resources.core.reset)"}
    chunk = max(1, avail // per)
    chunks = [min(chunk, n_repos - i) for i in range(0, n_repos, chunk)]
    return {"per_repo_calls": per, "total_calls": per * n_repos, "available": avail,
            "fits": per * n_repos <= avail, "chunk_size": chunk, "chunks": chunks,
            "advice": "one run" if len(chunks) <= 1 else
            f"{len(chunks)} runs of <= {chunk} repos; start the next after the core reset"}


# ------------------------------------------------------------------ cli

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="factory fan-out orchestration helper")
    ap.add_argument("--file", help="state file (default: $FACTORY_STATE or ./.agents/factory-state.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("args")
    p.add_argument("--stage", required=True); p.add_argument("--workspace", required=True)
    p.add_argument("--kb", required=True); p.add_argument("--verified"); p.add_argument("--skill-dir")
    p.add_argument("--jobs", type=int, default=DEFAULT_JOBS); p.add_argument("--mark", action="store_true")
    p = sub.add_parser("record")
    p.add_argument("--stage", required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--result"); g.add_argument("--log")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("merge"); p.add_argument("--stage", required=True)
    p.add_argument("--connections"); p.add_argument("files", nargs="+")
    p = sub.add_parser("plan"); p.add_argument("--repos", type=int, required=True)
    p.add_argument("--remaining", type=int); p.add_argument("--per-repo", type=int)
    p = sub.add_parser("route"); p.add_argument("--repos", type=int, required=True)
    a = ap.parse_args(argv)
    path = S.state_path(a.file)

    def load_json(src: str):
        text = sys.stdin.read() if src == "-" else Path(src).read_text(encoding="utf-8")
        return text

    try:
        if a.cmd == "args":
            out = build_args(path, a.stage, a.workspace, a.kb, a.verified, a.jobs, a.skill_dir, a.mark)
        elif a.cmd == "record":
            if a.result:
                try:
                    rep = json.loads(load_json(a.result))
                except ValueError as e:
                    raise FanoutError(f"--result is not JSON: {e}")
                if rep.get("stage") != a.stage:
                    raise FanoutError(f"report stage {rep.get('stage')!r} != --stage {a.stage!r}")
                out = record(path, a.stage, events_from_report(rep), rep.get("connections"), a.dry_run)
            else:
                out = record(path, a.stage, events_from_log(load_json(a.log)), None, a.dry_run)
        elif a.cmd == "merge":
            items = []
            for f in a.files:
                v = json.loads(load_json(f))
                for r in (v if isinstance(v, list) else [v]):
                    check_repo_result(r)
                    items.append(r)
            conn = json.loads(load_json(a.connections)) if a.connections else None
            out = merge(a.stage, items, conn)
        elif a.cmd == "plan":
            out = plan(a.repos, a.remaining, a.per_repo)
        else:
            out = {"repos": a.repos, "route": route(a.repos)}
    except (FanoutError, S.StateError, OSError, ValueError) as e:
        print(f"fanout.py: {e}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
