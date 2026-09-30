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

    def __init__(self, meta: dict | None, title: str, sections: dict[str, list[str]], order: list[str],
                 preamble: list[str] | None = None):
        self.meta = meta
        self.title = title
        self.sections = sections
        self.order = order
        self.preamble = preamble or []

    @classmethod
    def parse(cls, text: str) -> "Doc":
        meta = None
        if text.startswith("---\n"):
            end = text.find("\n---\n", 4)
            if end < 0:
                raise KBError("unterminated frontmatter")
            meta = yaml_load(text[4:end])
            text = text[end + 5:]
        title, sections, order, cur, preamble = "", {}, [], None, []
        for line in text.split("\n"):
            if line.startswith("## "):
                cur = line[3:].strip()
                sections[cur] = []
                order.append(cur)
            elif cur is None:
                if line.startswith("# ") and not title:
                    title = line[2:].strip()
                elif title:
                    preamble.append(line)
            else:
                sections[cur].append(line)
        for lst in [preamble, *sections.values()]:
            while lst and not lst[-1].strip():
                lst.pop()
            while lst and not lst[0].strip():
                lst.pop(0)
        return cls(meta, title, sections, order, preamble)

    def render(self) -> str:
        out = []
        if self.meta is not None:
            out += ["---", yaml_dump(self.meta), "---", ""]
        out += [f"# {self.title}", ""]
        if self.preamble:
            out += self.preamble + [""]
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


def effective(facts: list[dict]) -> dict | None:
    """The fact that wins by precedence (user > infra:* > manifest); first line wins ties."""
    best = None
    for f in facts:
        if best is None or source_rank(f["source"]) > source_rank(best["source"]):
            best = f
    return best


def upsert_fact(doc: Doc, section: str, key: str, value: str, source: str, *, verified: str, context: str) -> str:
    """Add a fact line. Never rewrites or deletes an existing line with a different value,
    never touches a `source: user` line. Returns added|refreshed|same|conflict.

    - same key+value+source: refresh `verified` (dated, non-user lines only) -> refreshed|same
    - same key, different value: a question is raised; if the new source has strictly
      higher precedence its line is appended too (it becomes the effective value) ->
      conflict
    """
    source = validate_source(source)
    value = str(value)
    existing = [(name, idx, f) for name, idx, f in doc.facts() if f["key"] == key]
    for name, idx, f in existing:
        if f["value"] == value and f["source"] == source:
            if source != "user" and f["verified"] not in ("pending", verified) and verified != "pending":
                doc.sections[name][idx] = fact_line(key, value, source, verified)
                return "refreshed"
            return "same"
    if any(f["value"] == value for _, _, f in existing):
        return "same"  # already stated by another source
    own = [(n, i) for n, i, f in existing if f["source"] == source and source != "user"]
    others = [f for _, _, f in existing if f["source"] != source]
    if own and not others:
        # the same (non-user) source now says something else and nobody else has an
        # opinion: refresh that source's own line in place.
        n, i = own[0]
        doc.sections[n][i] = fact_line(key, value, source, verified)
        return "updated"
    if existing:
        best = effective([f for _, _, f in existing])
        higher = source_rank(source) > source_rank(best["source"])
        keep = (value, source) if higher else (best["value"], best["source"])
        doc.add_question(
            f"{context}: `{key}` is `{best['value']}` ({best['source']}) but `{value}` ({source}); "
            f"precedence keeps `{keep[0]}` ({keep[1]}). Confirm or correct."
        )
        if higher:
            doc.section(section).append(fact_line(key, value, source, verified))
        return "conflict"
    doc.section(section).append(fact_line(key, value, source, verified))
    return "added"


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

