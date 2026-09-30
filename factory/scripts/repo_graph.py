#!/usr/bin/env python3
"""Build a cross-repo dependency graph (graph.json) from local checkouts and/or gh api.

Usage:
    repo_graph.py <path|owner/name>... --org <org> [--jobs N] [--out graph.json] [--refresh]
    repo_graph.py --org <org> [--max-repos 50] [--workspaces ~/workspaces] ...   (enumerate org)
    add --activity to fetch gh metadata/activity for local checkouts too.

Schema: see factory/references/knowledge-base.md ("graph.json").
Local paths without --activity make no network calls. The gh layer lives in
repo_graph_gh.py and is imported only when needed. Stdlib only.
"""
import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

VERSION = 1
SKIP_DIRS = {".git", "node_modules", "target", "vendor", "dist", "build", ".venv",
             "venv", "__pycache__", ".forge", ".next", ".cache"}
MAX_FILE = 1_000_000
MANIFESTS = {"Cargo.toml": "cargo", "package.json": "npm",
             "pyproject.toml": "pyproject", "go.mod": "go"}
CONFIG_EXT = {".json", ".yaml", ".yml", ".toml", ".env", ".ini", ".conf", ".cfg",
              ".properties"}
LANG_EXT = {".rs": "Rust", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript",
            ".jsx": "JavaScript", ".py": "Python", ".go": "Go", ".java": "Java",
            ".kt": "Kotlin", ".rb": "Ruby", ".swift": "Swift", ".c": "C", ".cpp": "C++",
            ".cs": "C#", ".sh": "Shell"}
COMMENT_RE = re.compile(r"^\s*(//|/?\*|#(?!!)|--|<!--)")
URL_RE = re.compile(r"\b(wss?|https?)://([A-Za-z0-9_.\-]+):(\d{2,5})")
EXPOSE_RE = re.compile(r"^\s*EXPOSE\s+(.+)$", re.I)
# Listen defaults in source: `const DEFAULT_PORT: u16 = 9753`, `default_value = "127.0.0.1:9753"`.
LISTEN_RE = re.compile(r"\bPORT\b[^=\n]{0,20}=\s*['\"]?(\d{2,5})\b|"
                       r"(?:bind|listen|addr|default_value)\w*\W{1,6}(?:0\.0\.0\.0|127\.0\.0\.1|localhost|\[::\]):(\d{2,5})", re.I)
TEST_RE = re.compile(r"(^|/)(tests?|__tests__|fixtures|testdata|e2e)/|[._-](test|spec)\.\w+$|_test\.go$")
COMPOSE_PORT_RE = re.compile(r"^\s*-\s*['\"]?(?:[\d.]+:)?(\d{2,5})(?::(\d{2,5}))?(?:/\w+)?['\"]?\s*$")


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def read_lines(p):
    try:
        big = p.stat().st_size > MAX_FILE
        return [] if big else p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def walk(root):
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        dirnames[:] = sorted(d for d in dirnames
                             if d not in SKIP_DIRS and not (d.startswith(".") and d not in (".github",)))
        for f in sorted(filenames):
            yield Path(dirpath) / f, (f if rel_dir == "." else f"{rel_dir}/{f}").replace(os.sep, "/")


def git(path, *args):
    if not (path / ".git").exists():
        return ""
    try:
        return subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def full_name_for(path, org):
    m = re.search(r"github\.com[:/]([\w.\-]+/[\w.\-]+?)(?:\.git)?/?$",
                  git(path, "config", "--get", "remote.origin.url"))
    return m.group(1) if m else f"{org}/{path.resolve().name}"


