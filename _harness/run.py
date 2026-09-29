#!/usr/bin/env python3
"""Shared compatibility + fixture harness for the tailcallhq/skills library.

    python3 _harness/run.py [--keep] [--out evidence.json] [--only NAME ...]
    python3 _harness/run.py --prompt-smoke --model M --provider P   # optional, spends tokens

Default mode spends no model tokens and mutates nothing outside a temp dir:
it inventories the target forge3 runtime, builds disposable fixtures, runs
their verify recipes, checks negative capability gates, exercises
changed-file discovery across awkward git states, self-tests redaction and
process cleanup, and runs distribution checks against the real skill loader.

A guard snapshots the user-global skill checkout and this repo's git status
before and after, and fails the run if either changed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import changes, distribution, fixtures, proc, runtime  # noqa: E402
from lib.redact import redact, safe_env  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


class Report:
    def __init__(self) -> None:
        self.results: list[dict] = []

    def add(self, name: str, status: str, detail: str = "", seconds: float | None = None, **extra) -> None:
        assert status in ("pass", "fail", "blocked", "skipped")
        self.results.append({"scenario": name, "status": status, "detail": redact(detail)[:800],
                             "seconds": None if seconds is None else round(seconds, 2), **extra})
        mark = {"pass": "PASS", "fail": "FAIL", "blocked": "BLOCKED", "skipped": "SKIP"}[status]
        print(f"[{mark:7}] {name}: {redact(detail)[:160]}", file=sys.stderr)


def timed(fn):
    t = time.monotonic()
    out = fn()
    return out, time.monotonic() - t


# --- guards ------------------------------------------------------------------

def _guard_snapshot() -> dict:
    snap = {}
    home_skills = Path.home() / ".forge" / "tailcall-skills"
    for label, path in (("user_global_skills", home_skills), ("this_repo", REPO)):
        if (path / ".git").exists():
            head = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            st = subprocess.run(["git", "-C", str(path), "status", "--porcelain"], capture_output=True, text=True).stdout
            snap[label] = {"head": head, "status": st}
    return snap


# --- scenarios ---------------------------------------------------------------

def scenario_ts_web(root: Path, inv: dict, rep: Report) -> None:
    if not shutil.which("node"):
        return rep.add("fixture:ts-web", "blocked", "node not on PATH")
    ver = subprocess.run(["node", "--version"], capture_output=True, text=True).stdout.strip()
    major, minor = (int(x) for x in ver.lstrip("v").split(".")[:2])
    if (major, minor) < (22, 6):
        return rep.add("fixture:ts-web", "blocked", f"node {ver} lacks --experimental-strip-types")
    d = fixtures.make(root, "ts-web")
    cp, s = timed(lambda: proc.run(["npm", "test", "--silent"], cwd=d, timeout=120, env=safe_env()))
    rep.add("fixture:ts-web:test", "pass" if cp.returncode == 0 else "fail",
            f"node {ver}; exit {cp.returncode}; {(cp.stdout + cp.stderr)[-300:]}", s)
    port = proc.free_port()
    env = {**safe_env(), "PORT": str(port)}
    log = root / "ts-web.log"
    try:
        with proc.background(["node", "--experimental-strip-types", "src/server.ts"], cwd=d, log_path=log, env=env) as p:
            waited = proc.wait_until(proc.http_ok(f"http://127.0.0.1:{port}/health"), timeout=20, proc=p)
            import urllib.request
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/todos", method="POST")
            with urllib.request.urlopen(req, timeout=5) as r:
                body = r.read().decode()
            pid = p.pid
        alive = _pid_alive(pid)
        rep.add("fixture:ts-web:run", "pass" if (not alive and '"title":"item"' in body) else "fail",
                f"ready in {waited:.2f}s on port {port}; POST /api/todos -> {body[:80]}; reaped={not alive}", waited)
    except Exception as e:  # noqa: BLE001
        rep.add("fixture:ts-web:run", "fail", f"{e}; log: {log.read_text()[-300:] if log.exists() else ''}")


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def scenario_py_cli(root: Path, inv: dict, rep: Report) -> None:
    d = fixtures.make(root, "py-cli")
    cp, s = timed(lambda: proc.run([sys.executable, "-m", "unittest", "-q"], cwd=d, timeout=60, env=safe_env()))
    rep.add("fixture:py-cli:test", "pass" if cp.returncode == 0 else "fail", f"exit {cp.returncode}; {cp.stderr[-200:]}", s)
    cp = proc.run([sys.executable, "-m", "wclite.cli"], cwd=d, timeout=30, input="a b\nc\n", env=safe_env())
    rep.add("fixture:py-cli:run", "pass" if cp.stdout.strip() == "2 3" else "fail", f"stdout={cp.stdout.strip()!r}")


def scenario_rust_lib(root: Path, inv: dict, rep: Report) -> None:
    if not shutil.which("cargo"):
        return rep.add("fixture:rust-lib", "blocked", "cargo not on PATH")
    d = fixtures.make(root, "rust-lib")
    env = {**safe_env(), "CARGO_TARGET_DIR": str(root / "cargo-target")}
    cp, s = timed(lambda: proc.run(["cargo", "test", "--offline", "--quiet"], cwd=d, timeout=300, env=env))
    rep.add("fixture:rust-lib:test", "pass" if cp.returncode == 0 else "fail",
            f"exit {cp.returncode}; {(cp.stdout + cp.stderr)[-200:]}", s)


def scenario_negative(root: Path, inv: dict, rep: Report) -> None:
    for name, (_, gate) in fixtures.NEGATIVE.items():
        fixtures.make(root, name)
        ok, why = runtime.require(inv, gate=gate)
        # A present-but-unverified driver is still not a pass: nothing here exercises it.
        status = "blocked" if not ok else "skipped"
        rep.add(f"negative:{name}", status,
                f"{why}; reported as unsupported — no claim of support without an exercised driver",
                gate=gate)


def scenario_timeout_cleanup(root: Path, inv: dict, rep: Report) -> None:
    if os.name == "nt":
        return rep.add("helper:timeout_kills_group", "skipped", "POSIX-only probe")
    marker = root / "grandchild.pid"
    script = f"sleep 60 & echo $! > {marker}; wait"
    t = time.monotonic()
    try:
        proc.run(["sh", "-c", script], cwd=root, timeout=1.5)
        rep.add("helper:timeout_kills_group", "fail", "did not time out")
    except subprocess.TimeoutExpired:
        time.sleep(0.3)
        gpid = int(marker.read_text().strip()) if marker.exists() else -1
        alive = gpid > 0 and _pid_alive(gpid)
        rep.add("helper:timeout_kills_group", "fail" if alive else "pass",
                f"grandchild {gpid} alive={alive}", time.monotonic() - t)
    try:
        proc.run(["true"], cwd=root / "does-not-exist")
        rep.add("helper:explicit_cwd_required", "fail", "ran in a missing cwd")
    except FileNotFoundError:
        rep.add("helper:explicit_cwd_required", "pass", "missing cwd rejected before spawn")


def scenario_redaction(root: Path, inv: dict, rep: Report) -> None:
    fake = {
        "GITHUB_TOKEN=ghp_" + "A" * 36: "ghp_",
        "Authorization: Bearer abc.def.ghi-" + "x" * 20: "abc.def",
        '{"api_key": "sk-proj-' + "B" * 24 + '"}': "sk-proj-B",
        "https://user:hunter2pass@example.invalid/repo": "hunter2pass",
        "AKIA" + "C" * 16: "AKIA" + "C" * 16,
    }
    leaks = [k for k, needle in fake.items() if needle in redact(k) and "REDACTED" not in redact(k)]
    leaks += [k for k, needle in fake.items() if needle[-6:] in redact(k)]
    rep.add("helper:redaction", "fail" if leaks else "pass", f"{len(fake)} synthetic secrets; leaks={len(set(leaks))}")


def scenario_changes(root: Path, inv: dict, rep: Report) -> None:
    v = fixtures.make_git_variants(root)
    expect = {
        "initial": (lambda c: not c.has_commits and "a.txt" in c.staged and "b.txt" in c.untracked),
        "dirty": (lambda c: c.staged == ["staged.txt"] and "tracked.txt" in c.unstaged and "new.txt" in c.untracked),
        "no_upstream": (lambda c: c.base is None and "no upstream" in c.base_reason and c.committed == []),
        "feature": (lambda c: c.committed == ["y.txt"] and c.base_reason.startswith("fallback")),
    }
    for k, pred in expect.items():
        c = changes.discover(v[k])
        rep.add(f"changes:{k}", "pass" if pred(c) else "fail", json.dumps(c.to_dict())[:400])
    c = changes.discover(v["no_upstream"], base="trunk")
    rep.add("changes:no_upstream_explicit_base", "pass" if c.committed == ["y.txt"] else "fail", f"committed={c.committed}")
    c = changes.discover(v["monorepo"], scope=v["monorepo"] / "packages" / "api")
    ok = c.all_files == ["packages/api/index.ts", "packages/api/new.ts"]
    rep.add("changes:monorepo_scope", "pass" if ok else "fail", f"all_files={c.all_files}")
    try:
        changes.discover(root)
        rep.add("changes:not_a_repo", "fail", "no error outside a repository")
    except RuntimeError:
        rep.add("changes:not_a_repo", "pass", "clear error outside a repository")


def scenario_distribution(root: Path, inv: dict, rep: Report) -> dict:
    for l in distribution.lint():
        rep.add(f"lint:{l['skill']}", "pass" if l["ok"] else "fail",
                "; ".join(l["problems"] + [f"warn: {w}" for w in l["warnings"]]) or "ok")
    ev, s = timed(distribution.check)
    for c in ev["checks"]:
        rep.add(f"dist:{c['check']}", "pass" if c["ok"] else "fail", c["detail"])
    rep.add("dist:cleanup", "pass" if ev.get("cleaned_up") else "fail", ev["fixture"], s)
    return ev


def prompt_smoke(root: Path, rep: Report, model: str, provider: str) -> dict:
    """One real model turn in a disposable fixture: records model/provider, timing and skill loads."""
    creator = Path.home() / ".forge" / "tailcall-skills" / "skill-author"
    sys.path.insert(0, str(creator))
    from scripts import forge_client  # type: ignore
    sub = root / "smoke"
    sub.mkdir(exist_ok=True)
    d = fixtures.make(sub, "py-cli")
    prompt = ("Run this project's tests and tell me whether they pass. Do not modify files, do not "
              "install anything, do not use the network, and do not write outside this directory.")
    t = time.monotonic()
    res = forge_client.run_prompt(prompt, cwd=str(d), timeout=180, model=model, provider=provider)
    dur = time.monotonic() - t
    status = subprocess.run(["git", "status", "--porcelain"], cwd=d, capture_output=True, text=True).stdout
    ev = {"model": model, "provider": provider, "seconds": round(dur, 1), "timed_out": res.get("timed_out"),
          "skills_loaded": res.get("skills_loaded"), "tool_calls": [c.get("name") for c in res.get("tool_calls", [])],
          "fixture_mutated": bool(status.strip()), "text_tail": redact(res.get("text", ""))[-400:]}
    ok = not res.get("timed_out") and not ev["fixture_mutated"]
    rep.add("prompt_smoke", "pass" if ok else "fail",
            f"{model}/{provider} {dur:.1f}s skills={ev['skills_loaded']} mutated={ev['fixture_mutated']}", dur)
    return ev


SCENARIOS = {
    "ts-web": scenario_ts_web, "py-cli": scenario_py_cli, "rust-lib": scenario_rust_lib,
    "negative": scenario_negative, "cleanup": scenario_timeout_cleanup, "redaction": scenario_redaction,
    "changes": scenario_changes,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="write evidence JSON here")
    ap.add_argument("--keep", action="store_true", help="keep the temp fixture root")
    ap.add_argument("--only", nargs="*", help=f"subset of {list(SCENARIOS) + ['distribution']}")
    ap.add_argument("--prompt-smoke", action="store_true", help="run one real model turn (spends tokens)")
    ap.add_argument("--model")
    ap.add_argument("--provider")
    a = ap.parse_args()
    if a.prompt_smoke and not (a.model and a.provider):
        ap.error("--prompt-smoke needs explicit --model and --provider (no default model is assumed)")

    started = time.time()
    before = _guard_snapshot()
    root = Path(tempfile.mkdtemp(prefix="skills-harness-"))
    rep = Report()
    evidence: dict = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
                      "platform": {"os": sys.platform, "python": sys.version.split()[0]},
                      "fixture_root": str(root),
                      "model_provider": {"model": a.model, "provider": a.provider} if a.prompt_smoke
                      else "none (no model turn; runtime probes only)"}
    try:
        empty = root / "runtime-probe"
        empty.mkdir()
        try:
            inv, s = timed(lambda: runtime.inventory(empty))
            evidence["runtime"] = inv
            rep.add("runtime:inventory", "pass",
                    f"{inv['binary']} v{inv['version']}: {len(inv['rpc_methods'])} methods, "
                    f"{inv['extension_request_variants']} extension variants, "
                    f"{sum(1 for e in inv['extensions'] if e['enabled'])} enabled extensions, "
                    f"{len(inv['tools'])} tools, {len(inv['skills'])} skills, {len(inv['commands'])} commands", s)
            for g, info in inv["classification"]["gates"].items():
                rep.add(f"gate:{g}", "pass" if info["status"] == "present" else "blocked",
                        f"{info['kind']}: {info['status']}")
        except Exception as e:  # noqa: BLE001
            rep.add("runtime:inventory", "blocked", f"forge3 unavailable: {e}")
            inv = {"classification": {"callable_tools": [], "gates": {}}}

        only = set(a.only or list(SCENARIOS) + ["distribution"])
        for name, fn in SCENARIOS.items():
            if name in only:
                try:
                    fn(root, inv, rep)
                except Exception as e:  # noqa: BLE001
                    rep.add(f"{name}", "fail", f"harness error: {e!r}")
        if "distribution" in only and "runtime" in evidence:
            evidence["distribution"] = scenario_distribution(root, inv, rep)
        if a.prompt_smoke:
            evidence["prompt_smoke"] = prompt_smoke(root, rep, a.model, a.provider)
    finally:
        if not a.keep:
            shutil.rmtree(root, ignore_errors=True)
        after = _guard_snapshot()
        mutated = [k for k in before if before[k] != after.get(k)]
        rep.add("guard:no_external_mutation", "fail" if mutated else "pass",
                f"changed: {mutated}" if mutated else "user-global skills checkout and this repo unchanged")
        rep.add("guard:fixture_cleanup", "pass" if a.keep or not root.exists() else "fail",
                "kept by --keep" if a.keep else "temp root removed")

    evidence["duration_s"] = round(time.time() - started, 1)
    evidence["results"] = rep.results
    counts = {s: sum(1 for r in rep.results if r["status"] == s) for s in ("pass", "fail", "blocked", "skipped")}
    evidence["summary"] = counts
    text = redact(json.dumps(evidence, indent=2))
    if a.out:
        Path(a.out).write_text(text)
    print(json.dumps(counts))
    return 1 if counts["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