CONN_PREAMBLE = [
    "System-to-system edges. Rows with `verified: pending` are candidates found in code by",
    "`kb.py ingest`; confirm or correct them by setting `source` to `user` and a date in",
    "`verified`. Automation never edits or removes a `user` row. The graph is generated.",
]
CONN_HEADER = "| from -> to | protocol | auth | source | verified |"
CONN_SEP = "|---|---|---|---|---|"
MERMAID_BEGIN = "<!-- mermaid:begin (generated by kb.py; do not edit) -->"
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
        "connections.md": Connections([]).render(),
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
        results["purpose"] = upsert_fact(doc, "Purpose", "purpose", args.purpose, args.source or "user",
                                         verified=today(), context=args.name)
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


SECTION_ALIASES = {s.lower(): s for s in SECTIONS}
SECTION_ALIASES.update({"build": "Build, run, test", "run": "Build, run, test", "test": "Build, run, test",
                        "questions": "Open questions"})


def cmd_add_fact(args) -> dict:
    kb = kb_path(args)
    refuse_secrets(args.system, args.key, args.value, args.source)
    section = SECTION_ALIASES.get(args.section.lower())
    if not section or section == "Open questions":
        raise KBError(f"--section must be one of {', '.join(SECTIONS[:-1])}")
    ensure_clean(kb, args.force)
    doc = require_system(kb, args.system)
    verified = "pending" if args.pending else today()
    result = upsert_fact(doc, section, args.key, args.value, args.source, verified=verified, context=args.system)
    save_system(kb, doc)
    write_index(kb)
    committed = commit(kb, f"kb: {args.system} {args.key} ({args.source})")
    return {"system": args.system, "key": args.key, "result": result, "committed": committed}


# --------------------------------------------------------------------------- connections


def _cell(s) -> str:
    return str(s if s not in (None, "") else "?").replace("|", "\\|").replace("\n", " ")


class Connections:
    """connections.md: an `Edges` table, a generated mermaid `Graph`, `Open questions`."""

    def __init__(self, rows: list[dict], questions: list[str] | None = None, extra: Doc | None = None):
        self.rows = rows
        self.questions = questions or []
        self.extra = extra  # any sections humans added, preserved verbatim

    @classmethod
    def load(cls, kb: Path) -> "Connections":
        p = kb / "connections.md"
        if not p.exists():
            return cls([])
        doc = Doc.parse(p.read_text())
        rows = []
        for line in doc.sections.get("Edges", []):
            if not line.startswith("|") or line.startswith(CONN_HEADER) or line.startswith("|---"):
                continue
            cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
            if len(cells) != 5 or " -> " not in cells[0]:
                continue
            frm, to = (x.strip() for x in cells[0].split(" -> ", 1))
            rows.append({"from": frm, "to": to, "protocol": cells[1], "auth": cells[2],
                         "source": cells[3], "verified": cells[4]})
        return cls(rows, list(doc.sections.get("Open questions", [])), doc)

    def key(self, r):
        return (r["from"], r["to"], r["protocol"])

    def find(self, frm, to, protocol=None):
        return [r for r in self.rows if r["from"] == frm and r["to"] == to and (protocol is None or r["protocol"] == protocol)]

    def add_question(self, text: str) -> bool:
        qid = question_id(text)
        if any(f"question: {qid};" in q for q in self.questions):
            return False
        self.questions.append(question_line(text))
        return True

    def upsert(self, row: dict) -> str:
        """Candidate/infra/user edge. Never deletes; never edits a `user` row."""
        src = row["source"]
        same = self.find(row["from"], row["to"], row["protocol"])
        if same:
            cur = same[0]
            if cur["source"] == src:
                if cur["auth"] == "?" and row.get("auth", "?") != "?":
                    cur["auth"] = row["auth"]
                    return "updated"
                return "same"
            if source_rank(src) > source_rank(cur["source"]):
                cur.update(row)
                return "updated"
            if row.get("auth", "?") not in ("?", cur["auth"]) and cur["auth"] != "?":
                self.add_question(f"connection {row['from']} -> {row['to']} ({row['protocol']}): auth `{cur['auth']}` "
                                  f"({cur['source']}) vs `{row['auth']}` ({src}); keeping {cur['source']}. Confirm?")
                return "conflict"
            return "same"
        # a different protocol between the same systems is a separate connection
        self.rows.append({"auth": "?", **row})
        return "added"

    def mermaid(self) -> list[str]:
        def nid(s):
            return re.sub(r"[^A-Za-z0-9_]", "_", s)
        nodes, lines = [], []
        for r in sorted(self.rows, key=lambda r: (r["from"], r["to"], r["protocol"])):
            for n in (r["from"], r["to"]):
                if n not in nodes:
                    nodes.append(n)
            arrow = "-.->" if r["verified"] == "pending" else "-->"
            label = r["protocol"].replace('"', "'")
            lines.append(f"  {nid(r['from'])} {arrow}|\"{label}\"| {nid(r['to'])}")
        decl = [f"  {nid(n)}[\"{n}\"]" for n in sorted(nodes)]
        return [MERMAID_BEGIN, "```mermaid", "graph LR", *decl, *lines, "```", MERMAID_END]

    def render(self) -> str:
        rows = sorted(self.rows, key=lambda r: (r["from"], r["to"], r["protocol"]))
        table = [CONN_HEADER, CONN_SEP] + [
            f"| {_cell(r['from'])} -> {_cell(r['to'])} | {_cell(r['protocol'])} | {_cell(r['auth'])} | "
            f"{_cell(r['source'])} | {_cell(r['verified'])} |" for r in rows
        ]
        doc = Doc(None, "Connections", {"Edges": table, "Graph": self.mermaid(), "Open questions": self.questions},
                  ["Edges", "Graph"], CONN_PREAMBLE)
        if self.extra:
            for name in self.extra.order:
                if name not in ("Edges", "Graph", "Open questions"):
                    doc.sections[name] = self.extra.sections[name]
                    doc.order.append(name)
        doc.order.append("Open questions")
        return doc.render()

    def save(self, kb: Path) -> None:
        write_text(kb / "connections.md", self.render())


