#!/usr/bin/env python3
"""kb.py - maintain the factory knowledge base (a living HLD of the user's systems).

The knowledge base is a private GitHub repository (`<org>/knowledge`), checked out
at ~/workspaces/knowledge. See references/knowledge-base.md for the layout and the
provenance rules this script enforces.

Stdlib only; shells out to `git` and `gh`.

Every mutating command:
  - refuses on a dirty checkout unless --force,
  - refuses any value that looks like a secret (always, --force does not help),
  - never rewrites an existing `source: user` fact,
  - commits its change locally (never pushes; `propose` pushes a branch + opens a PR).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_KB = os.path.expanduser("~/workspaces/knowledge")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

# --------------------------------------------------------------------------- errors


class KBError(Exception):
    """User-facing refusal; exit code attached."""

    def __init__(self, msg: str, code: int = 1):
        super().__init__(msg)
        self.code = code


def today() -> str:
    return os.environ.get("KB_TODAY") or _dt.date.today().isoformat()


# --------------------------------------------------------------------------- secrets

SECRET_PATTERNS = [
    ("aws-access-key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("github-token", re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("api-secret-key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("password-assignment", re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)\s*[=:]\s*\S+")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("service-account-json", re.compile(r"\"type\"\s*:\s*\"service_account\"|\"private_key(_id)?\"\s*:")),
]


def find_secret(text: str) -> str | None:
    for name, pat in SECRET_PATTERNS:
        if pat.search(text or ""):
            return name
    return None


def refuse_secrets(*values) -> None:
    for v in values:
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            refuse_secrets(*v)
            continue
        if isinstance(v, dict):
            refuse_secrets(*v.values())
            continue
        hit = find_secret(str(v))
        if hit:
            raise KBError(f"refused: value looks like a secret ({hit}); the knowledge base never stores credentials", 3)


# --------------------------------------------------------------------------- tiny YAML subset
# Enough for frontmatter we write ourselves: mappings, block lists (of scalars or
# mappings), flow lists of scalars, double-quoted scalars, `# comments`.

_PLAIN_OK = re.compile(r"^[A-Za-z0-9_./@+\-][A-Za-z0-9_./@+\-: ()]*$")


def _scalar_out(v) -> str:
    if v is None:
        return "null"
    s = str(v)
    if (_PLAIN_OK.match(s) and not s.endswith((" ", ":")) and ": " not in s
            and s not in ("null", "true", "false", "~")):
        return s
    return json.dumps(s)


def yaml_dump(obj: dict, indent: int = 0) -> str:
    pad = " " * indent
    out = []
    for k, v in obj.items():
        if isinstance(v, dict):
            if not v:
                out.append(f"{pad}{k}: {{}}")
            else:
                out.append(f"{pad}{k}:")
                out.append(yaml_dump(v, indent + 2))
        elif isinstance(v, list):
            if not v:
                out.append(f"{pad}{k}: []")
            elif all(not isinstance(x, (dict, list)) for x in v):
                out.append(f"{pad}{k}: [" + ", ".join(_scalar_out(x) for x in v) + "]")
            else:
                out.append(f"{pad}{k}:")
                for item in v:
                    if isinstance(item, dict):
                        body = yaml_dump(item, indent + 4).split("\n")
                        first = body[0][indent + 4:]
                        out.append(f"{pad}  - {first}")
                        out.extend(body[1:])
                    else:
                        out.append(f"{pad}  - {_scalar_out(item)}")
        else:
            out.append(f"{pad}{k}: {_scalar_out(v)}")
    return "\n".join(out)


def _strip_comment(s: str) -> str:
    q = None
    for i, c in enumerate(s):
        if q:
            if c == "\\" and q == '"':
                continue
            if c == q:
                q = None
        elif c in "\"'":
            q = c
        elif c == "#" and (i == 0 or s[i - 1] in " \t"):
            return s[:i].rstrip()
    return s.strip()


def _split_flow(s: str) -> list[str]:
    items, cur, q = [], "", None
    for c in s:
        if q:
            cur += c
            if c == q:
                q = None
        elif c in "\"'":
            q = c
            cur += c
        elif c == ",":
            items.append(cur.strip())
            cur = ""
        else:
            cur += c
    if cur.strip():
        items.append(cur.strip())
    return items


def _scalar_in(s: str):
    s = _strip_comment(s)
    if s == "" or s in ("null", "~"):
        return None
    if s.startswith("[") and s.endswith("]"):
        return [_scalar_in(x) for x in _split_flow(s[1:-1])]
    if s == "{}":
        return {}
    if s.startswith('"'):
        return json.loads(s)
    if s.startswith("'") and s.endswith("'"):
        return s[1:-1].replace("''", "'")
    return s


_KEY_RE = re.compile(r"^([A-Za-z0-9_.\-]+):(?:\s+(.*))?$")


def yaml_load(text: str) -> dict:
    lines = []
    for raw in text.split("\n"):
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        ind = len(raw) - len(raw.lstrip(" "))
        lines.append([ind, raw.strip()])
    obj, _ = _parse_block(lines, 0, lines[0][0] if lines else 0)
    return obj or {}


def _parse_block(lines, i, indent):
    if i < len(lines) and lines[i][1].startswith("- ") or (i < len(lines) and lines[i][1] == "-"):
        return _parse_list(lines, i, indent)
    return _parse_map(lines, i, indent)


def _parse_map(lines, i, indent):
    out = {}
    while i < len(lines) and lines[i][0] == indent and not lines[i][1].startswith("- "):
        m = _KEY_RE.match(lines[i][1])
        if not m:
            raise KBError(f"cannot parse frontmatter line: {lines[i][1]!r}")
        key, rest = m.group(1), (m.group(2) or "")
        i += 1
        if _strip_comment(rest) == "":
            if i < len(lines) and (lines[i][0] > indent or (lines[i][0] == indent and lines[i][1].startswith("- "))):
                out[key], i = _parse_block(lines, i, lines[i][0])
            else:
                out[key] = None
        else:
            out[key] = _scalar_in(rest)
    return out, i


def _parse_list(lines, i, indent):
    out = []
    while i < len(lines) and lines[i][0] == indent and (lines[i][1].startswith("- ") or lines[i][1] == "-"):
        rest = lines[i][1][2:].strip()
        if _KEY_RE.match(rest) and not rest.startswith(('"', "'")):
            lines[i] = [indent + 2, rest]
            item, i = _parse_map(lines, i, indent + 2)
            out.append(item)
        else:
            out.append(_scalar_in(rest))
            i += 1
    return out, i


# --------------------------------------------------------------------------- documents

FACT_RE = re.compile(
    r"^- \*\*(?P<key>[^*]+)\*\*: (?P<value>.*?) <!-- source: (?P<source>.+?); verified: (?P<verified>\S+) -->$"
)
QUESTION_RE = re.compile(r"^- \[(?P<done>[ x])\] (?P<text>.*?) <!-- question: (?P<id>[0-9a-f]+); raised: (?P<raised>\S+) -->$")

SECTIONS = ["Purpose", "Interfaces", "Dependencies", "Build, run, test", "Open questions"]


def source_rank(source: str) -> int:
    s = (source or "").strip()
    if s == "user":
        return 3
    if s.startswith("infra:"):
        return 2
    return 1


def validate_source(source: str) -> str:
    s = (source or "").strip()
    if not s:
        raise KBError("every fact needs --source (repo path | gh api ... | infra:<platform>:<resource> | user)")
    if s.startswith("infra:") and len(s.split(":", 2)) < 3:
        raise KBError(f"infra source must be infra:<platform>:<resource>, got {s!r}")
    if "-->" in s or "\n" in s or ";" in s:
        raise KBError(f"invalid source {s!r}")
    return s


def fact_line(key: str, value: str, source: str, verified: str) -> str:
    for part, label in ((key, "key"), (value, "value")):
        if "\n" in str(part) or "-->" in str(part) or "<!--" in str(part):
            raise KBError(f"invalid {label} {part!r}")
    if "*" in key:
        raise KBError(f"invalid key {key!r}")
    return f"- **{key}**: {value} <!-- source: {source}; verified: {verified} -->"


def question_id(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:10]


def question_line(text: str) -> str:
    return f"- [ ] {text} <!-- question: {question_id(text)}; raised: {today()} -->"


class Doc:
    """A markdown file with optional YAML frontmatter and `## ` sections."""

    def __init__(self, meta: dict | None, title: str, sections: dict[str, list[str]], order: list[str]):
        self.meta = meta
        self.title = title
        self.sections = sections
        self.order = order

    @classmethod
    def parse(cls, text: str) -> "Doc":
        meta = None
        if text.startswith("---\n"):
            end = text.find("\n---\n", 4)
            if end < 0:
                raise KBError("unterminated frontmatter")
            meta = yaml_load(text[4:end])
            text = text[end + 5:]
        title, sections, order, cur = "", {}, [], None
        for line in text.split("\n"):
            if line.startswith("## "):
                cur = line[3:].strip()
                sections[cur] = []
                order.append(cur)
            elif cur is None:
                if line.startswith("# ") and not title:
                    title = line[2:].strip()
            else:
                sections[cur].append(line)
        for k in sections:
            while sections[k] and not sections[k][-1].strip():
                sections[k].pop()
            while sections[k] and not sections[k][0].strip():
                sections[k].pop(0)
        return cls(meta, title, sections, order)

    def render(self) -> str:
        out = []
        if self.meta is not None:
            out += ["---", yaml_dump(self.meta), "---", ""]
        out += [f"# {self.title}", ""]
        for name in self.order:
            out.append(f"## {name}")
            out.append("")
            if self.sections[name]:
                out += self.sections[name]
                out.append("")
        return "\n".join(out).rstrip("\n") + "\n"

    def section(self, name: str) -> list[str]:
        if name not in self.sections:
            self.sections[name] = []
            # keep Open questions last
            if "Open questions" in self.order and name != "Open questions":
                self.order.insert(self.order.index("Open questions"), name)
            else:
                self.order.append(name)
        return self.sections[name]

    def facts(self, section: str | None = None):
        for name in self.order:
            if section and name != section:
                continue
            for idx, line in enumerate(self.sections[name]):
                m = FACT_RE.match(line)
                if m:
                    yield name, idx, m.groupdict()

    def questions(self):
        for line in self.sections.get("Open questions", []):
            m = QUESTION_RE.match(line)
            if m:
                yield m.groupdict()

    def add_question(self, text: str) -> bool:
        qid = question_id(text)
        if any(q["id"] == qid for q in self.questions()):
            return False
        self.section("Open questions").append(question_line(text))
        return True


def upsert_fact(doc: Doc, section: str, key: str, value: str, source: str, *, context: str) -> str:
    """Add a fact; never overwrite a different value. Returns added|refreshed|conflict|same."""
    source = validate_source(source)
    existing = [f for _, _, f in doc.facts() if f["key"] == key]
    for f in existing:
        if f["value"] == value:
            if f["source"] == source and f["source"] != "user" and f["verified"] != "pending":
                # re-seen from the same non-user source: refresh verification date
                lines = doc.sections[_section_of(doc, f)]
                i = lines.index(fact_line(f["key"], f["value"], f["source"], f["verified"]))
                lines[i] = fact_line(key, value, source, today() if source_rank(source) > 1 else f["verified"])
                return "refreshed"
            return "same"
    if existing:
        best = max(existing, key=lambda f: source_rank(f["source"]))
        winner = best if source_rank(best["source"]) >= source_rank(source) else {"value": value, "source": source}
        doc.add_question(
            f"{context} `{key}`: `{best['value']}` ({best['source']}) vs `{value}` ({source}); "
            f"precedence keeps `{winner['value']}` from {winner['source']}. Confirm?"
        )
        return "conflict"
    verified = "pending" if source_rank(source) == 1 and source.startswith("pending:") else today()
    doc.section(section).append(fact_line(key, value, source, verified))
    return "added"


def _section_of(doc: Doc, fact: dict) -> str:
    for name, _, f in doc.facts():
        if f == fact:
            return name
    raise KeyError(fact)


# --------------------------------------------------------------------------- git


def run(cmd: list[str], cwd: str | Path | None = None, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, cwd=cwd, text=True, capture_output=capture)
    if check and p.returncode != 0:
        raise KBError(f"`{' '.join(cmd)}` failed ({p.returncode}): {(p.stderr or p.stdout).strip()}")
    return p


def git(kb: Path, *args, check=True) -> str:
    return run(["git", *args], cwd=kb, check=check).stdout.strip()


def is_git(path: Path) -> bool:
    return (path / ".git").exists()


def ensure_clean(kb: Path, force: bool) -> None:
    if not is_git(kb):
        raise KBError(f"{kb} is not a knowledge-base checkout (run `kb.py init` or `kb.py ensure-repo`)")
    if not (kb / "systems").is_dir():
        raise KBError(f"{kb} has no systems/ directory (run `kb.py init {kb}`)")
    dirty = git(kb, "status", "--porcelain")
    if dirty and not force:
        raise KBError(f"refused: {kb} has uncommitted changes (commit/stash them or pass --force):\n{dirty}", 4)


def commit(kb: Path, message: str) -> bool:
    git(kb, "add", "-A")
    if not git(kb, "status", "--porcelain"):
        return False
    ident = []
    if not run(["git", "config", "user.email"], cwd=kb, check=False).stdout.strip():
        ident = ["-c", "user.name=factory", "-c", "user.email=factory@localhost"]
    git(kb, *ident, "commit", "-q", "-m", message)
    return True


def write_text(path: Path, text: str) -> None:
    hit = find_secret(text)
    if hit:
        raise KBError(f"refused: {path.name} would contain a secret ({hit})", 3)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


# --------------------------------------------------------------------------- skeleton

README = """# Knowledge base

