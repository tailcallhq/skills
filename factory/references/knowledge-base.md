# Knowledge base

> This file currently documents only the `graph.json` schema. The knowledge-base
> layout, fact provenance and precedence rules are added in a later issue.

## `graph.json` (repo graph)

Produced by `scripts/repo_graph.py` from local checkouts and/or the GitHub API. It is the machine-readable
input that the knowledge-base phase turns into `systems/*.md` and `connections.md`.
Regenerate it at any time: scanning is deterministic, and `--jobs N` output is
identical to `--jobs 1` output apart from `generated_at`.

```
repo_graph.py <path|owner/name>... --org <org> [--activity] [--jobs 8] [--out graph.json] [--refresh]
repo_graph.py --org <org> [--max-repos 50] [--workspaces ~/workspaces] [--jobs 8] [--out graph.json]
```

- `<path>` scans a local checkout and makes no network calls, unless `--activity` is passed.
- `owner/name` (anything that is not an existing path) is scanned via `gh api` without
  cloning. A single GraphQL query fetches the manifests, `.gitmodules`,
  Dockerfile/compose and `.github/workflows/*` at HEAD, and the same edge detection
  runs on that text.
- `--org` with no targets, or with `--max-repos N`, enumerates the org's
  non-archived, non-fork repos, most recently pushed first (default cap 50). Each
  enumerated repo uses `<workspaces>/<name>` when that checkout exists; otherwise it
  is scanned via gh.
- `--activity` adds gh metadata and `activity` to local checkouts.
- The gh code lives in `scripts/repo_graph_gh.py` and is imported only when needed.
  Call counts are in `references/parallelism.md`.

```json
{
  "version": 1,
  "generated_at": "2026-09-30T07:30:00+00:00",
  "repos": [
    {
      "full_name": "acme/api",
      "path": "/home/me/workspaces/api",
      "default_branch": "main",
      "languages": ["Rust"],
      "manifests": [ { "file": "Cargo.toml", "kind": "cargo", "deps": ["serde", "tokio"] } ],
      "ports": [8080],
      "errors": [],
      "topics": ["backend"],
      "activity": {
        "open_prs": 4, "merged_prs_30d": 12,
        "top_contributors": [ { "login": "alice", "contributions": 310 } ],
        "active_branches_14d": [ { "name": "main", "last_commit": "2026-09-29T10:00:00Z" } ]
      }
    }
  ],
  "edges": [
    { "from": "acme/web", "to": "acme/api", "kind": "url", "protocol": "ws",
      "evidence": "config/app.json:2", "source": "repo_graph" }
  ],
  "url_refs": [
    { "from": "acme/web", "scheme": "ws", "host": "localhost", "port": 8080,
      "evidence": "config/app.json:2" }
  ],
  "api_calls": 2,
  "rate_limited": false
}
```

`api_calls` (total gh calls made by this run) and `rate_limited` (true if any backoff
happened) are present only when gh was used. A top-level `errors[]` records failures
of org enumeration.

### `repos[]`

| field | required | meaning |
|---|---|---|
| `full_name` | yes | `owner/name` from `remote.origin.url`; if there is no GitHub remote, falls back to `<org>/<dirname>` |
| `path` | no | absolute local checkout path; `null` for repos scanned via gh |
| `source` | no | `gh api` when the repo was scanned remotely (no checkout) |
| `default_branch` | no | from gh when it was used, otherwise `origin/HEAD`; omitted when unknown |
| `languages` | yes | from GitHub linguist (by bytes) when gh was used, otherwise by local file count; highest first |
| `manifests` | yes | `{file, kind: cargo\|npm\|pyproject\|go, deps: [direct dep names]}` |
| `ports` | yes | ports the repo exposes: Dockerfile `EXPOSE`, compose `ports:`, listen defaults in non-test source |
| `errors` | yes | per-repo scan and gh problems (such as `gh api graphql: HTTP 403 ...`); one bad repo never fails the run |
| `topics` | no | GitHub topics (gh only) |
| `archived` | no | GitHub archived flag (gh only) |
| `activity` | no | gh only: `open_prs`, `merged_prs_30d`, `top_contributors[]` (top 5 `{login, contributions}`), `active_branches_14d[]` (`{name, last_commit}`, taken from the 100 most recently committed branches) |

### `edges[]`

| field | required | meaning |
|---|---|---|
| `from` | yes | `full_name` of the repo containing the evidence |
| `to` | yes | `full_name`, or `external:<host>` (reserved; not emitted yet) |
| `kind` | yes | `submodule` (`.gitmodules` url), `manifest` (`github.com/<org>/x` in a manifest), `workflow_uses` (`uses: <org>/x` in `.github/workflows`), `image` (`image:`/`FROM` `<org>/x`), `url` (`ws/http(s)://host:port` in config or non-test source where the port is exposed by another scanned repo) |
| `protocol` | no | URL scheme, for `url` edges only |
| `evidence` | yes | `file:line`, relative to the `from` repo root |
| `source` | yes | always `repo_graph` for this script; other producers use their own value |

`url_refs[]` holds the raw URL hits that `url` edges are resolved from. They are kept
so that a later incremental run, which scans a new repo exposing the port, can
resolve edges without rescanning the referring repo.

### Merge semantics (`--out`)

- Repos already present in `--out` are kept as they are and not rescanned, unless
  `--refresh` is passed.
- For every rescanned repo, its old edges and `url_refs` are dropped and replaced.
- `url` edges are recomputed on every run from the full `url_refs` and `ports` sets.
- The write is atomic (tmp file + rename).

Per-repo timing is printed to stderr.

Remote scans do not walk the full tree. They collect no source-file listen ports or
URL refs, and do not read nested manifests. Edges still come from root manifests,
`.gitmodules`, Dockerfile/compose and workflows. For full coverage, clone the repo into
`~/workspaces`.