def cmd_add_connection(args) -> dict:
    kb = kb_path(args)
    refuse_secrets(args.frm, args.to, args.protocol, args.auth, args.source)
    source = validate_source(args.source)
    ensure_clean(kb, args.force)
    for s in (args.frm, args.to):
        if not s.startswith("external:"):
            require_system(kb, s)
    conns = Connections.load(kb)
    result = conns.upsert({"from": args.frm, "to": args.to, "protocol": args.protocol, "auth": args.auth or "?",
                           "source": source, "verified": today()})
    conns.save(kb)
    write_index(kb)
    committed = commit(kb, f"kb: connection {args.frm} -> {args.to} ({args.protocol}, {source})")
    return {"from": args.frm, "to": args.to, "result": result, "committed": committed}


# --------------------------------------------------------------------------- ingest

MAX_DEPS = 25

EDGE_PROTOCOL = {
    "manifest": "library (manifest)",
    "submodule": "git submodule",
    "workflow_uses": "ci (workflow uses)",
    "image": "container image",
}


def repo_slug(full_name: str) -> str:
    name = full_name.split("/")[-1].lower()
    name = re.sub(r"[^a-z0-9._-]+", "-", name).strip("-.") or "repo"
    return name


def cmd_ingest(args) -> dict:
    """graph.json -> candidate systems, facts and connection rows (verified: pending).

    Never deletes, never edits `source: user` lines, idempotent (a second run on the
    same graph commits nothing).
    """
    kb = kb_path(args)
    try:
        graph = json.loads(Path(args.graph).read_text())
    except (OSError, ValueError) as e:
        raise KBError(f"cannot read graph {args.graph}: {e}")
    if graph.get("version") != 1:
        raise KBError(f"unsupported graph.json version {graph.get('version')!r}")
    refuse_secrets(json.dumps(graph))
    ensure_clean(kb, args.force)

    # repo -> system: existing systems claim their repos; unclaimed repos get a candidate
    owner: dict[str, str] = {}
    for name, doc in iter_systems(kb):
        for r in (doc.meta or {}).get("repos") or []:
            owner.setdefault(r, name)
    stats = {"systems_added": [], "facts": {}, "connections": {}, "questions": 0}

    def bump(d, k):
        d[k] = d.get(k, 0) + 1

    docs: dict[str, Doc] = {}
    for repo in sorted(graph.get("repos", []), key=lambda r: r["full_name"]):
        fn = repo["full_name"]
        sysname = owner.get(fn) or repo_slug(fn)
        owner[fn] = sysname
        doc = docs.get(sysname) or load_system(kb, sysname)
        if doc is None:
            doc = new_system(sysname)
            stats["systems_added"].append(sysname)
        docs[sysname] = doc
        if not isinstance(doc.meta.get("repos"), list):
            doc.meta["repos"] = []
        _add_unique(doc.meta["repos"], [fn])
        before = sum(1 for _ in doc.questions())
        facts = []
        if repo.get("languages"):
            facts.append(("Build, run, test", f"{fn} languages", ", ".join(repo["languages"]), fn))
        if repo.get("default_branch"):
            facts.append(("Build, run, test", f"{fn} default branch", repo["default_branch"], fn))
        path = repo.get("path")
        if path and (Path(path) / "AGENTS.md").is_file():
            branch = repo.get("default_branch") or "main"
            facts.append(("Build, run, test", f"{fn} build/run/test",
                          f"see [AGENTS.md](https://github.com/{fn}/blob/{branch}/AGENTS.md)", f"{fn}:AGENTS.md"))
        for m in repo.get("manifests") or []:
            if m.get("deps"):
                deps = m["deps"]
                shown = ", ".join(deps[:MAX_DEPS]) + (f" (+{len(deps) - MAX_DEPS} more)" if len(deps) > MAX_DEPS else "")
                # keyed by manifest file: workspaces have many manifests of the same kind
                facts.append(("Dependencies", f"{fn}:{m['file']} {m['kind']} deps", shown, f"{fn}:{m['file']}"))
        if repo.get("ports"):
            facts.append(("Interfaces", f"{fn} exposed ports", ", ".join(str(p) for p in repo["ports"]), fn))
        for section, key, value, src in facts:
            bump(stats["facts"], upsert_fact(doc, section, key, value, src, verified="pending", context=sysname))
        stats["questions"] += sum(1 for _ in doc.questions()) - before

    conns = Connections.load(kb)
    qbefore = len(conns.questions)
    for e in sorted(graph.get("edges", []), key=lambda e: (e["from"], e["to"], e["kind"], e.get("evidence", ""))):
        frm = owner.get(e["from"], repo_slug(e["from"]))
        to = e["to"] if e["to"].startswith("external:") else owner.get(e["to"], repo_slug(e["to"]))
        if frm == to:
            continue  # internal to one system
        proto = e.get("protocol") if e["kind"] == "url" else EDGE_PROTOCOL.get(e["kind"], e["kind"])
        src = f"{e['from']}:{e['evidence']}"
        if ";" in src or "-->" in src:
            continue
        bump(stats["connections"], conns.upsert({"from": frm, "to": to, "protocol": proto or "?", "auth": "?",
                                                 "source": src, "verified": "pending"}))
        if frm in docs and not to.startswith("external:"):
            bump(stats["facts"], upsert_fact(docs[frm], "Dependencies", f"uses {to} ({proto})",
                                             f"[{to}]({to}.md)", src, verified="pending", context=frm))
    stats["questions"] += len(conns.questions) - qbefore

    for doc in docs.values():
        save_system(kb, doc)
    conns.save(kb)
    write_index(kb)
    committed = commit(kb, f"kb: ingest {Path(args.graph).name} ({len(graph.get('repos', []))} repos)")
    stats["committed"] = committed
    return stats


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