A living high-level design (HLD) of our systems and how they connect. Maintained by
the `factory` skill (`kb.py`) and by humans. **Private repository: never make it public.**

## Layout

- `index.md` - generated by `kb.py index`; do not edit by hand.
- `systems/<system>.md` - one file per system: YAML frontmatter (`name`, `repos`,
  `owners`, `runtime` incl. `environments`, `monitoring`, `status`, `verified`) and
  sections Purpose, Interfaces, Dependencies, Build/run/test, Open questions.
- `connections.md` - system-to-system edges (table + generated mermaid graph).
- `decisions.md` - dated decisions, including infra changes that were applied.

## Provenance rules

- Every fact line ends with `<!-- source: <src>; verified: <date|pending> -->`, where
  `<src>` is a repo path (`owner/repo:path`), `gh api ...`, `infra:<platform>:<resource>`
  or `user`.
- Precedence: `user` > `infra:*` > repo manifests/code. A disagreement is never an
  overwrite: it becomes an entry under "Open questions" (and a board issue).
- Automation never edits or deletes a `source: user` line, and only adds candidate
  rows (`verified: pending`) until a human confirms them.
- All changes arrive as pull requests from `factory/*` branches; humans merge.
- Hostnames and topology are fine here. Secrets are not: tokens, keys, passwords and
  service-account JSON are refused by `kb.py` and must never be committed.
