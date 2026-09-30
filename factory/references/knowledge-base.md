# Knowledge base

> This file currently documents only the `graph.json` schema. The knowledge-base
> layout, fact provenance and precedence rules are added in a later issue.

## `graph.json` (repo graph)

Produced by `scripts/repo_graph.py` from local checkouts. It is the machine-readable
input that the knowledge-base phase turns into `systems/*.md` and `connections.md`.
Regenerate it at any time: scanning is deterministic, and `--jobs N` output is
identical to `--jobs 1` output apart from `generated_at`.

```
repo_graph.py <path>... --org <org> [--jobs 8] [--out graph.json] [--refresh]
```

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
      "errors": []
    }
  ],
  "edges": [
    { "from": "acme/web", "to": "acme/api", "kind": "url", "protocol": "ws",
      "evidence": "config/app.json:2", "source": "repo_graph" }
  ],
  "url_refs": [
    { "from": "acme/web", "scheme": "ws", "host": "localhost", "port": 8080,
      "evidence": "config/app.json:2" }
  ]
}
```

### `repos[]`

| field | required | meaning |
|---|---|---|
| `full_name` | yes | `owner/name` from `remote.origin.url`; if there is no GitHub remote, falls back to `<org>/<dirname>` |
| `path` | no | absolute local checkout path |
| `default_branch` | no | from `origin/HEAD`; omitted when unknown |
| `languages` | yes | ordered by file count, highest first |
| `manifests` | yes | `{file, kind: cargo\|npm\|pyproject\|go, deps: [direct dep names]}` |
| `ports` | yes | ports the repo exposes: Dockerfile `EXPOSE`, compose `ports:`, listen defaults in non-test source |
| `errors` | yes | per-repo scan problems; one bad repo never fails the run |

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

Scope: local checkouts only. Org enumeration, `gh api` metadata and activity
metrics are added by `repo_graph.py` in a later issue.
