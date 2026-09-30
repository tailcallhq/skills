#!/usr/bin/env python3
"""Resumable state for the factory skill: `.agents/factory-state.json`.

Stdlib only. Every mutation is a locked read-modify-write followed by an atomic
replace (tmp file + fsync + os.replace), so parallel fan-out callers can record
per-repo progress without losing writes. The previous good copy is kept as
`<file>.bak` and used to recover from a corrupt state file.

Usage (state file defaults to ./.agents/factory-state.json; override with
--file or FACTORY_STATE):

  state.py init [--org ORG] [--force]
  state.py get [DOTTED.PATH]
  state.py next [--json]
  state.py summary
  state.py set-phase ID STATUS [--output k=v ...] [--blocker MSG] [--force]
  state.py set-repo NAME STAGE STATUS [--path P] [--url U] [--force]
  state.py pending-repos STAGE [--ready]
  state.py set KEY VALUE              # org | kb.url|path|project_id | machine.cloud|push_triggers
  state.py set-routine ID [--automation-id A] [--cursor C]
  state.py set-change ID STEP STATUS [--platform P] [--system S] [--classification C]
                      [--env-scope staging|staging+prod] [--plan-hash H]
                      [--inverse-plan-path P] [--blocker MSG] [--force]
  state.py change-next ID [--json]

Exit codes: 0 ok, 1 refused / invalid argument, 2 usage error.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

VERSION = 2  # v2: infra_changes (v1 files are upgraded in place on load)
DEFAULT_FILE = Path(".agents") / "factory-state.json"

STATUSES = ("pending", "in_progress", "done", "skipped", "blocked")
FINISHED = ("done", "skipped")

# (id, name, per-repo stages the phase fans out over)
PHASES = (
    ("0", "preflight", ()),
    ("1", "knowledge base", ()),
    ("2", "bootstrap repos", ("clone",)),
    ("3", "research", ("survey", "graph", "draft")),
    ("4", "tracker MCP", ()),
    ("4b", "monitoring MCP", ()),
    ("4c", "infra read-only", ()),
    ("5", "board", ()),
    ("6", "routines", ()),
)
PHASE_IDS = tuple(p[0] for p in PHASES)
PHASE_NAMES = {p[0]: p[1] for p in PHASES}
PHASE_STAGES = {p[0]: p[2] for p in PHASES}
STAGES = ("clone", "survey", "graph", "draft")

# Infra change flow (references/infra-changes.md). Strictly ordered, none skippable
# except the *_prod steps when the ask was staging-only (env_scope == "staging").
CHANGE_STEPS = (
    "classified",
    "planned_staging", "approved_staging", "applied_staging", "verified_staging",
    "planned_prod", "approved_prod", "applied_prod", "verified_prod",
    "recorded",
)
PROD_STEPS = tuple(s for s in CHANGE_STEPS if s.endswith("_prod"))
APPLY_STEPS = ("applied_staging", "applied_prod")
CLASSIFICATIONS = ("additive", "mutating", "destructive")
ENV_SCOPES = ("staging", "staging+prod")
CHANGE_FIELDS = ("platform", "system", "classification", "env_scope", "plan_hash", "inverse_plan_path")

SETTABLE = {
    "org": str,
    "kb.url": str,
    "kb.path": str,
    "kb.project_id": str,
    "machine.cloud": bool,
    "machine.push_triggers": bool,
}

_THREAD_LOCK = threading.Lock()


class StateError(Exception):
    """A refused or invalid operation (exit code 1)."""


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def state_path(arg: str | None = None) -> Path:
    return Path(arg or os.environ.get("FACTORY_STATE") or DEFAULT_FILE)


# --------------------------------------------------------------------------- schema

def fresh(org: str | None = None) -> dict:
    ts = now()
    return {
        "version": VERSION,
        "started_at": ts,
        "org": org,
        "kb": {"url": None, "path": None, "project_id": None},
        "machine": {"cloud": None, "push_triggers": None},
        "phases": [
            {"id": pid, "name": name, "status": "pending", "outputs": {}, "blocker": None, "updated_at": ts}
            for pid, name, _ in PHASES
        ],
        "repos": {},
        "routines": {},
        "infra_changes": {},
    }


def upgrade(state: dict) -> bool:
    """Upgrade an older-version state dict in place. Returns True if changed."""
    if not isinstance(state, dict) or not isinstance(state.get("version"), int):
        return False
    changed = False
    if state["version"] < 2:
        state.setdefault("infra_changes", {})
        state["version"] = 2
        changed = True
    return changed


def validate(state: object) -> list[str]:
    """Return a list of schema problems (empty when valid)."""
    errs: list[str] = []
    if not isinstance(state, dict):
        return ["top level is not an object"]
    if not isinstance(state.get("version"), int):
        errs.append("version missing or not an int")
    elif state["version"] > VERSION:
        errs.append(f"version {state['version']} is newer than supported {VERSION}")
    for key, typ in (("kb", dict), ("machine", dict), ("repos", dict), ("routines", dict), ("phases", list)):
        if not isinstance(state.get(key), typ):
            errs.append(f"{key} missing or not a {typ.__name__}")
    if isinstance(state.get("phases"), list):
        ids = []
        for p in state["phases"]:
            if not isinstance(p, dict) or p.get("status") not in STATUSES or "id" not in p:
                errs.append(f"bad phase entry: {p!r}"[:120])
            else:
                ids.append(p["id"])
        missing = [pid for pid in PHASE_IDS if pid not in ids]
        if missing:
            errs.append(f"phases missing: {missing}")
    if isinstance(state.get("repos"), dict):
        for name, r in state["repos"].items():
            if not isinstance(r, dict) or not isinstance(r.get("stages"), dict):
                errs.append(f"bad repo entry: {name}")
            elif any(v not in STATUSES for v in r["stages"].values()):
                errs.append(f"bad stage status in repo: {name}")
    if state.get("version") == VERSION and not isinstance(state.get("infra_changes"), dict):
        errs.append("infra_changes missing or not a dict")
    if isinstance(state.get("infra_changes"), dict):
        for cid, c in state["infra_changes"].items():
            if not isinstance(c, dict) or not isinstance(c.get("steps"), dict):
                errs.append(f"bad infra change entry: {cid}")
            elif any(k not in CHANGE_STEPS or v not in STATUSES for k, v in c["steps"].items()):
                errs.append(f"bad step in infra change: {cid}")
            elif c.get("classification") not in (None,) + CLASSIFICATIONS:
                errs.append(f"bad classification in infra change: {cid}")
            elif c.get("env_scope") not in (None,) + ENV_SCOPES:
                errs.append(f"bad env_scope in infra change: {cid}")
    return errs


# --------------------------------------------------------------------------- io

@contextlib.contextmanager
def locked(path: Path, timeout: float = 30.0):
    """Exclusive lock on `<path>.lock`, across threads and processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with _THREAD_LOCK:
        try:
            import fcntl  # POSIX
        except ImportError:  # pragma: no cover - Windows fallback: O_EXCL lock file
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    break
                except FileExistsError:
                    if time.monotonic() > deadline:
                        raise StateError(f"timed out waiting for lock {lock_path}")
                    time.sleep(0.02)
            try:
                yield
            finally:
                os.close(fd)
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(lock_path)
            return
        with open(lock_path, "a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    upgrade(data)
    return data if not validate(data) else None


def load(path: Path, warn=None) -> dict | None:
    """Load state; recover from corruption. Returns None when no state exists.

    Recovery order for an unreadable/invalid file: quarantine it as
    `<file>.corrupt-<ts>`, then restore `<file>.bak` if valid, else start fresh
    (keeping `org` if it can be salvaged). Must be called under `locked()` when
    the result will be written back.
    """
    warn = warn or (lambda m: print(f"state.py: {m}", file=sys.stderr))
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError) as e:
        data, problems = None, [f"unparseable: {e}"]
    else:
        upgrade(data)  # older schema: upgraded in memory, persisted on the next write
        problems = validate(data)
    if not problems:
        return data
    if isinstance(data, dict) and isinstance(data.get("version"), int) and data["version"] > VERSION:
        raise StateError(f"{path}: version {data['version']} is newer than this script supports ({VERSION})")
    quarantine = path.with_name(f"{path.name}.corrupt-{int(time.time() * 1000)}")
    os.replace(path, quarantine)
    backup = _read_json(path.with_name(path.name + ".bak"))
    if backup is not None:
        warn(f"state file was corrupt ({'; '.join(problems)}); moved to {quarantine.name}, restored from .bak")
        _write(path, backup, keep_backup=False)
        return backup
    org = data.get("org") if isinstance(data, dict) and isinstance(data.get("org"), str) else None
    warn(f"state file was corrupt ({'; '.join(problems)}); moved to {quarantine.name}, no valid .bak, "
         f"starting fresh - completed work must be re-verified")
    state = fresh(org)
    state["recovered_from"] = quarantine.name
    _write(path, state, keep_backup=False)
    return state