"""

CONNECTIONS_HEAD = """# Connections

System-to-system edges. `verified: pending` rows are candidates found in code; confirm
or correct them (set `source` to `user`). The graph below is generated by `kb.py index`.

"""
CONN_HEADER = "| from -> to | protocol | auth | source | verified |"
CONN_SEP = "|---|---|---|---|---|"
MERMAID_BEGIN = "<!-- mermaid:begin (generated) -->"
MERMAID_END = "<!-- mermaid:end -->"

DECISIONS = """# Decisions

Newest first. One entry per decision: date, decision, context, who approved, links
(PRs, board issues). Infra changes record the plan, the inverse plan and both approvals.
"""


def cmd_init(args) -> dict:
    kb = Path(args.path).expanduser().resolve()
    if kb.exists():
        extra = [p.name for p in kb.iterdir() if p.name != ".git"]
        if extra and not args.force:
            raise KBError(f"refused: {kb} is not empty ({', '.join(sorted(extra)[:5])}); pass --force to add the skeleton", 4)
    kb.mkdir(parents=True, exist_ok=True)
    if not is_git(kb):
        run(["git", "init", "-q", "-b", "main"], cwd=kb)
    created = []
    files = {
        "README.md": README,
        "connections.md": CONNECTIONS_HEAD + CONN_HEADER + "\n" + CONN_SEP + "\n\n## Graph\n\n" + MERMAID_BEGIN + "\n" + MERMAID_END + "\n\n## Open questions\n",
        "decisions.md": DECISIONS,
        "systems/.gitkeep": "",
    }
    for rel, text in files.items():
        p = kb / rel
        if not p.exists():
            write_text(p, text)
            created.append(rel)
    write_index(kb)
    committed = commit(kb, "kb: initialise knowledge base skeleton")
    return {"path": str(kb), "created": created, "committed": committed}


# --------------------------------------------------------------------------- systems


def system_path(kb: Path, name: str) -> Path:
    if not SLUG_RE.match(name):
        raise KBError(f"system id must be a lowercase slug, got {name!r}")
    return kb / "systems" / f"{name}.md"


def new_system(name: str, status: str = "candidate") -> Doc:
    meta = {
        "name": name,
        "repos": [],
        "owners": [],
        "runtime": {"environments": []},
        "monitoring": [],
        "status": status,
        "verified": "pending",
    }
    return Doc(meta, name, {s: [] for s in SECTIONS}, list(SECTIONS))


def load_system(kb: Path, name: str) -> Doc | None:
    p = system_path(kb, name)
    return Doc.parse(p.read_text()) if p.exists() else None


def save_system(kb: Path, doc: Doc) -> None:
    write_text(system_path(kb, doc.meta["name"]), doc.render())


def _add_unique(lst: list, values) -> bool:
    changed = False
    for v in values or []:
        if v not in lst:
            lst.append(v)
            changed = True
    return changed


def cmd_add_system(args) -> dict:
    kb = kb_path(args)
    refuse_secrets(args.name, args.repo, args.owner, args.purpose, args.status, args.source, args.runtime)
    ensure_clean(kb, args.force)
    doc = load_system(kb, args.name)
    created = doc is None
    if created:
        doc = new_system(args.name, args.status or ("active" if args.source == "user" else "candidate"))
    meta = doc.meta
    meta.setdefault("repos", [])
    meta.setdefault("owners", [])
    _add_unique(meta["repos"], args.repo)
    _add_unique(meta["owners"], args.owner)
    if args.status and not created and args.status != meta.get("status"):
        if args.source == "user" or meta.get("status") in (None, "candidate"):
            meta["status"] = args.status
    if args.runtime:
        rt = meta.setdefault("runtime", {}) or {}
        meta["runtime"] = rt
        rt.setdefault("platform", args.runtime)
    results = {}
    if args.purpose:
        results["purpose"] = upsert_fact(doc, "Purpose", "purpose", args.purpose, args.source or "user", context=args.name)
    if args.source == "user":
        meta["verified"] = today()
    save_system(kb, doc)
    write_index(kb)
    committed = commit(kb, f"kb: {'add' if created else 'update'} system {args.name}")
    return {"system": args.name, "created": created, "facts": results, "committed": committed}


def require_system(kb: Path, name: str) -> Doc:
    doc = load_system(kb, name)
    if doc is None:
        raise KBError(f"no such system {name!r} (create it with `kb.py add-system {name}`)")
    return doc


ENV_NAMES = ("staging", "prod", "dev", "preview", "qa")


def cmd_add_env(args) -> dict:
    """runtime.environments[]: {name, id, platform?, url?, source, verified}; keyed by name."""
    kb = kb_path(args)
    refuse_secrets(args.system, args.name, args.id, args.platform, args.url, args.source)
    source = validate_source(args.source)
    ensure_clean(kb, args.force)
    doc = require_system(kb, args.system)
    rt = doc.meta.get("runtime") or {}
    doc.meta["runtime"] = rt
    envs = rt.get("environments") or []
    rt["environments"] = envs
    entry = {"name": args.name, "id": args.id}
    if args.platform:
        entry["platform"] = args.platform
    if args.url:
        entry["url"] = args.url
    entry["source"] = source
    entry["verified"] = today()
    result = "added"
    cur = next((e for e in envs if e.get("name") == args.name), None)
    if cur is None:
        envs.append(entry)
    else:
        diffs = [k for k in ("id", "platform", "url") if entry.get(k) and cur.get(k) and entry[k] != cur[k]]
        if diffs and source != "user":
            # Automation never overwrites: the disagreement becomes an open question.
            result = "conflict"
            keep = cur if source_rank(cur.get("source", "")) >= source_rank(source) else entry
            doc.add_question(
                f"{args.system} environment `{args.name}`: " + ", ".join(
                    f"{k} `{cur[k]}` ({cur.get('source')}) vs `{entry[k]}` ({source})" for k in diffs
                ) + f"; precedence keeps {keep['source']}. Confirm?"
            )
        elif cur.get("source") == "user" and source != "user":
            result = "same"  # never touch a user-sourced entry
        else:
            # A user statement is authoritative; otherwise only fill gaps / refresh.
            for k, v in entry.items():
                if source == "user" or not cur.get(k) or k == "verified":
                    cur[k] = v
            if source == "user":
                cur["source"] = "user"
            result = "updated" if diffs else "refreshed"
    save_system(kb, doc)
    write_index(kb)
    committed = commit(kb, f"kb: {args.system} environment {args.name}")
    return {"system": args.system, "environment": args.name, "result": result, "committed": committed}


MONITOR_KINDS = ("sentry", "datadog", "grafana", "prometheus", "pagerduty", "opentelemetry", "posthog")
MONITOR_KEYS = ("kind", "org", "project", "site", "url", "mcp", "source", "verified")


def cmd_add_monitor(args) -> dict:
    """Per references/monitoring.md "Knowledge base integration"; dedupe on (system, kind, project)."""
    kb = kb_path(args)
    if args.kind == "github-actions":
        raise KBError("github-actions is not a KB monitor: CI is recorded under the repo (see monitoring.md)")
    if args.kind not in MONITOR_KINDS:
        raise KBError(f"--kind must be one of {', '.join(MONITOR_KINDS)}")
    if not args.source:
        raise KBError("at least one --source <owner/repo>:<path> (or `user`) is required")
    refuse_secrets(args.system, args.project, args.org, args.site, args.url, args.mcp, args.source)
    for s in args.source:
        if s != "user" and not re.match(r"^[\w.\-]+/[\w.\-]+:\S+$", s):
            raise KBError(f"--source must be <owner/repo>:<path> or `user`, got {s!r}")
    ensure_clean(kb, args.force)
    doc = require_system(kb, args.system)
    mons = doc.meta.get("monitoring") or []
    doc.meta["monitoring"] = mons
    cur = next((m for m in mons if m.get("kind") == args.kind and m.get("project") == args.project), None)
    new = {"kind": args.kind, "org": args.org, "project": args.project, "site": args.site,
           "url": args.url, "mcp": args.mcp}
    if cur is None:
        cur = {}
        mons.append(cur)
        result = "added"
    else:
        result = "updated"
    for k, v in new.items():
        if v:
            cur[k] = v
    srcs = cur.get("source") or []
    if isinstance(srcs, str):
        srcs = [srcs]
    _add_unique(srcs, args.source)
    cur["source"] = srcs
    cur["verified"] = today()
    ordered = {k: cur[k] for k in MONITOR_KEYS if cur.get(k)}
    ordered.update({k: v for k, v in cur.items() if k not in ordered and v})
    mons[mons.index(cur)] = ordered
    save_system(kb, doc)
    write_index(kb)
    committed = commit(kb, f"kb: {args.system} monitor {args.kind}{'/' + args.project if args.project else ''}")
    return {"system": args.system, "monitor": ordered, "result": result, "committed": committed}


# --------------------------------------------------------------------------- index


def iter_systems(kb: Path):
    d = kb / "systems"
    if not d.is_dir():
        return
    for p in sorted(d.glob("*.md")):
        yield p.stem, Doc.parse(p.read_text())


def write_index(kb: Path) -> None:
    rows = []
    for name, doc in iter_systems(kb):
        m = doc.meta or {}
        purpose = next((f["value"] for _, _, f in doc.facts("Purpose")), "")
        nq = sum(1 for q in doc.questions() if q["done"] == " ")
        rows.append(
            f"| [{name}](systems/{name}.md) | {m.get('status') or ''} | {', '.join(m.get('repos') or [])} | "
            f"{', '.join(m.get('owners') or [])} | {purpose} | {m.get('verified') or ''} | {nq} |"
        )
    text = "# Index\n\n<!-- generated by kb.py index; do not edit -->\n\n"
    text += "| system | status | repos | owners | purpose | verified | open questions |\n|---|---|---|---|---|---|---|\n"
    text += "\n".join(rows) + ("\n" if rows else "")
    text += "\nSee [connections.md](connections.md) for the system graph and [decisions.md](decisions.md) for decisions.\n"
    write_text(kb / "index.md", text)


# --------------------------------------------------------------------------- cli


def kb_path(args) -> Path:
    return Path(args.kb or os.environ.get("KB_DIR") or DEFAULT_KB).expanduser().resolve()


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--kb", help=f"knowledge-base checkout (default $KB_DIR or {DEFAULT_KB})")
    common.add_argument("--force", action="store_true", help="write even if the checkout is dirty")

    p = argparse.ArgumentParser(prog="kb.py", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", parents=[common], help="create the skeleton + initial commit")
    s.add_argument("path")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("add-system", parents=[common], help="create or extend systems/<name>.md")
    s.add_argument("name")
    s.add_argument("--repo", action="append", default=[], help="owner/repo (repeatable)")
    s.add_argument("--owner", action="append", default=[], help="team or @handle (repeatable)")
    s.add_argument("--purpose")
    s.add_argument("--status", choices=["candidate", "active", "deprecated", "retired"])
    s.add_argument("--runtime", help="runtime platform, e.g. fly, k8s, lambda")
    s.add_argument("--source", default="user", help="provenance of --purpose (default: user)")
    s.set_defaults(func=cmd_add_system)

    s = sub.add_parser("add-env", parents=[common], help="record a runtime environment (staging/prod ids)")
    s.add_argument("system")
    s.add_argument("name", help="environment name, e.g. staging, prod")
    s.add_argument("--id", required=True, help="platform identifier (project/app/cluster/namespace)")
    s.add_argument("--platform", help="e.g. aws, gcp, fly, k8s, vercel")
    s.add_argument("--url", help="public or internal base URL (hostnames are fine; no credentials)")
    s.add_argument("--source", required=True, help="infra:<platform>:<resource> | owner/repo:path | user")
    s.set_defaults(func=cmd_add_env)

    s = sub.add_parser("add-monitor", parents=[common], help="record a monitoring system (references/monitoring.md)")
    s.add_argument("system")
    s.add_argument("--kind", required=True)
    s.add_argument("--project", help="vendor-side id: Sentry project, Datadog service, ...")
    s.add_argument("--org")
    s.add_argument("--site")
    s.add_argument("--url")
    s.add_argument("--mcp", help="mcp_add alias, only once connected")
    s.add_argument("--source", action="append", default=[], help="<owner/repo>:<path> or `user` (repeatable)")
    s.set_defaults(func=cmd_add_monitor)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        out = args.func(args)
    except KBError as e:
        print(f"kb.py: {e}", file=sys.stderr)
        return e.code
    if out is not None:
        print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