def cmd_index(args) -> dict:
    kb = kb_path(args)
    ensure_clean(kb, args.force)
    write_index(kb)
    conns = Connections.load(kb)
    conns.save(kb)  # regenerates the mermaid graph from the table
    committed = commit(kb, "kb: regenerate index and connection graph")
    return {"systems": sum(1 for _ in iter_systems(kb)), "connections": len(conns.rows), "committed": committed}


# --------------------------------------------------------------------------- stale


def _age(verified: str, now: _dt.date) -> int | None:
    try:
        return (now - _dt.date.fromisoformat(str(verified))).days
    except ValueError:
        return None


def collect_stale(kb: Path, days: int) -> dict:
    """Dated facts older than `days` (stale) and unconfirmed candidates (pending)."""
    now = _dt.date.fromisoformat(today())
    stale, pending, questions = [], [], []

    def check(item: dict, verified):
        if verified in (None, "", "pending"):
            pending.append(item)
            return
        age = _age(verified, now)
        if age is None or age > days:
            stale.append({**item, "verified": str(verified), "age_days": age})

    for name, doc in iter_systems(kb):
        m = doc.meta or {}
        f = f"systems/{name}.md"
        if m.get("status") != "candidate":
            check({"file": f, "system": name, "kind": "system", "key": name}, m.get("verified"))
        for _, _, fact in doc.facts():
            if fact["source"] == "user" and fact["verified"] == "pending":
                continue
            check({"file": f, "system": name, "kind": "fact", "key": fact["key"], "source": fact["source"]},
                  fact["verified"])
        for e in ((m.get("runtime") or {}).get("environments") or []):
            check({"file": f, "system": name, "kind": "environment", "key": e.get("name"), "source": e.get("source")},
                  e.get("verified"))
        for mon in m.get("monitoring") or []:
            key = mon.get("kind") + (f"/{mon['project']}" if mon.get("project") else "")
            check({"file": f, "system": name, "kind": "monitor", "key": key}, mon.get("verified"))
        for q in doc.questions():
            if q["done"] == " ":
                questions.append({"file": f, "system": name, "id": q["id"], "text": q["text"], "raised": q["raised"]})
    conns = Connections.load(kb)
    for r in conns.rows:
        check({"file": "connections.md", "system": r["from"], "kind": "connection",
               "key": f"{r['from']} -> {r['to']} ({r['protocol']})", "source": r["source"]}, r["verified"])
    for line in conns.questions:
        mq = QUESTION_RE.match(line)
        if mq and mq["done"] == " ":
            questions.append({"file": "connections.md", "system": None, "id": mq["id"], "text": mq["text"],
                              "raised": mq["raised"]})
    return {"days": days, "today": today(), "stale": stale, "pending": pending, "questions": questions}


