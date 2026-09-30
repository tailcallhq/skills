"""gh api layer for repo_graph.py (imported lazily; stdlib only; no tokens handled here).

All calls go through `gh api --include` so rate-limit headers are visible.
Failures are returned as (None, error_string) and recorded in the repo's errors[].
"""
import json
import os
import subprocess
import sys
import threading
import time

LOW_WATER = 50          # same rule as detect_tracker.sh
CALLS_PER_REPO = 2     # 1 GraphQL (metadata+activity+files) + 1 REST (contributors)
MAX_SLEEP = 60          # never block longer than this per backoff


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def parse_include(text):
    """Split `gh api --include` output into (status, headers, body_text)."""
    status, headers = 0, {}
    head, sep, body = text.partition("\r\n\r\n")
    if not sep:
        head, sep, body = text.partition("\n\n")
    lines = head.splitlines()
    if lines and lines[0].startswith("HTTP/"):
        parts = lines[0].split()
        status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        for line in lines[1:]:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    else:
        body = text
    return status, headers, body


class Gh:
    """Thread-safe `gh api` wrapper with rate-limit backoff and call counting."""

    def __init__(self, gh="gh", low_water=LOW_WATER, max_sleep=MAX_SLEEP, sleep=time.sleep):
        self.gh, self.low_water, self.max_sleep, self.sleep = gh, low_water, max_sleep, sleep
        self.remaining, self.reset = None, None
        self.calls, self.backoffs = 0, 0
        self._lock = threading.Lock()

    def _maybe_backoff(self):
        with self._lock:
            rem, reset = self.remaining, self.reset
        if rem is None or rem >= self.low_water:
            return
        wait = max(0, min(self.max_sleep, (reset or time.time()) - time.time()))
        log(f"[repo_graph] gh rate limit low ({rem} remaining); backing off {wait:.0f}s")
        with self._lock:
            self.backoffs += 1
        self.sleep(wait)

    def api(self, endpoint, *args):
        """Return (data, None) or (None, "error"). Never raises."""
        self._maybe_backoff()
        cmd = [self.gh, "api", "--include", *args, endpoint]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                               env={**os.environ, "GH_PROMPT_DISABLED": "1"})
        except (OSError, subprocess.SubprocessError) as exc:
            return None, f"gh api {endpoint}: {exc}"
        status, headers, body = parse_include(p.stdout)
        with self._lock:
            self.calls += 1
            if "x-ratelimit-remaining" in headers:
                try:
                    self.remaining = int(headers["x-ratelimit-remaining"])
                    self.reset = int(headers.get("x-ratelimit-reset", 0)) or None
                except ValueError:
                    pass
        if p.returncode != 0 or status >= 400:
            try:
                j = json.loads(body)
                msg = j.get("message") or ((j.get("errors") or [{}])[0].get("message", ""))
            except (ValueError, AttributeError):
                tail = (p.stderr or body).strip().splitlines()
                msg = tail[-1] if tail else ""
            code = f"HTTP {status}" if status and status != 200 else "error"
            return None, f"gh api {endpoint}: {code} {msg}".strip()
        try:
            data = json.loads(body) if body.strip() else None
        except ValueError as exc:
            return None, f"gh api {endpoint}: bad JSON: {exc}"
        if isinstance(data, dict) and data.get("errors") and not data.get("data"):
            return None, f"gh api {endpoint}: {data['errors'][0].get('message', 'graphql error')}"
        return data, None


# ---------------------------------------------------------------- queries

# Files fetched for remote (no-checkout) scans: alias -> path at HEAD.
REMOTE_FILES = {"cargo": "Cargo.toml", "npm": "package.json", "pyproject": "pyproject.toml",
                "gomod": "go.mod", "gitmodules": ".gitmodules", "dockerfile": "Dockerfile",
                "compose1": "docker-compose.yml", "compose2": "docker-compose.yaml",
                "compose3": "compose.yml", "compose4": "compose.yaml"}