def _write(path: Path, state: dict, keep_backup: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if keep_backup and path.exists() and _read_json(path) is not None:
        bak = path.with_name(path.name + ".bak")
        tmp_bak = bak.with_name(bak.name + ".tmp")
        tmp_bak.write_bytes(path.read_bytes())
        os.replace(tmp_bak, bak)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def mutate(path: Path, fn, create: bool = False):
    """Locked read-modify-write. `fn(state)` mutates in place and may return a value."""
    with locked(path):
        state = load(path)
        if state is None:
            if not create:
                raise StateError(f"no state file at {path}; run `state.py init` first")
            state = fresh()
        result = fn(state)
        _write(path, state)
        return result


# --------------------------------------------------------------------------- operations

def _phase(state: dict, pid: str) -> dict:
    for p in state["phases"]:
        if p["id"] == pid:
            return p
    raise StateError(f"unknown phase {pid!r}; expected one of {', '.join(PHASE_IDS)}")


def _check_status(status: str) -> None:
    if status not in STATUSES:
        raise StateError(f"unknown status {status!r}; expected one of {', '.join(STATUSES)}")


def init(path: Path, org: str | None = None, force: bool = False) -> tuple[dict, bool]:
    """Create the state file. Existing state is kept unless force. Returns (state, created)."""
    with locked(path):
        existing = None if force else load(path)
        if existing is not None:
            if org and existing.get("org") and existing["org"] != org:
                raise StateError(f"state already exists for org {existing['org']!r}; "
                                 f"pass --force to start over for {org!r}")
            if org and not existing.get("org"):
                existing["org"] = org
                _write(path, existing)
            return existing, False
        state = fresh(org)
        _write(path, state)
        return state, True


def set_phase(path: Path, pid: str, status: str, outputs: dict | None = None,
              blocker: str | None = None, force: bool = False) -> dict:
    _check_status(status)
    if status == "blocked" and not blocker:
        raise StateError("status 'blocked' requires --blocker MSG")

    def fn(state):
        p = _phase(state, pid)
        if p["status"] == "done" and status != "done" and not force:
            raise StateError(f"phase {pid} is done; refusing to set it to {status!r} without --force")
        p["status"] = status
        p["blocker"] = blocker if status == "blocked" else None
        if outputs:
            p.setdefault("outputs", {}).update(outputs)
        p["updated_at"] = now()
        return p

    return mutate(path, fn)


def set_repo(path: Path, name: str, stage: str, status: str, repo_path: str | None = None,
             url: str | None = None, force: bool = False) -> dict:
    _check_status(status)
    if stage not in STAGES:
        raise StateError(f"unknown stage {stage!r}; expected one of {', '.join(STAGES)}")

    def fn(state):
        r = state["repos"].setdefault(name, {"path": None, "url": None,
                                             "stages": {s: "pending" for s in STAGES}})
        for s in STAGES:
            r["stages"].setdefault(s, "pending")
        cur = r["stages"][stage]
        if cur == "done" and status != "done" and not force:
            raise StateError(f"repo {name} stage {stage} is done; refusing to set it to {status!r} without --force")
        r["stages"][stage] = status
        if repo_path is not None:
            r["path"] = repo_path
        if url is not None:
            r["url"] = url
        r["updated_at"] = now()
        return r

    return mutate(path, fn)


def pending_repos(state: dict | None, stage: str, ready: bool = False) -> list[str]:
    """Repos whose `stage` is not done/skipped. With ready, also require earlier stages finished."""
    if stage not in STAGES:
        raise StateError(f"unknown stage {stage!r}; expected one of {', '.join(STAGES)}")
    if not state:
        return []
    earlier = STAGES[:STAGES.index(stage)]
    out = []
    for name in sorted(state["repos"]):
        stages = state["repos"][name]["stages"]
        if stages.get(stage, "pending") in FINISHED:
            continue
        if ready and any(stages.get(s, "pending") not in FINISHED for s in earlier):
            continue
        out.append(name)
    return out


def set_key(path: Path, key: str, value: str) -> dict:
    if key not in SETTABLE:
        raise StateError(f"cannot set {key!r}; settable keys: {', '.join(SETTABLE)}")
    if SETTABLE[key] is bool:
        low = value.lower()
        if low not in ("true", "false", "null"):
            raise StateError(f"{key} takes true|false|null")
        val = None if low == "null" else low == "true"
    else:
        val = None if value == "" else value

    def fn(state):
        parts = key.split(".")
        target = state
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = val
        return state

    return mutate(path, fn)


def set_routine(path: Path, rid: str, automation_id: str | None = None, cursor: str | None = None) -> dict:
    def fn(state):
        r = state["routines"].setdefault(rid, {"automation_id": None, "cursor": None})
        if automation_id is not None:
            r["automation_id"] = automation_id
        if cursor is not None:
            r["cursor"] = cursor
        r["updated_at"] = now()
        return r

    return mutate(path, fn)


def _new_change() -> dict:
    c = {k: None for k in CHANGE_FIELDS}
    c.update({"steps": {s: "pending" for s in CHANGE_STEPS}, "blocker": None, "updated_at": now()})
    return c


def set_change(path: Path, cid: str, step: str, status: str, fields: dict | None = None,
               blocker: str | None = None, force: bool = False) -> dict:
    """Advance one step of an infra change. Enforces the infra-changes.md ordering.

    - steps run strictly in CHANGE_STEPS order: a step may only become
      in_progress/done once every earlier step is done (or a prod step skipped);
    - only *_prod steps may be `skipped`, and only when env_scope == "staging";
    - `done -> other` is refused unless --force, EXCEPT applied_* which can never
      be downgraded or re-run (not even with --force): an apply that started
      (`in_progress`) may only move to done or blocked;
    - classification/env_scope are required before `classified` is done;
      plan_hash is required before planned_staging is done and is immutable once
      approved_staging is done (a different change needs a new change id).
    """
    _check_status(status)
    if step not in CHANGE_STEPS:
        raise StateError(f"unknown change step {step!r}; expected one of {', '.join(CHANGE_STEPS)}")
    if status == "blocked" and not blocker:
        raise StateError("status 'blocked' requires --blocker MSG")
    fields = {k: v for k, v in (fields or {}).items() if v is not None}
    for k in fields:
        if k not in CHANGE_FIELDS:
            raise StateError(f"unknown change field {k!r}")
    if fields.get("classification") and fields["classification"] not in CLASSIFICATIONS:
        raise StateError(f"classification must be one of {', '.join(CLASSIFICATIONS)}")
    if fields.get("env_scope") and fields["env_scope"] not in ENV_SCOPES:
        raise StateError(f"env_scope must be one of {', '.join(ENV_SCOPES)}")

    def fn(state):
        changes = state.setdefault("infra_changes", {})
        c = changes.setdefault(cid, _new_change())
        for s in CHANGE_STEPS:
            c["steps"].setdefault(s, "pending")
        steps = c["steps"]
        cur = steps[step]

        # field updates (with immutability rules)
        if "plan_hash" in fields and c.get("plan_hash") and fields["plan_hash"] != c["plan_hash"] \
                and steps["approved_staging"] == "done":
            raise StateError(f"change {cid}: plan_hash is fixed once staging is approved; "
                             f"open a new change id for a different change")
        if "classification" in fields and c.get("classification") and steps["classified"] == "done" \
                and fields["classification"] != c["classification"] and not force:
            if CLASSIFICATIONS.index(fields["classification"]) < CLASSIFICATIONS.index(c["classification"]):
                raise StateError(f"change {cid}: classification can only be raised after it is done "
                                 f"({c['classification']} -> {fields['classification']} refused)")
        if "env_scope" in fields and c.get("env_scope") and fields["env_scope"] != c["env_scope"] \
                and any(steps[s] not in ("pending",) for s in PROD_STEPS):
            raise StateError(f"change {cid}: env_scope cannot change once a prod step has started")
        merged = dict(c, **fields)

        # applied_* are one-way
        if step in APPLY_STEPS:
            if cur == "done" and status != "done":
                raise StateError(f"change {cid}: {step} is done; an applied step can never be re-run or "
                                 f"downgraded (roll back with the inverse plan as a new change)")
            if cur == "in_progress" and status not in ("in_progress", "done", "blocked"):
                raise StateError(f"change {cid}: {step} was started; it may have partially applied. "
                                 f"Inspect live state and mark it done or blocked, never {status!r}")
            if cur == "blocked" and status not in ("done", "blocked"):
                raise StateError(f"change {cid}: {step} is blocked after an apply attempt; it may only be "
                                 f"marked done (live state matches the plan) or stay blocked, never {status!r}")
        elif cur == "done" and status != "done" and not force:
            raise StateError(f"change {cid}: {step} is done; refusing to set it to {status!r} without --force")

        # skipping
        if status == "skipped":
            if step not in PROD_STEPS:
                raise StateError(f"change {cid}: {step} cannot be skipped (no step of the policy is skippable)")
            if merged.get("env_scope") != "staging":
                raise StateError(f"change {cid}: prod steps may only be skipped when env_scope is 'staging'")

        # ordering
        if status in ("in_progress", "done"):
            idx = CHANGE_STEPS.index(step)
            for prev in CHANGE_STEPS[:idx]:
                ok = steps[prev] == "done" or (prev in PROD_STEPS and steps[prev] == "skipped")
                if not ok:
                    raise StateError(f"change {cid}: cannot mark {step} {status} before {prev} is done "
                                     f"(currently {steps[prev]})")
            if step in PROD_STEPS and merged.get("env_scope") != "staging+prod":
                raise StateError(f"change {cid}: prod steps require env_scope 'staging+prod' "
                                 f"(the user's ask must include prod)")
            if step == "classified" and status == "done":
                for k in ("platform", "classification", "env_scope"):
                    if not merged.get(k):
                        raise StateError(f"change {cid}: --{k.replace('_', '-')} required before classified is done")
            if step == "planned_staging" and status == "done" and not merged.get("plan_hash"):
                raise StateError(f"change {cid}: --plan-hash required before planned_staging is done")
            if step == "applied_staging" and not merged.get("inverse_plan_path"):
                raise StateError(f"change {cid}: --inverse-plan-path required before applying")

        c.update(fields)
        steps[step] = status
        if status == "blocked":
            c["blocker"] = blocker
        elif not any(v == "blocked" for v in steps.values()):
            c["blocker"] = None
        c["updated_at"] = now()
        return {"id": cid, **c}

    return mutate(path, fn)


def change_next(state: dict | None, cid: str) -> dict:
    """Where a resumed infra change continues: the first step not finished."""
    c = (state or {}).get("infra_changes", {}).get(cid)
    if c is None:
        return {"id": cid, "state": "none", "step": CHANGE_STEPS[0], "status": "pending", "blocker": None,
                "done": [], "message": f"no infra change {cid!r}; start at step classified"}
    steps = c["steps"]
    done = [s for s in CHANGE_STEPS if steps.get(s) in FINISHED]
    for s in CHANGE_STEPS:
        st = steps.get(s, "pending")
        if st in FINISHED:
            continue
        msg = f"change {cid}: resuming at {s} ({st})"
        if st == "blocked":
            msg += f", blocked: {c.get('blocker')}"
        if s in APPLY_STEPS and st in ("in_progress", "blocked"):
            msg += ("; the apply was already started - do NOT re-apply: compare live state with the "
                    "plan, then mark it done, or roll back with the inverse plan")
        return {"id": cid, "state": "resume", "step": s, "status": st, "blocker": c.get("blocker"),
                "done": done, "never_rerun": [a for a in APPLY_STEPS if steps.get(a) in ("done", "in_progress", "blocked")],
                "message": msg}
    return {"id": cid, "state": "complete", "step": None, "status": "done", "blocker": None,
            "done": done, "never_rerun": [a for a in APPLY_STEPS if steps.get(a) == "done"],
            "message": f"change {cid}: complete"}


def next_phase(state: dict | None) -> dict:
    """Describe where a (re)invocation should continue."""
    if state is None:
        return {"state": "none", "phase": PHASE_IDS[0], "name": PHASE_NAMES[PHASE_IDS[0]],
                "status": "pending", "blocker": None, "done": [], "pending_repos": [],
                "message": f"no factory state; start at phase 0 ({PHASE_NAMES['0']})"}
    done = [p["id"] for p in state["phases"] if p["status"] in FINISHED]
    order = {pid: i for i, pid in enumerate(PHASE_IDS)}
    for p in sorted(state["phases"], key=lambda p: order.get(p["id"], 99)):
        if p["status"] in FINISHED:
            continue
        stages = PHASE_STAGES.get(p["id"], ())
        pend = sorted({n for s in stages for n in pending_repos(state, s)})
        msg = f"resuming at phase {p['id']} ({p.get('name') or PHASE_NAMES.get(p['id'], '')})"
        if p["status"] == "blocked":
            msg += f", blocked: {p.get('blocker')}"
        if stages:
            msg += f", repos {','.join(pend)} pending" if pend else ", no repos pending"
        return {"state": "resume", "phase": p["id"], "name": p.get("name"), "status": p["status"],
                "blocker": p.get("blocker"), "done": done, "pending_repos": pend, "message": msg}
    return {"state": "complete", "phase": None, "name": None, "status": "done", "blocker": None,
            "done": done, "pending_repos": [], "message": "all factory phases are done"}


def summary(state: dict | None) -> str:
    if state is None:
        return next_phase(None)["message"]
    lines = [f"factory state v{state['version']} org={state.get('org')} started {state.get('started_at')}"]
    if state.get("recovered_from"):
        lines.append(f"  WARNING: recovered from corrupt file {state['recovered_from']}; re-verify done work")
    for p in state["phases"]:
        extra = f" - blocker: {p['blocker']}" if p.get("blocker") else ""
        outs = ", ".join(f"{k}={v}" for k, v in (p.get("outputs") or {}).items())
        lines.append(f"  phase {p['id']:<2} {p.get('name', ''):<16} {p['status']:<11}"
                     f"{(' [' + outs + ']') if outs else ''}{extra}")
    if state["repos"]:
        lines.append("  repos:          " + "  ".join(f"{s:<11}" for s in STAGES))
        for name in sorted(state["repos"]):
            st = state["repos"][name]["stages"]
            lines.append(f"    {name:<14}" + "  ".join(f"{st.get(s, 'pending'):<11}" for s in STAGES))
    lines.append(next_phase(state)["message"])
    return "\n".join(lines)


def get_path(state: dict | None, dotted: str | None):
    if state is None:
        raise StateError("no state file; run `state.py init` first")
    cur = state
    for part in (dotted.split(".") if dotted else []):
        if isinstance(cur, list):
            match = [p for p in cur if isinstance(p, dict) and p.get("id") == part]
            if not match:
                raise StateError(f"no element with id {part!r} in {dotted!r}")
            cur = match[0]
        elif isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            raise StateError(f"no key {part!r} in {dotted!r}")
    return cur


# --------------------------------------------------------------------------- cli

def _kv(items: list[str] | None) -> dict:
    out = {}
    for item in items or []:
        if "=" not in item:
            raise StateError(f"--output expects k=v, got {item!r}")
        k, v = item.split("=", 1)
        out[k] = v
    return out


def _read_only(path: Path) -> dict | None:
    # Reads also take the lock so they never observe (or race with) a recovery.
    with locked(path):
        return load(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="factory resumable state (.agents/factory-state.json)")
    ap.add_argument("--file", help="state file (default: $FACTORY_STATE or ./.agents/factory-state.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init"); p.add_argument("--org"); p.add_argument("--force", action="store_true")
    p = sub.add_parser("get"); p.add_argument("path", nargs="?")
    p = sub.add_parser("next"); p.add_argument("--json", action="store_true")
    sub.add_parser("summary")
    p = sub.add_parser("set-phase")
    p.add_argument("id"); p.add_argument("status")
    p.add_argument("--output", action="append", metavar="K=V")
    p.add_argument("--blocker"); p.add_argument("--force", action="store_true")
    p = sub.add_parser("set-repo")
    p.add_argument("name"); p.add_argument("stage"); p.add_argument("status")
    p.add_argument("--path", dest="repo_path"); p.add_argument("--url"); p.add_argument("--force", action="store_true")
    p = sub.add_parser("pending-repos"); p.add_argument("stage"); p.add_argument("--ready", action="store_true")
    p = sub.add_parser("set"); p.add_argument("key"); p.add_argument("value")
    p = sub.add_parser("set-routine"); p.add_argument("id")
    p.add_argument("--automation-id"); p.add_argument("--cursor")
    p = sub.add_parser("set-change")
    p.add_argument("id"); p.add_argument("step"); p.add_argument("status")
    for f in CHANGE_FIELDS:
        p.add_argument("--" + f.replace("_", "-"), dest=f)
    p.add_argument("--blocker"); p.add_argument("--force", action="store_true")
    p = sub.add_parser("change-next"); p.add_argument("id"); p.add_argument("--json", action="store_true")

    a = ap.parse_args(argv)
    path = state_path(a.file)
    try:
        if a.cmd == "init":
            state, created = init(path, a.org, a.force)
            print(("initialized " if created else "exists, resuming ") + str(path), file=sys.stderr)
            print(next_phase(state)["message"])
        elif a.cmd == "get":
            print(json.dumps(get_path(_read_only(path), a.path), indent=2))
        elif a.cmd == "next":
            info = next_phase(_read_only(path))
            print(json.dumps(info) if a.json else info["message"])
        elif a.cmd == "summary":
            print(summary(_read_only(path)))
        elif a.cmd == "set-phase":
            print(json.dumps(set_phase(path, a.id, a.status, _kv(a.output), a.blocker, a.force)))
        elif a.cmd == "set-repo":
            print(json.dumps(set_repo(path, a.name, a.stage, a.status, a.repo_path, a.url, a.force)))
        elif a.cmd == "pending-repos":
            print(json.dumps(pending_repos(_read_only(path), a.stage, a.ready)))
        elif a.cmd == "set":
            set_key(path, a.key, a.value)
        elif a.cmd == "set-routine":
            print(json.dumps(set_routine(path, a.id, a.automation_id, a.cursor)))
        elif a.cmd == "set-change":
            fields = {f: getattr(a, f) for f in CHANGE_FIELDS}
            print(json.dumps(set_change(path, a.id, a.step, a.status, fields, a.blocker, a.force)))
        elif a.cmd == "change-next":
            info = change_next(_read_only(path), a.id)
            print(json.dumps(info) if a.json else info["message"])
    except StateError as e:
        print(f"state.py: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