def cmd_stale(args) -> dict:
    kb = kb_path(args)
    if not (kb / "systems").is_dir():
        raise KBError(f"{kb} is not a knowledge base (no systems/)")
    out = collect_stale(kb, args.days)
    out["counts"] = {k: len(out[k]) for k in ("stale", "pending", "questions")}
    return out


# --------------------------------------------------------------------------- GitHub: ensure-repo / propose

PROTECTION_ADVICE = """\
kb.py: recommended (factory never changes org or repo settings; ask an admin):
  - protect the default branch: require a pull request + 1 approval, no force-push, no deletion
  - keep the repository PRIVATE; restrict it to the teams whose systems it describes
  - enable secret scanning + push protection (a second net behind kb.py's refusal)"""


def gh_json(args: list[str], cwd=None):
    p = run(["gh", *args], cwd=cwd, check=False)
    if p.returncode != 0:
        return None, (p.stderr or p.stdout).strip()
    try:
        return json.loads(p.stdout or "null"), ""
    except ValueError:
        return None, f"unexpected gh output: {p.stdout[:200]}"


def repo_visibility(repo: str, cwd=None) -> tuple[dict | None, str]:
    """(info, err). info is None with err "" when the repo does not exist."""
    info, err = gh_json(["repo", "view", repo, "--json", "visibility,defaultBranchRef,url,nameWithOwner"], cwd=cwd)
    if info is None and re.search(r"Could not resolve to a Repository|Not Found|HTTP 404", err):
        return None, ""
    if info is None:
        raise KBError(f"gh repo view {repo} failed: {err}")
    return info, ""


