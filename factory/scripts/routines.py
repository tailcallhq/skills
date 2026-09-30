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
  routines.py drift --infra infra.json [--kb DIR]
      infra-drift routine: compare live infra (infra_graph.py output) with the
      KB's connections.md. Read-only; prints {live_only, kb_only, unmapped,
      intended_missing, counts}.

The prompt text lives in references/routines.md as fenced ```text <block>
code blocks: `preamble` + `<name>` + `rules`. This script only fills
{{placeholders}}; it never calls automation_create. Stdlib only.
Exit 0 ok, 1 refused/invalid, 2 usage.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE.parent
DOC = SKILL_DIR / "references" / "routines.md"

# name -> metadata. Prompt text: DOC. Keep in sync with the catalog table (tested).
# Polling intake routines: defined as complete automation_create YAML in
# references/triggers.md (they carry a cursor). Listed here; not rendered here.
TRIGGERS: dict[str, dict] = {
    "ci_failure_intake": {"title": "factory: CI failure intake", "cron": "*/30 * * * *",
                          "tier": "fast", "tokens": "~5k + 2k/failure",
                          "writes": "work-board ci-failure issues (see triggers.md)"},
    "alert_intake": {"title": "factory: alert intake", "cron": "*/15 * * * *",
                     "tier": "fast", "tokens": "~5k + 2k/alert",
                     "writes": "work-board alert issues (see triggers.md)"},
}

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
    "workflow-suggestions": {
        "title": "Factory: workflow suggestions",
        "cron": "0 7 * * 5",
        "tier": "intelligent",
        "tokens": "~80k",
        "writes": "up to 3 proposal issues (skill or routine) on the work board",
    },
    "infra-drift": {
        "title": "Factory: infra drift",
        "cron": "0 4 * * 3",
        "tier": "fast",
        "tokens": "~25k",
        "writes": "KB-board question issues; work-board infra-change issues (never applies)",
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
    if name in TRIGGERS:
        raise RoutineError(f"{name!r} is defined as YAML in references/triggers.md; copy it from there")
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
    if name in TRIGGERS:
        raise RoutineError(f"{name!r} is defined as YAML in references/triggers.md; copy it from there")
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


# --------------------------------------------------------------------------- drift

# resources that run code and so should map to a repo (infra_graph.py kinds)
WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "CronJob", "Job",
                  "ecs-service", "lambda", "aws_ecs_service", "aws_lambda_function"}

def _key(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:10]


def drift(infra_path: Path, kb_dir: Path) -> dict:
    """Live infra edges vs connections.md, at the (from, to) system level.

    - live_only: a live edge between two known systems (or a system and
      external:<host>) with no KB row in either protocol -> `question`.
    - kb_only: a KB row whose source is `infra:<p>:...` for a platform read in
      this run, with no live edge any more -> `question` (maybe removed) or a
      human-filed `infra-change`.
    - unmapped: live workloads that map to no repo/system -> `question`.
    - intended_missing: a `source: user` KB row (a human-stated intended
      state) between two systems deployed on a platform read in this run, with
      no live edge -> `infra-change` issue for a human. Never planned/applied.
    Never writes; keys are stable hashes for issue markers.
    """
    import kb as K  # same directory; reuse the KB parsers

    try:
        infra = json.loads(Path(infra_path).read_text())
    except (OSError, ValueError) as e:
        raise RoutineError(f"cannot read infra {infra_path}: {e}")
    if not (Path(kb_dir) / "systems").is_dir():
        raise RoutineError(f"{kb_dir} is not a knowledge base (no systems/)")
    platforms = sorted((infra.get("platforms") or {}).keys()) or sorted(
        {r.get("platform") for r in infra.get("resources", []) if r.get("platform")})
    owner: dict[str, str] = {}
    for name, doc in K.iter_systems(Path(kb_dir)):
        for r in (doc.meta or {}).get("repos") or []:
            owner.setdefault(r, name)
    systems = set(owner.values()) | {n for n, _ in K.iter_systems(Path(kb_dir))}

    def sys_of(node: str) -> str | None:
        if node.startswith("external:"):
            return node
        if node in owner:
            return owner[node]
        if "/" in node and not node.startswith("infra:"):
            slug = K.repo_slug(node)
            return slug if slug in systems else None
        return None

    conns = K.Connections.load(Path(kb_dir))
    kb_pairs = {(r["from"], r["to"]) for r in conns.rows}
    live_pairs: dict[tuple, dict] = {}
    for e in infra.get("edges", []):
        a, b = sys_of(e["from"]), sys_of(e["to"])
        if a and b and a != b:
            live_pairs.setdefault((a, b), e)
    live_only = [
        {"key": _key("live", a, b), "from": a, "to": b, "kind": e.get("kind"), "protocol": e.get("protocol"),
         "evidence": e.get("evidence"), "source": e.get("source")}
        for (a, b), e in sorted(live_pairs.items()) if (a, b) not in kb_pairs
    ]
    deployed = {sys_of(r["repo"]) for r in infra.get("resources", []) if r.get("repo")} - {None}
    intended_missing = [
        {"key": _key("intended", r["from"], r["to"], r["protocol"]), "from": r["from"], "to": r["to"],
         "protocol": r["protocol"], "source": r["source"], "verified": r["verified"]}
        for r in sorted(conns.rows, key=lambda r: (r["from"], r["to"], r["protocol"]))
        if r["source"] == "user" and (r["from"], r["to"]) not in live_pairs
        and all(n in deployed or n.startswith("external:") for n in (r["from"], r["to"]))
        and not all(n.startswith("external:") for n in (r["from"], r["to"]))
    ]
    kb_only = [
        {"key": _key("kb", r["from"], r["to"], r["protocol"]), "from": r["from"], "to": r["to"],
         "protocol": r["protocol"], "source": r["source"], "verified": r["verified"]}
        for r in sorted(conns.rows, key=lambda r: (r["from"], r["to"], r["protocol"]))
        if any(r["source"].startswith(f"infra:{p}:") for p in platforms) and (r["from"], r["to"]) not in live_pairs
    ]
    unmapped = [
        {"key": _key("unmapped", r["source"]), "resource": r["source"], "kind": r.get("kind"),
         "images": r.get("images", [])}
        for r in sorted(infra.get("resources", []), key=lambda r: r["source"])
        if not r.get("repo") and r.get("kind") in WORKLOAD_KINDS
    ]
    out = {"platforms": platforms, "live_only": live_only, "kb_only": kb_only, "unmapped": unmapped,
           "intended_missing": intended_missing,
           "counts": {"live_only": len(live_only), "kb_only": len(kb_only), "unmapped": len(unmapped),
                      "intended_missing": len(intended_missing)},
           "errors": infra.get("errors", [])}
    if find_credential(json.dumps(out)):
        raise RoutineError("refusing: drift output contains a credential-like string")
    return out


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
    p = sub.add_parser("drift"); p.add_argument("--infra", required=True)
    p.add_argument("--kb", default=os.environ.get("KB_DIR") or str(Path.home() / "workspaces" / "knowledge"))
    a = ap.parse_args(argv)
    try:
        if a.cmd == "list":
            rows = [dict(name=n, **m) for n, m in {**CATALOG, **TRIGGERS}.items()]
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
        elif a.cmd == "drift":
            print(json.dumps(drift(Path(a.infra), Path(a.kb)), indent=2))
    except RoutineError as e:
        print(f"routines.py: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