_BLOB = "... on Blob{text isBinary}"
REPO_QUERY = """query($owner:String!,$name:String!,$merged:String!){
 repository(owner:$owner,name:$name){
  nameWithOwner isArchived defaultBranchRef{name}
  languages(first:20,orderBy:{field:SIZE,direction:DESC}){nodes{name}}
  repositoryTopics(first:20){nodes{topic{name}}}
  pullRequests(states:OPEN){totalCount}
  refs(refPrefix:"refs/heads/",first:100,orderBy:{field:TAG_COMMIT_DATE,direction:DESC}){
   nodes{name target{... on Commit{committedDate}}}}
  %FILES%
  workflows:object(expression:"HEAD:.github/workflows"){... on Tree{entries{name object{%BLOB%}}}}
 }
 merged:search(query:$merged,type:ISSUE,first:1){issueCount}
}""".replace("%FILES%", "\n  ".join(
    f'{a}:object(expression:"HEAD:{p}"){{{_BLOB}}}' for a, p in REMOTE_FILES.items())
).replace("%BLOB%", _BLOB)
ORG_QUERY = """query($org:String!,$n:Int!,$cursor:String){
 repositoryOwner(login:$org){repositories(first:$n,after:$cursor,orderBy:{field:PUSHED_AT,direction:DESC}){
  pageInfo{hasNextPage endCursor} nodes{nameWithOwner isArchived isFork}}}}"""


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_repo(gh, full_name, now=None):
    """ONE GraphQL query + ONE REST call (contributors). Returns (info, files, errors).

    info: default_branch, languages, topics, activity{...}. files: {path: text} fetched
    at HEAD (manifests, .gitmodules, Dockerfile/compose, .github/workflows/*).
    """
    import datetime as dt
    now = now or dt.datetime.now(dt.timezone.utc)
    owner, name = full_name.split("/", 1)
    merged_q = f"repo:{full_name} is:pr is:merged merged:>={(now - dt.timedelta(days=30)).date()}"
    errors, info, files = [], {}, {}
    data, err = gh.api("graphql", "-f", f"query={REPO_QUERY}", "-f", f"owner={owner}",
                       "-f", f"name={name}", "-f", f"merged={merged_q}")
    repo = ((data or {}).get("data") or {}).get("repository")
    if err or not repo:
        errors.append(err or f"gh graphql {full_name}: repository not found")
    else:
        cutoff = _iso(now - dt.timedelta(days=14))
        branches = [{"name": n["name"], "last_commit": (n.get("target") or {}).get("committedDate")}
                    for n in (repo.get("refs") or {}).get("nodes", [])]
        info = {"default_branch": (repo.get("defaultBranchRef") or {}).get("name"),
                "languages": [n["name"] for n in repo["languages"]["nodes"]],
                "topics": sorted(n["topic"]["name"] for n in repo["repositoryTopics"]["nodes"]),
                "archived": repo.get("isArchived", False),
                "activity": {
                    "open_prs": repo["pullRequests"]["totalCount"],
                    "merged_prs_30d": ((data["data"].get("merged") or {}).get("issueCount")),
                    "active_branches_14d": sorted(
                        (b for b in branches if (b["last_commit"] or "") >= cutoff),
                        key=lambda b: (b["last_commit"], b["name"]), reverse=True),
                    "top_contributors": []}}
        for alias, path in REMOTE_FILES.items():
            blob = repo.get(alias)
            if blob and not blob.get("isBinary") and blob.get("text") is not None:
                files[path] = blob["text"]
        for e in ((repo.get("workflows") or {}).get("entries") or []):
            blob = e.get("object") or {}
            if e["name"].endswith((".yml", ".yaml")) and blob.get("text") is not None:
                files[f".github/workflows/{e['name']}"] = blob["text"]
    if not info:
        return info, files, errors
    contrib, err = gh.api(f"repos/{full_name}/contributors?per_page=5")
    if err:
        errors.append(err)
    else:
        info["activity"]["top_contributors"] = [
            {"login": c.get("login"), "contributions": c.get("contributions")}
            for c in (contrib or [])[:5]]
    return info, files, errors


def list_org(gh, org, max_repos):
    """Enumerate non-archived, non-fork repos, most recently pushed first. ceil(N/100) calls."""
    out, cursor, errors = [], None, []
    while len(out) < max_repos:
        args = ["-f", f"query={ORG_QUERY}", "-f", f"org={org}", "-F", f"n={min(100, max_repos)}"]
        if cursor:
            args += ["-f", f"cursor={cursor}"]
        data, err = gh.api("graphql", *args)
        conn = (((data or {}).get("data") or {}).get("repositoryOwner") or {}).get("repositories")
        if err or not conn:
            errors.append(err or f"gh graphql: owner {org} not found")
            break
        out += [n["nameWithOwner"] for n in conn["nodes"]
                if not n.get("isArchived") and not n.get("isFork")]
        if not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]
    return out[:max_repos], errors


# ---------------------------------------------------------------- repo records

def add_activity(gh, repo):
    """--activity on a local checkout: merge gh metadata into the record (files unused)."""
    info, _files, errors = fetch_repo(gh, repo["full_name"])
    repo["errors"] += errors
    _merge(repo, info)
    return repo


def _merge(repo, info):
    for k in ("default_branch", "topics", "activity"):
        if info.get(k) is not None:
            repo[k] = info[k]
    if info.get("languages"):
        repo["languages"] = info["languages"]  # GitHub linguist beats extension counts


def scan_remote(gh, full_name, org, scan_files):
    """No checkout: fetch manifests/workflows/.gitmodules via one GraphQL query and run the
    same edge detection on the fetched text. Returns (repo, edges, url_refs)."""
    repo = {"full_name": full_name, "path": None, "source": "gh api", "languages": [],
            "manifests": [], "ports": [], "errors": []}
    info, files, errors = fetch_repo(gh, full_name)
    repo["errors"] += errors
    _merge(repo, info)
    items = [(rel, (lambda t=text: t.splitlines())) for rel, text in sorted(files.items())]
    return scan_files(repo, org, items)