def cmd_ensure_repo(args) -> dict:
    """Clone (or, with --create after approval, create) the private `<org>/<name>` KB repo."""
    repo = f"{args.org}/{args.name}"
    path = Path(args.path or os.environ.get("KB_DIR") or DEFAULT_KB).expanduser().resolve()
    info, _ = repo_visibility(repo)
    created = False
    if info is None:
        if not args.create:
            print(json.dumps({"url": None, "path": str(path), "visibility": None, "created": False,
                              "exists": False, "action": f"gh repo create {repo} --private (needs approval; rerun with --create)"},
                             indent=2))
            raise KBError(f"{repo} does not exist; rerun with --create after the user approves a new PRIVATE repo", 5)
        run(["gh", "repo", "create", repo, "--private",
             "--description", "Knowledge base: living HLD of our systems (maintained by factory; private)"])
        created = True
        info, _ = repo_visibility(repo)
        if info is None:
            raise KBError(f"created {repo} but cannot read it back")
    vis = (info.get("visibility") or "").upper()
    if vis != "PRIVATE":
        print(json.dumps({"url": info.get("url"), "path": str(path), "visibility": vis, "created": created}, indent=2))
        raise KBError(f"refused: {repo} is {vis or 'of unknown visibility'}; the knowledge base must be PRIVATE. "
                      "Nothing was cloned or written.", 2)
    url = info.get("url") or f"https://github.com/{repo}"
    if is_git(path):
        origin = git(path, "remote", "get-url", "origin", check=False)
        if not re.search(rf"[:/]{re.escape(repo)}(\.git)?$", origin or "", re.I):
            raise KBError(f"{path} is a checkout of {origin or 'no origin'}, not {repo}")
        git(path, "fetch", "-q", "origin", check=False)
    elif path.exists() and any(path.iterdir()):
        raise KBError(f"{path} exists and is not a git checkout; move it or pass --path")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        run(["gh", "repo", "clone", repo, str(path), "--", "-q"])
    print(PROTECTION_ADVICE, file=sys.stderr)
    return {"url": url, "path": str(path), "visibility": vis, "created": created,
            "default_branch": (info.get("defaultBranchRef") or {}).get("name"),
            "initialized": (path / "systems").is_dir()}


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:50].rstrip("-") or "update"


def remote_default_branch(kb: Path) -> str | None:
    out = git(kb, "ls-remote", "--symref", "origin", "HEAD", check=False)
    m = re.search(r"ref: refs/heads/(\S+)\s+HEAD", out or "")
    return m.group(1) if m else None


