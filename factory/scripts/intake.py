#!/usr/bin/env python3
"""Turn CI failures and monitoring alerts into Forge board-issue payloads.

Polling intake for factory's event-trigger routines (references/triggers.md).
Deterministic and side-effect free apart from read-only `gh run list` calls:
it never creates issues, never calls `project_run`, never writes state. The
routine prompt files the emitted payloads with `project_update` and stores the
printed cursor with `state.py set-routine <id> --cursor <cursor>`.

  intake.py ci     --org ORG --since CURSOR [--graph .agents/graph.json] [--state F]
                   [--repos a,b] [--kb DIR] [--allowlist F] [--jobs 8] [--default-branch-only]
  intake.py alerts --source sentry|datadog|pagerduty|grafana --since CURSOR
                   [--kb DIR] [--allowlist F] < export.json

stdout (JSON): {"cursor", "issues": [payload...], "duplicates", "polled", "warnings"}
Exit: 0 ok (partial failures are `warnings`), 2 usage / unreadable input.

Cursor: "<ISO-8601 UTC poll time>" optionally followed by
";seen=<h1>,<h2>,..." (short hashes of recently emitted dedupe keys, newest
last, at most SEEN_LIMIT). Each poll re-reads `--lookback` hours before the
mark (runs finish after they are created, alerts arrive late); anything whose
key is in `seen` is a duplicate, anything older than mark - lookback is
ignored. An empty/"null" cursor means "the last 24 hours".
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import kb as K  # noqa: E402  (KB parsing + secret patterns; single source of truth)

SEEN_LIMIT = 500
FIRST_POLL = _dt.timedelta(hours=24)
LOOKBACK_H = 6  # overlap: a run created before the mark may fail after the previous poll
SOURCES = ("sentry", "datadog", "pagerduty", "grafana")
LABELS = ("ci-failure", "alert")
UNKNOWN = "unknown"
BODY_MAX = 1500


class UsageError(Exception):
    pass


# --------------------------------------------------------------------------- time + cursor


def utc(s) -> _dt.datetime | None:
    """Parse ISO-8601 (Z or offset) or epoch seconds/ms; None if unparseable."""
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)) or (isinstance(s, str) and re.fullmatch(r"\d{9,13}", s)):
        v = float(s)
        return _dt.datetime.fromtimestamp(v / 1000 if v > 1e11 else v, _dt.timezone.utc)
    try:
        d = _dt.datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)).astimezone(_dt.timezone.utc)


def iso(d: _dt.datetime) -> str:
    return d.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now() -> _dt.datetime:
    env = os.environ.get("INTAKE_NOW")  # tests pin the clock
    return utc(env) if env else _dt.datetime.now(_dt.timezone.utc)


def parse_cursor(raw: str | None) -> tuple[_dt.datetime, list[str]]:
    raw = (raw or "").strip()
    if raw in ("", "null", "none", "None"):
        return now() - FIRST_POLL, []
    mark, _, rest = raw.partition(";")
    t = utc(mark)
    if t is None:
        raise UsageError(f"--since: not an ISO-8601 timestamp or intake cursor: {raw[:60]!r}")
    seen = []
    if rest.startswith("seen="):
        seen = [h for h in rest[5:].split(",") if re.fullmatch(r"[0-9a-f]{12}", h)]
    return t, seen


def format_cursor(t: _dt.datetime, seen: list[str]) -> str:
    seen = seen[-SEEN_LIMIT:]
    return iso(t) + (";seen=" + ",".join(seen) if seen else "")


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- redaction

_EXTRA_SECRETS = [
    ("bearer", re.compile(r"(?i)\b(bearer|token)\s+[A-Za-z0-9._~+/\-]{16,}=*")),
    ("url-credentials", re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)")),
    ("query-secret", re.compile(r"(?i)(?<=[?&])(access_token|token|key|api_key|apikey|sig|signature|secret|password)=[^&\s]+")),
]


def redact(text) -> str:
    """Replace every secret-looking substring; values are never echoed back."""
    if text is None:
        return ""
    s = str(text)
    for name, pat in _EXTRA_SECRETS + list(K.SECRET_PATTERNS):
        if name == "password-assignment":
            s = pat.sub(lambda m: re.split(r"[=:]", m.group(0), 1)[0].rstrip() + "=[REDACTED]", s)
        elif name == "query-secret":
            s = pat.sub(lambda m: m.group(1) + "=[REDACTED]", s)
        else:
            s = pat.sub(f"[REDACTED:{name}]", s)
    return s


# --------------------------------------------------------------------------- KB system map


def _host(u: str | None) -> str | None:
    if not u:
        return None
    u = u.strip().lower()
    h = urlparse(u if "://" in u else "//" + u).hostname
    return h or None


_ENV_SUFFIX = re.compile(r"[-_.](prod|production|prd|staging|stage|stg|dev|development|qa|preview)$")


class SystemMap:
    """Resolves repos, service names and hostnames to KB system ids.

    Sources, strongest first: monitoring[].project for the same vendor kind,
    runtime.environments[] ids and url hosts, system ids, repo names, then
    `connections.md` rows `X -> external:<host>` (host resolves to X, the
    system that depends on it). Anything else is `unknown`.
    """

    def __init__(self, kb: Path | None):
        self.repo = {}      # "owner/name" (lower) -> system
        self.names = {}     # service / host / id (lower) -> (rank, system)
        self.by_kind = {}   # (kind, project lower) -> system
        self.warnings = []
        if kb is None or not (kb / "systems").is_dir():
            if kb is not None:
                self.warnings.append(f"no KB systems/ at {kb}; every item maps to system `unknown`")
            return
        for name, doc in K.iter_systems(kb):
            m = doc.meta or {}
            self._name(name, name, 2)
            for r in m.get("repos") or []:
                self.repo[str(r).lower()] = name
                self._name(str(r).split("/")[-1], name, 3)
            for mon in m.get("monitoring") or []:
                if mon.get("project"):
                    self.by_kind[(mon.get("kind"), str(mon["project"]).lower())] = name
                    self._name(mon["project"], name, 1)
            for env in ((m.get("runtime") or {}).get("environments") or []):
                self._name(env.get("id"), name, 1)
                self._name(_host(env.get("url")), name, 1)
        try:
            conns = K.Connections.load(kb)
        except Exception as e:  # a broken connections.md must not stop intake
            self.warnings.append(f"connections.md unreadable: {e}")
            return
        for r in conns.rows:
            for a, b in ((r["from"], r["to"]), (r["to"], r["from"])):
                if a.startswith("external:") and not b.startswith("external:"):
                    self._name(_host(a[len("external:"):]) or a[len("external:"):], b, 4)

    def _name(self, key, system, rank):
        if not key:
            return
        k = str(key).lower()
        if k not in self.names or rank < self.names[k][0]:
            self.names[k] = (rank, system)

    def for_repo(self, full_name: str) -> str:
        return self.repo.get(full_name.lower(), UNKNOWN)

    def for_alert(self, kind: str, services: list, hosts: list) -> str:
        best = None
        for s in services:
            if not s:
                continue
            s = str(s).lower()
            if (kind, s) in self.by_kind:
                return self.by_kind[(kind, s)]
            for cand in (s, _ENV_SUFFIX.sub("", s)):
                if cand in self.names and (best is None or self.names[cand][0] < best[0]):
                    best = self.names[cand]
        for h in hosts:
            h = _host(str(h)) if h else None
            if not h:
                continue
            for cand in (h, h.split(".")[0], _ENV_SUFFIX.sub("", h.split(".")[0])):
                if cand in self.names and (best is None or self.names[cand][0] < best[0]):
                    best = self.names[cand]
        return best[1] if best else UNKNOWN


# --------------------------------------------------------------------------- allowlist


def load_allowlist(path: str | None) -> set[tuple[str, str]]:
    """{"version": 1, "auto_run": [{"system": "api", "label": "ci-failure"}]} -> {(system, label)}.

    Exact matches only: no wildcards, and `unknown` can never be allowlisted.
    """
    if not path:
        return set()
    try:
        data = json.loads(Path(path).expanduser().read_text())
    except (OSError, ValueError) as e:
        raise UsageError(f"--allowlist {path}: {e}")
    rules = data.get("auto_run") if isinstance(data, dict) else None
    if not isinstance(rules, list):
        raise UsageError(f"--allowlist {path}: expected {{\"auto_run\": [{{\"system\", \"label\"}}]}}")
    out = set()
    for r in rules:
        if not isinstance(r, dict) or not r.get("system") or not r.get("label"):
            raise UsageError(f"--allowlist {path}: every rule needs `system` and `label`: {r!r}")
        sys_, label = str(r["system"]), str(r["label"])
        if "*" in sys_ or "*" in label or sys_ == UNKNOWN:
            raise UsageError(f"--allowlist {path}: wildcards and `unknown` are not allowed: {r!r}")
        if label not in LABELS:
            raise UsageError(f"--allowlist {path}: label must be one of {', '.join(LABELS)}: {r!r}")
        out.add((sys_, label))
    return out


# --------------------------------------------------------------------------- payloads


def payload(*, key, title, body_lines, system, label, url, at, source, allow, priority="medium", extra=None):
    marker = f"<!-- factory-intake: {key_hash(key)} -->"  # lets the routine also dedupe against the board
    body = "\n".join(redact(x) for x in body_lines if x)
    if len(body) > BODY_MAX:
        body = body[:BODY_MAX] + "\n[truncated]"
    p = {
        "title": redact(title)[:200],
        "body": f"{body}\n\nKB system: `{system}`\n{marker}",
        "labels": [label],
        "system": system,
        "priority": priority,
        "url": redact(url),
        "source": source,
        "occurred_at": iso(at) if at else None,
        "dedupe_key": key_hash(key),
        "auto_run": (system, label) in allow,
    }
    if extra:
        p.update(extra)
    return p


def select(items, since, seen, lookback):
    """items: (key, time, payload). Drops too-old + already-seen, dedupes within the batch."""
    seen_set, fresh, dup, new_keys = set(seen), [], 0, []
    floor = since - lookback
    mark = max(since, now())  # the poll time: the next poll re-reads [mark - lookback, ...)
    for key, at, p in sorted(items, key=lambda x: (x[1] or since, x[0])):
        h = key_hash(key)
        if h in seen_set:
            dup += 1
            continue
        if at is not None and at < floor:
            continue
        seen_set.add(h)
        new_keys.append(h)
        fresh.append(p)
    # keep hashes still within the overlap window, plus everything new
    return fresh, dup, format_cursor(mark, [h for h in seen if h not in new_keys] + new_keys)


# --------------------------------------------------------------------------- ci


RUN_FIELDS = "databaseId,workflowName,headBranch,headSha,url,createdAt,displayTitle,event,conclusion"


def repos_from(args) -> tuple[list[str], dict[str, str]]:
    """-> (sorted owner/name list within --org, {repo: default_branch})."""
    repos, branches = set(), {}
    if args.repos:
        repos |= {r.strip() for r in args.repos.split(",") if r.strip()}
    graph = Path(args.graph)
    if graph.exists():
        try:
            for r in json.loads(graph.read_text()).get("repos", []):
                if r.get("full_name"):
                    repos.add(r["full_name"])
                    if r.get("default_branch"):
                        branches[r["full_name"]] = r["default_branch"]
        except (OSError, ValueError, AttributeError):
            pass
    st = Path(args.state)
    if st.exists():
        try:
            for name, r in (json.loads(st.read_text()).get("repos") or {}).items():
                m = re.search(r"github\.com[/:]([^/]+/[^/]+?)(?:\.git)?/?$", r.get("url") or "")
                repos.add(m.group(1) if m else f"{args.org}/{name}")
        except (OSError, ValueError, AttributeError):
            pass
    org = args.org.lower()
    return sorted(r for r in repos if r.split("/")[0].lower() == org), branches


def gh_runs(repo: str, since: _dt.datetime) -> list[dict]:
    cmd = ["gh", "run", "list", "-R", repo, "--status", "failure", "--created", f">={iso(since)}",
           "--limit", "100", "--json", RUN_FIELDS]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError((p.stderr.strip().splitlines() or ["gh failed"])[-1][:200])
    return json.loads(p.stdout or "[]")


def cmd_ci(args) -> dict:
    since, seen = parse_cursor(args.since)
    allow = load_allowlist(args.allowlist)
    smap = SystemMap(Path(args.kb).expanduser() if args.kb else None)
    repos, branches = repos_from(args)
    warnings = list(smap.warnings)
    if not repos:
        warnings.append(f"no {args.org} repos found in --repos, {args.graph} or {args.state}")
    items = []
    lookback = _dt.timedelta(hours=args.lookback)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
        futs = {ex.submit(gh_runs, r, since - lookback): r for r in repos}
        for f in concurrent.futures.as_completed(futs):
            repo = futs[f]
            try:
                runs = f.result()
            except Exception as e:  # one repo failing never stops the poll
                warnings.append(f"{repo}: {e}")
                continue
            system = smap.for_repo(repo)
            for run in runs:
                if run.get("conclusion") not in (None, "failure"):
                    continue
                wf, br, sha = run.get("workflowName") or "?", run.get("headBranch") or "?", run.get("headSha") or "?"
                if args.default_branch_only and repo in branches and br != branches[repo]:
                    continue
                key = f"ci|{repo}|{wf}|{br}|{sha}"
                at = utc(run.get("createdAt"))
                items.append((key, at, payload(
                    key=key, title=f"CI failure: {wf} on {br} ({repo})",
                    body_lines=[f"Workflow **{wf}** failed on `{br}` at `{sha[:12]}` in `{repo}`.",
                                f"Run: {run.get('url') or '?'}",
                                f"Trigger: {run.get('event') or '?'}; title: {run.get('displayTitle') or ''}",
                                "Logs: `gh run view " + str(run.get("databaseId") or "") + f" -R {repo} --log-failed`"],
                    system=system, label="ci-failure", url=run.get("url"), at=at, source="github-actions",
                    allow=allow, extra={"repo": repo, "workflow": wf, "branch": br, "head_sha": sha})))
    fresh, dup, cursor = select(items, since, seen, lookback)
    return {"cursor": cursor, "issues": fresh, "duplicates": dup, "polled": len(repos),
            "warnings": sorted(warnings)}


# --------------------------------------------------------------------------- alerts


LIST_KEYS = ("issues", "incidents", "alerts", "events", "monitors", "results", "data", "items")


def unwrap(data):
    """Accept a bare list, {<list key>: [...]}, or an MCP tool result {content: [{type: text, text: json}]}."""
    if isinstance(data, dict) and isinstance(data.get("content"), list):
        out = []
        for c in data["content"]:
            if isinstance(c, dict) and c.get("type") == "text":
                try:
                    out.extend(unwrap(json.loads(c.get("text") or "null")))
                except ValueError:
                    continue
        return out
    if isinstance(data, dict):
        for k in LIST_KEYS:
            if isinstance(data.get(k), list):
                return data[k]
            if isinstance(data.get(k), dict):  # e.g. {"data": {"alerts": [...]}}
                inner = unwrap(data[k])
                if inner:
                    return inner
        return [data] if data else []
    return data if isinstance(data, list) else []


def _tags(v) -> dict:
    if isinstance(v, dict):
        return {str(k): str(x) for k, x in v.items()}
    out = {}
    for t in v or []:
        k, _, x = str(t).partition(":")
        out.setdefault(k, x)
    return out


SEVERITY_HIGH = {"fatal", "critical", "error", "high", "p1", "p2", "sev1", "sev2", "alert"}


def norm_alert(source: str, a: dict) -> dict | None:
    """-> {fp, title, detail, url, at, services, hosts, severity, status} or None for resolved/ok."""
    if not isinstance(a, dict):
        return None
    g = a.get
    if source == "sentry":
        proj = g("project")
        proj = proj.get("slug") if isinstance(proj, dict) else proj
        tags = _tags(g("tags"))
        return {"fp": g("id") or g("shortId") or g("fingerprint"), "title": g("title") or g("culprit"),
                "detail": g("culprit") or (g("metadata") or {}).get("value"), "url": g("permalink") or g("url"),
                "at": g("lastSeen") or g("firstSeen") or g("dateCreated"), "services": [proj, tags.get("service")],
                "hosts": [tags.get("server_name"), tags.get("url")], "severity": g("level"),
                "status": g("status"), "resolved": g("status") in ("resolved", "ignored")}
    if source == "datadog":
        tags = _tags(g("tags"))
        state = str(g("overall_state") or g("alert_type") or g("status") or "").lower()
        fp = g("aggregation_key") or (f"{g('monitor_id')}|{g('monitor_groups') or tags.get('host') or ''}"
                                      if g("monitor_id") else None) or g("id")
        return {"fp": fp, "title": g("title") or g("name"), "detail": g("message") or g("text"),
                "url": g("url") or g("overall_state_url"), "at": g("date_happened") or g("timestamp") or g("overall_state_modified") or g("modified"),
                "services": [tags.get("service"), tags.get("app")], "hosts": [tags.get("host"), g("host")],
                "severity": g("priority") or state, "status": state,
                "resolved": state in ("ok", "success", "recovered", "resolved", "info")}
    if source == "pagerduty":
        svc = g("service")
        svc = (svc.get("summary") or svc.get("name")) if isinstance(svc, dict) else svc
        body = g("body") if isinstance(g("body"), dict) else {}
        prio = g("priority")
        sev = (prio.get("summary") if isinstance(prio, dict) else None) or g("urgency")
        return {"fp": g("incident_key") or g("id"), "title": g("title") or g("summary") or g("description"),
                "detail": body.get("details") or g("description"), "url": g("html_url") or g("self"),
                "at": g("created_at") or g("last_status_change_at"), "services": [svc], "hosts": [],
                "severity": sev,
                "status": g("status"), "resolved": g("status") == "resolved"}
    if source == "grafana":
        labels, ann = g("labels") or {}, g("annotations") or {}
        st = g("status")
        state = str((st.get("state") if isinstance(st, dict) else st) or g("state") or "").lower()
        return {"fp": g("fingerprint") or g("id") or json.dumps(labels, sort_keys=True),
                "title": ann.get("summary") or labels.get("alertname") or g("title"),
                "detail": ann.get("description"), "url": g("generatorURL") or g("url"),
                "at": g("startsAt") or g("activeAt") or g("updatedAt"),
                "services": [labels.get("service"), labels.get("app"), labels.get("job"), labels.get("namespace")],
                "hosts": [labels.get("instance"), labels.get("host")],
                "severity": labels.get("severity"), "status": state,
                "resolved": state in ("resolved", "inactive", "normal", "ok", "suppressed")}
    return None


def cmd_alerts(args, stdin) -> dict:
    since, seen = parse_cursor(args.since)
    allow = load_allowlist(args.allowlist)
    smap = SystemMap(Path(args.kb).expanduser() if args.kb else None)
    raw = stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else []
    except ValueError as e:
        raise UsageError(f"stdin is not JSON ({e}); pass the MCP query result as-is")
    warnings, items, total = list(smap.warnings), [], 0
    for a in unwrap(data):
        total += 1
        n = norm_alert(args.source, a)
        if not n or n.get("resolved"):
            continue
        if not n.get("fp"):
            warnings.append(f"{args.source}: alert without an id/fingerprint skipped: {redact(n.get('title'))[:80]!r}")
            continue
        key = f"alert|{args.source}|{n['fp']}"
        at = utc(n.get("at"))
        system = smap.for_alert(args.source, n["services"], n["hosts"])
        sev = str(n.get("severity") or "").lower()
        title = str(n.get("title") or "untitled alert")
        items.append((key, at, payload(
            key=key, title=f"Alert ({args.source}): {title}",
            body_lines=[f"**{args.source}** alert `{n['fp']}` ({n.get('status') or 'firing'}, severity {sev or '?'}).",
                        f"Link: {n.get('url') or '?'}",
                        str(n.get("detail") or "")],
            system=system, label="alert", url=n.get("url"), at=at, source=args.source, allow=allow,
            priority="high" if sev in SEVERITY_HIGH else "medium",
            extra={"fingerprint": redact(n["fp"]),
                   "service": redact(next((s for s in n["services"] if s), "")) or None})))
    fresh, dup, cursor = select(items, since, seen, _dt.timedelta(hours=args.lookback))
    return {"cursor": cursor, "issues": fresh, "duplicates": dup, "polled": total,
            "warnings": sorted(set(warnings))}


# --------------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="intake.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--since", default="", help="cursor from `state.py get routines.<id>.cursor` (empty = last 24h)")
    common.add_argument("--kb", default=os.environ.get("KB_DIR") or os.path.expanduser("~/workspaces/knowledge"),
                        help="KB checkout for the repo/service -> system map")
    common.add_argument("--allowlist", help="JSON allowlist; only exact system+label matches get auto_run: true")
    common.add_argument("--lookback", type=float, default=LOOKBACK_H,
                        help=f"hours re-read before the cursor mark (default {LOOKBACK_H})")
    c = sub.add_parser("ci", parents=[common], help="failed GitHub Actions runs -> ci-failure payloads")
    c.add_argument("--org", required=True)
    c.add_argument("--graph", default=".agents/graph.json")
    c.add_argument("--state", default=os.environ.get("FACTORY_STATE") or ".agents/factory-state.json")
    c.add_argument("--repos", help="extra comma-separated owner/name repos")
    c.add_argument("--jobs", type=int, default=8)
    c.add_argument("--default-branch-only", action="store_true",
                   help="ignore failures on branches other than the graph's default_branch")
    a = sub.add_parser("alerts", parents=[common], help="monitoring export on stdin -> alert payloads")
    a.add_argument("--source", required=True, choices=SOURCES)
    return p


def main(argv=None, stdin=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        out = cmd_ci(args) if args.cmd == "ci" else cmd_alerts(args, stdin or sys.stdin)
    except UsageError as e:
        print(f"intake.py: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=1))
    print(f"cursor: {out['cursor'].split(';')[0]} ({len(out['issues'])} new, {out['duplicates']} duplicate)",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