def default_branch(path):
    ref = git(path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    return ref.split("/", 1)[1] if "/" in ref else None


# ---------------------------------------------------------------- manifests

def _name(spec):
    m = re.match(r"\s*([A-Za-z0-9_.\-@/]+)", spec)
    return m.group(1) if m else None


def parse_cargo(lines):
    deps, section = [], ""
    for line in lines:
        s = line.split("#", 1)[0].strip()
        m = re.match(r"^\[(.+)\]$", s)
        if m:
            section = m.group(1).strip()
            sub = re.match(r"^(?:.*\.)?(?:dev-|build-)?dependencies\.([\w\-]+)$", section)
            if sub:
                deps.append(sub.group(1))
                section = ""
            continue
        if re.search(r"(^|\.)(dev-|build-)?dependencies$", section):
            m = re.match(r"^([A-Za-z0-9_\-]+)\s*=", s)
            if m:
                deps.append(m.group(1))
    return deps


def parse_npm(lines):
    try:
        data = json.loads("\n".join(lines))
    except ValueError:
        return []
    deps = []
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        if isinstance(data.get(key), dict):
            deps += list(data[key])
    return deps


def parse_pyproject(lines):
    deps, section, in_array = [], "", False
    for line in lines:
        s = line.split("#", 1)[0].strip()
        m = re.match(r"^\[(.+)\]$", s)
        if m and not in_array:
            section = m.group(1).strip()
            continue
        if in_array:
            for q in re.findall(r"['\"]([^'\"]+)['\"]", s):
                deps.append(_name(q))
            if "]" in s:
                in_array = False
            continue
        if section == "project" and re.match(r"^dependencies\s*=", s) \
                or section == "project.optional-dependencies" and "=" in s:
            rhs = s.split("=", 1)[1]
            for q in re.findall(r"['\"]([^'\"]+)['\"]", rhs):
                deps.append(_name(q))
            in_array = "[" in rhs and "]" not in rhs
        elif re.match(r"^tool\.poetry\.(?:.*\.)?dependencies$", section):
            m = re.match(r"^([A-Za-z0-9_.\-]+)\s*=", s)
            if m and m.group(1) != "python":
                deps.append(m.group(1))
    return [d for d in deps if d]


def parse_go(lines):
    deps, block = [], False
    for line in lines:
        s = line.split("//", 1)[0].strip()
        if s.startswith("require ("):
            block = True
        elif block and s == ")":
            block = False
        elif block and s:
            deps.append(s.split()[0])
        elif s.startswith("require "):
            deps.append(s.split()[1])
    return deps


PARSERS = {"cargo": parse_cargo, "npm": parse_npm, "pyproject": parse_pyproject, "go": parse_go}


# ---------------------------------------------------------------- scanning

def scan_repo(path, org):
    """Scan one checkout. Returns (repo_record, edges, url_refs)."""
    path = Path(path).expanduser()
    fn = full_name_for(path, org)
    repo = {"full_name": fn, "path": str(path.resolve()), "languages": [], "manifests": [],
            "ports": [], "errors": []}
    repo.update({"default_branch": b} if (b := default_branch(path)) else {})
    if not path.is_dir():
        repo["errors"].append(f"not a directory: {path}")
        return repo, [], []
    return scan_files(repo, org, ((rel, lambda fp=fp: read_lines(fp)) for fp, rel in walk(path)))


def scan_files(repo, org, files):
    """Edge/port/url detection over (rel_path, lines_thunk) pairs. Mutates + returns repo."""
    fn = repo["full_name"]
    o = re.escape(org)
    gh_re = re.compile(rf"github\.com[/:]{o}/([A-Za-z0-9_.\-]+)", re.I)
    uses_re = re.compile(rf"^\s*-?\s*uses:\s*['\"]?{o}/([A-Za-z0-9_.\-]+)", re.I)
    image_re = re.compile(rf"^\s*(?:-\s*)?(?:image:|FROM\s)\s*['\"]?(?:--\S+\s+)*"
                          rf"(?:[A-Za-z0-9.\-]+(?::\d+)?/)?{o}/([A-Za-z0-9_.\-]+)", re.I)
    edges, url_refs, langs, ports = [], [], {}, set()

    def edge(name, kind, ev):
        target = f"{org}/{name[:-4] if name.endswith('.git') else name}"
        if target.lower() != fn.lower():
            edges.append({"from": fn, "to": target, "kind": kind, "evidence": ev,
                          "source": "repo_graph"})

    for rel, get_lines in files:
        base = rel.rsplit("/", 1)[-1]
        ext = os.path.splitext(base)[1].lower()
        if ext in LANG_EXT:
            langs[LANG_EXT[ext]] = langs.get(LANG_EXT[ext], 0) + 1
        is_manifest = base in MANIFESTS
        is_gitmodules = base == ".gitmodules"
        is_workflow = rel.startswith(".github/workflows/") and ext in (".yml", ".yaml")
        is_docker = base.startswith("Dockerfile") or base.endswith(".Dockerfile")
        is_compose = "compose" in base.lower() and ext in (".yml", ".yaml")
        is_config = ext in CONFIG_EXT or base.startswith(".env")
        is_source = ext in LANG_EXT and not TEST_RE.search(rel)
        if not any((is_manifest, is_gitmodules, is_workflow, is_docker, is_compose, is_config, is_source)):
            continue
        lines = get_lines()
        if is_manifest:
            kind = MANIFESTS[base]
            try:
                deps = PARSERS[kind](lines)
            except Exception as exc:  # noqa: BLE001 - record, never fail the scan
                deps = []
                repo["errors"].append(f"{rel}: {exc}")
            repo["manifests"].append({"file": rel, "kind": kind, "deps": sorted(set(deps))})
        in_ports = False
        for i, line in enumerate(lines, 1):
            ev = f"{rel}:{i}"
            if is_gitmodules:
                m = re.match(r"^\s*url\s*=", line) and gh_re.search(line)
                if m:
                    edge(m.group(1), "submodule", ev)
                continue
            if is_manifest:
                for m in gh_re.finditer(line):
                    edge(m.group(1), "manifest", ev)
            if is_workflow:
                m = uses_re.search(line)
                if m:
                    edge(m.group(1), "workflow_uses", ev)
            if is_docker or is_compose or is_workflow or ext in (".yml", ".yaml"):
                m = image_re.search(line)
                if m:
                    edge(m.group(1).split(":")[0].split("@")[0], "image", ev)
            if is_docker:
                m = EXPOSE_RE.match(line)
                if m:
                    ports.update(int(p) for p in re.findall(r"\d+", m.group(1)))
            if is_compose:
                if re.match(r"^\s*ports:\s*$", line):
                    in_ports = True
                    continue
                if in_ports:
                    m = COMPOSE_PORT_RE.match(line)
                    if m:
                        ports.update(int(p) for p in m.groups() if p)
                        continue
                    in_ports = False
            if is_source:
                m = LISTEN_RE.search(line)
                if m:
                    ports.add(int(m.group(1) or m.group(2)))
            if (is_config or is_source) and not is_manifest and not TEST_RE.search(rel) \
                    and not COMMENT_RE.match(line):
                for m in URL_RE.finditer(line):
                    url_refs.append({"from": fn, "scheme": m.group(1), "host": m.group(2),
                                     "port": int(m.group(3)), "evidence": ev})
    repo["languages"] = repo["languages"] or sorted(langs, key=lambda k: (-langs[k], k))
    repo["ports"] = sorted(ports)
    repo["manifests"].sort(key=lambda m: m["file"])
    return repo, edges, url_refs


def resolve_urls(url_refs, repos):
    """url edges: a config URL whose port is exposed by another scanned repo."""
    by_port = {}
    for r in repos:
        for p in r.get("ports", []):
            by_port.setdefault(p, []).append(r["full_name"])
    out = []
    for u in url_refs:
        for target in by_port.get(u["port"], []):
            if target != u["from"]:
                out.append({"from": u["from"], "to": target, "kind": "url",
                            "protocol": u["scheme"], "evidence": u["evidence"],
                            "source": "repo_graph"})
    return out


def edge_key(e):
    return (e["from"], e["to"], e["kind"], e["evidence"], e.get("protocol") or "")


def build(paths, org, jobs=8, existing=None, refresh=False, remote=(), activity=False, gh=None):
    """paths: local checkouts; remote: owner/name scanned via gh api (no clone)."""
    existing = existing or {}
    old_repos = {r["full_name"]: r for r in existing.get("repos", [])}
    items = [(p, full_name_for(Path(p).expanduser(), org), False) for p in paths] + \
            [(fn, fn, True) for fn in remote]
    todo = []
    for item in items:
        if item[1] in old_repos and not refresh:
            log(f"[repo_graph] {item[1]}: kept from existing graph (use --refresh to rescan)")
        else:
            todo.append(item)
    if gh is None and (activity and todo or any(r for _, _, r in todo)):
        import repo_graph_gh
        gh = repo_graph_gh.Gh()

    def scan(item):
        target, fn, is_remote = item
        if is_remote:
            import repo_graph_gh
            return repo_graph_gh.scan_remote(gh, fn, org, scan_files)
        res = scan_repo(target, org)
        if activity:
            import repo_graph_gh
            repo_graph_gh.add_activity(gh, res[0])
        return res

    def timed(item):
        t0 = time.monotonic()
        res = scan(item)
        log(f"[repo_graph] {res[0]['full_name']}: {time.monotonic() - t0:.2f}s, "
            f"{len(res[1])} edges, {len(res[2])} url refs")
        return res

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        results = list(pool.map(timed, todo))
    scanned = {r["full_name"] for r, _, _ in results}
    repos = {k: v for k, v in old_repos.items() if k not in scanned}
    # url edges are recomputed from url_refs every run (a new repo may expose the port)
    edges = [e for e in existing.get("edges", []) if e.get("from") not in scanned and e.get("kind") != "url"]
    url_refs = [u for u in existing.get("url_refs", []) if u["from"] not in scanned]
    for repo, es, us in results:
        repos[repo["full_name"]] = repo
        edges += es
        url_refs += us
    repo_list = sorted(repos.values(), key=lambda r: r["full_name"])
    edges += resolve_urls(url_refs, repo_list)
    uniq = {edge_key(e): e for e in edges}
    graph = {"version": VERSION,
             "generated_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
             "repos": repo_list,
             "edges": [uniq[k] for k in sorted(uniq)],
             "url_refs": sorted(url_refs, key=lambda u: (u["from"], u["evidence"], u["port"]))}
    if gh is not None:
        graph["api_calls"] = gh.calls
        graph["rate_limited"] = gh.backoffs > 0
    return graph


REMOTE_RE = re.compile(r"^[\w.\-]+/[\w.\-]+$")


def split_targets(targets, org_repos, workspaces):
    """-> (local_paths, remote_full_names). owner/name that is not an existing path is remote;
    enumerated org repos use ~/workspaces/<name> when that checkout exists."""
    local, remote = [], []
    for t in targets:
        (remote if REMOTE_RE.match(t) and not os.path.exists(t) else local).append(t)
    for fn in org_repos:
        ws = Path(workspaces).expanduser() / fn.split("/", 1)[1]
        if ws.is_dir():
            local.append(str(ws))
        elif fn not in remote:
            remote.append(fn)
    return list(dict.fromkeys(local)), remote


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="*", help="local checkouts or owner/name (scanned via gh api)")
    ap.add_argument("--org", required=True, help="GitHub org/owner used to recognise cross-repo refs")
    ap.add_argument("--max-repos", type=int, help="enumerate --org, at most N repos (default 50 "
                    "when no targets are given)")
    ap.add_argument("--workspaces", default="~/workspaces", help="where enumerated checkouts live")
    ap.add_argument("--activity", action="store_true", help="gh metadata+activity for local paths too")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--out", help="graph.json to write/merge (default: stdout)")
    ap.add_argument("--refresh", action="store_true", help="rescan repos already in --out")
    a = ap.parse_args(argv)
    gh, org_repos, org_errors = None, [], []
    if not a.paths or a.max_repos is not None:
        import repo_graph_gh
        gh = repo_graph_gh.Gh()
        cap = 50 if a.max_repos is None else a.max_repos
        org_repos, org_errors = repo_graph_gh.list_org(gh, a.org, cap)
        log(f"[repo_graph] {a.org}: {len(org_repos)} repos enumerated (cap {cap})")
    local, remote = split_targets(a.paths, org_repos, a.workspaces)
    existing = {}
    if a.out and os.path.exists(a.out):
        try:
            with open(a.out, encoding="utf-8") as fh:
                existing = json.load(fh)
        except ValueError as exc:
            log(f"[repo_graph] ignoring unreadable {a.out}: {exc}")
    graph = build(local, a.org, a.jobs, existing, a.refresh, remote, a.activity, gh)
    if org_errors:
        graph["errors"] = org_errors
    text = json.dumps(graph, indent=2) + "\n"
    if a.out:
        tmp = a.out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, a.out)
        log(f"[repo_graph] wrote {a.out}: {len(graph['repos'])} repos, {len(graph['edges'])} edges")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