def cmd_propose(args) -> dict:
    """Push local KB commits as a `factory/<slug>` branch and open a PR. Never pushes to
    the default branch, except `--init` for the very first commit of an empty repo."""
    kb = kb_path(args)
    refuse_secrets(args.title, args.body)
    ensure_clean(kb, args.force)
    origin = git(kb, "remote", "get-url", "origin", check=False)
    if not origin:
        raise KBError(f"{kb} has no origin remote (use `kb.py ensure-repo`)")
    info, _ = repo_visibility(origin, cwd=kb)  # re-checked on every proposal: never push to a public KB
    vis = ((info or {}).get("visibility") or "").upper()
    if vis != "PRIVATE":
        raise KBError(f"refused: origin {origin} is {vis or 'not found'}; the knowledge base must be PRIVATE", 2)
    current = git(kb, "rev-parse", "--abbrev-ref", "HEAD")
    default = remote_default_branch(kb)

    if args.init:
        if default is not None or git(kb, "ls-remote", "--heads", "origin", check=False):
            raise KBError("refused: --init only pushes the first commit of an EMPTY repository; "
                          "this one already has branches, so propose a PR instead", 5)
        run(["git", "push", "-q", "-u", "origin", f"HEAD:refs/heads/{current}"], cwd=kb)
        return {"pushed": current, "pr": None, "init": True}

    if default is None:
        raise KBError("origin has no default branch yet: push the skeleton first with `kb.py propose --init` "
                      "(after the user approves the initial commit to main)", 5)
    git(kb, "fetch", "-q", "origin", default)
    base = f"origin/{default}"
    ahead = git(kb, "rev-list", "--count", f"{base}..HEAD")
    if ahead == "0":
        return {"pushed": None, "pr": None, "reason": f"nothing to propose: HEAD has no commits beyond {base}"}

    if current == default:
        branch = f"factory/{slugify(args.title)}"
    elif current.startswith("factory/"):
        branch = current
    else:
        raise KBError(f"refused: on branch {current!r}; propose from {default} or a factory/* branch")

    if not args.allow_multiple:
        prs, err = gh_json(["pr", "list", "--state", "open", "--json", "number,headRefName,url,title",
                            "--limit", "100"], cwd=kb)
        if prs is None:
            raise KBError(f"gh pr list failed: {err}")
        mine = [p for p in prs if str(p.get("headRefName", "")).startswith("factory/") and p["headRefName"] != branch]
        if mine:
            raise KBError("refused: a factory PR is already open (" + ", ".join(p.get("url", str(p["number"])) for p in mine)
                          + "); merge or close it first, or pass --allow-multiple", 5)

    log = git(kb, "log", "--format=- %s", f"{base}..HEAD")
    body = args.body or (
        "Proposed by `factory` (`kb.py propose`). Review every fact's `source:`; rows marked "
        "`verified: pending` are unconfirmed candidates.\n\n### Commits\n\n" + log +
        "\n\nHumans merge; automation never does."
    )
    refuse_secrets(body)
    if current != branch:
        git(kb, "branch", "-f", branch, "HEAD")
    run(["git", "push", "-q", "-u", "origin", f"{branch}:refs/heads/{branch}"], cwd=kb)
    p = run(["gh", "pr", "create", "--base", default, "--head", branch, "--title", args.title, "--body", body], cwd=kb)
    pr_url = (p.stdout or "").strip().splitlines()[-1] if p.stdout.strip() else None
    if current == default:
        # the proposal lives on its branch; the local default branch goes back to the remote's
        git(kb, "reset", "-q", "--hard", base)
    return {"pushed": branch, "base": default, "pr": pr_url, "commits": int(ahead)}


# --------------------------------------------------------------------------- board-ops


def _existing_markers(path: str | None) -> set[str]:
    if not path:
        return set()
    data = json.loads(Path(path).read_text())
    issues = data.get("issues", data) if isinstance(data, dict) else data
    found = set()
    for it in issues or []:
        blob = f"{it.get('title', '')}\n{it.get('content', '') or it.get('body', '')}"
        found.update(re.findall(r"kb:(?:question|stale):[0-9a-z._-]+", blob))
    return found


