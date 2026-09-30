#!/usr/bin/env python3
"""factory routines: catalog, prompt rendering and cron validation (phase 6).

Usage:
  routines.py list [--json]
      The catalog (name, cron, tier, est. tokens, writes).
  routines.py render NAME --state PATH [--cron EXPR] [--timezone TZ]
                     [--skill-dir DIR] [--workspace DIR]
      Print the `automation_create` input for NAME: {name, prompt, trigger,
      overlap_policy}. Ids (org, KB repo/checkout/board, work board) are filled
      from the state file; refuses (exit 1) if any is missing.
  routines.py cron-check NAME [--cron EXPR]
      Validate NAME's default cron (or EXPR) as a five-field cron expression.

The prompt text lives in references/routines.md as fenced ```text <block>
code blocks: `preamble` + `<name>` + `rules`. This script only fills
{{placeholders}}; it never calls automation_create. Stdlib only.
Exit 0 ok, 1 refused/invalid, 2 usage.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE.parent
DOC = SKILL_DIR / "references" / "routines.md"

# name -> metadata. Prompt text: DOC. Keep in sync with the catalog table (tested).
CATALOG: dict[str, dict] = {
    "kb-refresh": {
        "title": "Factory: KB refresh",
        "cron": "0 5 * * 1",
        "tier": "fast",
        "tokens": "~30k",
        "writes": "one KB PR; KB-board issues (questions, stale facts)",
    },
    "repo-health": {
        "title": "Factory: repo health",
        "cron": "0 6 * * 1",
        "tier": "fast",
        "tokens": "~6k/repo + 10k",
        "writes": "work-board issues, one per repo with findings",
    },
    "board-triage": {
        "title": "Factory: board triage",
        "cron": "30 2 * * *",
        "tier": "fast",
        "tokens": "~15k",
        "writes": "one triage issue per board, updated in place",
    },
}

PLACEHOLDER = re.compile(r"\{\{([a-z_]+)\}\}")
BLOCK = re.compile(r"^```text ([a-z0-9-]+)\n(.*?)^```", re.S | re.M)

# Same families kb.py refuses, plus generic high-entropy bearer-ish strings.
CREDENTIAL_PATTERNS = [
    re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*[A-Za-z0-9/+_\-]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@"),  # url with user:pass
]


class RoutineError(Exception):
    pass


# --------------------------------------------------------------------------- cron

_FIELDS = [  # name, lo, hi, names
    ("minute", 0, 59, None),
    ("hour", 0, 23, None),
    ("day-of-month", 1, 31, None),
    ("month", 1, 12, ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]),
    ("day-of-week", 0, 7, ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]),
]


def _cron_value(tok: str, lo: int, hi: int, names, field: str) -> int:
    if names and tok.lower() in names:
        return names.index(tok.lower()) + (1 if field == "month" else 0)
    if not tok.isdigit():
        raise RoutineError(f"{field}: {tok!r} is not a number")
    v = int(tok)
    if not lo <= v <= hi:
        raise RoutineError(f"{field}: {v} out of range {lo}-{hi}")
    return v


def check_cron(expr: str) -> list[str]:
    """Validate a five-field cron (no @macros, no seconds). Returns the fields."""
    parts = expr.split()
    if len(parts) != 5:
        raise RoutineError(f"cron must have 5 fields (minute hour dom month dow), got {len(parts)}: {expr!r}")
    for part, (field, lo, hi, names) in zip(parts, _FIELDS):
        for item in part.split(","):
            if not item:
                raise RoutineError(f"{field}: empty list item in {part!r}")
            rng, _, step = item.partition("/")
            if _:
                if not step.isdigit() or int(step) < 1:
                    raise RoutineError(f"{field}: bad step {step!r}")
            if rng == "*":
                continue
            a, dash, b = rng.partition("-")
            va = _cron_value(a, lo, hi, names, field)
            if dash:
                vb = _cron_value(b, lo, hi, names, field)
                if vb < va:
                    raise RoutineError(f"{field}: range {rng!r} is reversed")
    return parts


# --------------------------------------------------------------------------- prompts

def load_blocks(doc: Path = DOC) -> dict[str, str]:
    blocks = {m.group(1): m.group(2).strip() for m in BLOCK.finditer(doc.read_text())}
    for need in ("preamble", "rules"):
        if need not in blocks:
            raise RoutineError(f"{doc}: missing ```text {need} block")
    return blocks


def raw_prompt(name: str, blocks: dict[str, str] | None = None) -> str:
    if name not in CATALOG:
        raise RoutineError(f"unknown routine {name!r}; one of: {', '.join(CATALOG)}")
    blocks = blocks or load_blocks()
    if name not in blocks:
        raise RoutineError(f"{DOC}: missing ```text {name} block")
    return "\n\n".join([blocks["preamble"], blocks[name], blocks["rules"]])


def find_credential(text: str) -> str | None:
    for pat in CREDENTIAL_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(0)[:12] + "..."
    return None


def ids_from_state(state: dict) -> dict[str, str]:
    """Required ids; raises naming every missing one."""
    kb = state.get("kb") or {}
    board = None
    for ph in state.get("phases") or []:
        if ph.get("id") == "5":
            board = (ph.get("outputs") or {}).get("project_id")
    kb_url = kb.get("url") or ""
    m = re.match(r"^(?:https?://github\.com/|git@github\.com:)?([^/\s]+/[^/\s]+?)(?:\.git)?/?$", kb_url)
    vals = {
        "org": state.get("org"),
        "kb_repo": m.group(1) if m else None,
        "kb_path": kb.get("path"),
        "kb_project_id": kb.get("project_id"),
        "board_project_id": board,
    }
    where = {
        "org": "org (state.py init --org)",
        "kb_repo": "kb.url (phase 1: state.py set kb.url)",
        "kb_path": "kb.path (phase 1: state.py set kb.path)",
        "kb_project_id": "kb.project_id (phase 1: state.py set kb.project_id)",
        "board_project_id": "phases[5].outputs.project_id (phase 5: set-phase 5 done --output project_id=...)",
    }
    missing = [where[k] for k, v in vals.items() if not v]
    if missing:
        raise RoutineError("state is missing: " + "; ".join(missing))
    return vals  # type: ignore[return-value]


def render(name: str, state_path: Path, cron: str | None = None, timezone: str | None = None,
           skill_dir: str | None = None, workspace: str | None = None) -> dict:
    if name not in CATALOG:
        raise RoutineError(f"unknown routine {name!r}; one of: {', '.join(CATALOG)}")
    try:
        state = json.loads(Path(state_path).read_text())
    except (OSError, ValueError) as e:
        raise RoutineError(f"cannot read state {state_path}: {e}")
    ids = ids_from_state(state)
    state_abs = Path(state_path).resolve()
    ws = workspace or str(state_abs.parent.parent if state_abs.parent.name == ".agents" else state_abs.parent)
    meta = CATALOG[name]
    values = dict(ids, name=name, tier=meta["tier"], state_path=str(state_abs),
                  skill_dir=skill_dir or str(SKILL_DIR), workspace=ws)
    for k, v in values.items():
        if find_credential(str(v)):
            raise RoutineError(f"refusing: value for {k} looks like a credential")
    text = raw_prompt(name)
    unknown = sorted(set(PLACEHOLDER.findall(text)) - set(values))
    if unknown:
        raise RoutineError(f"prompt has unknown placeholders: {', '.join(unknown)}")
    text = PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), text)
    if PLACEHOLDER.search(text):
        raise RoutineError("prompt still has unfilled placeholders")
    cred = find_credential(text)
    if cred:
        raise RoutineError(f"refusing: rendered prompt contains a credential-like string ({cred})")
    expr = cron or meta["cron"]
    check_cron(expr)
    trigger = {"cron": expr}
    if timezone:
        trigger["timezone"] = timezone
    return {"name": meta["title"], "prompt": text, "trigger": trigger, "overlap_policy": "skip"}


# --------------------------------------------------------------------------- cli

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="factory routines catalog / prompt renderer (phase 6)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("render"); p.add_argument("name"); p.add_argument("--state", required=True)
    p.add_argument("--cron"); p.add_argument("--timezone")
    p.add_argument("--skill-dir", help="factory skill dir the routine runs scripts from (default: this one)")
    p.add_argument("--workspace", help="workspace root (default: parent of the state's .agents dir)")
    p = sub.add_parser("cron-check"); p.add_argument("name"); p.add_argument("--cron")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "list":
            rows = [dict(name=n, **m) for n, m in CATALOG.items()]
            if a.json:
                print(json.dumps(rows, indent=2))
            else:
                for r in rows:
                    print(f"{r['name']:<22} {r['cron']:<14} {r['tier']:<12} {r['tokens']:<8} {r['writes']}")
        elif a.cmd == "render":
            print(json.dumps(render(a.name, Path(a.state), a.cron, a.timezone, a.skill_dir, a.workspace), indent=2))
        elif a.cmd == "cron-check":
            if a.name not in CATALOG:
                raise RoutineError(f"unknown routine {a.name!r}; one of: {', '.join(CATALOG)}")
            expr = a.cron or CATALOG[a.name]["cron"]
            print(json.dumps({"name": a.name, "cron": expr, "fields": check_cron(expr), "ok": True}))
    except RoutineError as e:
        print(f"routines.py: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