def cmd_board_ops(args) -> dict:
    """project_update JSON for the "Knowledge base" board: one issue per open question, one
    per system with stale facts. Deduped against --existing (project_get output) by marker."""
    kb = kb_path(args)
    if not (kb / "systems").is_dir():
        raise KBError(f"{kb} is not a knowledge base (no systems/)")
    data = collect_stale(kb, args.days)
    seen = _existing_markers(args.existing)
    ops = []
    if args.with_index:
        ops.append({"type": "set_project", "description": (kb / "index.md").read_text()})

    def issue(title, content, marker, label):
        if marker in seen:
            return
        seen.add(marker)
        op = {"type": "add_issue", "title": title[:200], "content": content + f"\n\n<!-- {marker} -->"}
        if label:
            op["labels"] = [label]
        ops.append(op)

    for q in data["questions"]:
        text = q["text"]
        short = text if len(text) <= 110 else text[:107] + "..."
        issue(f"KB question: {short}",
              f"Open question in `{q['file']}` (raised {q['raised']}):\n\n> {text}\n\n"
              "Answer it in the knowledge base: record the correct value with `source: user` "
              "(`kb.py add-fact ... --source user` or edit the file) and tick the question, then open a PR.",
              f"kb:question:{q['id']}", args.question_label)

    by_system: dict[str, list[dict]] = {}
    for s in data["stale"]:
        by_system.setdefault(s["file"], []).append(s)
    for f, items in sorted(by_system.items()):
        newest = max((i["verified"] for i in items), default="")
        marker = f"kb:stale:{hashlib.sha1((f + newest + str(len(items))).encode()).hexdigest()[:10]}"
        lines = "\n".join(f"- {i['kind']} `{i['key']}` (source `{i.get('source') or '-'}`, verified {i['verified']})"
                          for i in items)
        issue(f"KB stale: re-verify {len(items)} fact(s) in {f}",
              f"Not verified in the last {args.days} days:\n\n{lines}\n\n"
              "Re-check each against its source and refresh `verified:` (or correct it) via a KB PR.",
              marker, args.stale_label)
    return {"project_id": args.project_id, "operations": ops}


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

    s = sub.add_parser("add-fact", parents=[common], help="add a sourced fact line to a system")
    s.add_argument("system")
    s.add_argument("--section", required=True, help="purpose | interfaces | dependencies | build")
    s.add_argument("--key", required=True)
    s.add_argument("--value", required=True)
    s.add_argument("--source", required=True, help="owner/repo:path | gh api ... | infra:<platform>:<resource> | user")
    s.add_argument("--pending", action="store_true", help="mark as an unconfirmed candidate")
    s.set_defaults(func=cmd_add_fact)

    s = sub.add_parser("add-connection", parents=[common], help="record a system -> system edge")
    s.add_argument("frm", metavar="from")
    s.add_argument("to", help="system id or external:<host>")
    s.add_argument("--protocol", required=True, help="http, ws, grpc, sql, queue, library (manifest), ...")
    s.add_argument("--auth", help="mechanism only, e.g. oauth, mTLS, api-key header (never the key)")
    s.add_argument("--source", required=True)
    s.set_defaults(func=cmd_add_connection)

    s = sub.add_parser("ingest", parents=[common], help="merge graph.json (repo_graph.py) as pending candidates")
    s.add_argument("graph")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("index", parents=[common], help="regenerate index.md and the connections mermaid graph")
    s.set_defaults(func=cmd_index)

    s = sub.add_parser("stale", parents=[common], help="list facts not verified in --days, pending candidates, open questions")
    s.add_argument("--days", type=int, default=30)
    s.set_defaults(func=cmd_stale)

    s = sub.add_parser("ensure-repo", help="verify/clone the PRIVATE <org>/knowledge repo (exit 2 if public)")
    s.add_argument("org")
    s.add_argument("--name", default="knowledge")
    s.add_argument("--path", help=f"local checkout (default $KB_DIR or {DEFAULT_KB})")
    s.add_argument("--create", action="store_true", help="create it with `gh repo create --private` (only after approval)")
    s.set_defaults(func=cmd_ensure_repo)

    s = sub.add_parser("propose", parents=[common], help="push commits as factory/<slug> and open a PR")
    s.add_argument("title")
    s.add_argument("--body")
    s.add_argument("--init", action="store_true", help="push the initial skeleton to the default branch of an EMPTY repo")
    s.add_argument("--allow-multiple", action="store_true", help="open even if another factory/* PR is open")
    s.set_defaults(func=cmd_propose)

    s = sub.add_parser("board-ops", parents=[common], help="print project_update JSON for open questions + stale facts")
    s.add_argument("--project-id", default="<knowledge-base-project-id>")
    s.add_argument("--existing", help="project_get JSON (issues[]) to dedupe against")
    s.add_argument("--days", type=int, default=30)
    s.add_argument("--with-index", action="store_true", help="also set the project description to index.md")
    s.add_argument("--question-label", help="label id for question issues")
    s.add_argument("--stale-label", help="label id for stale issues")
    s.set_defaults(func=cmd_board_ops)
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
