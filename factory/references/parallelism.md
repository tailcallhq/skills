# factory parallelism: fan-out, model tiers, rate limits, merge

Factory must feel fast on a 20-50 repo org. Every per-repo step is **fan-out
work**. The orchestrator never loops over repos one at a time. This file is the
contract between the orchestrator (SKILL.md), `scripts/state.py`,
`scripts/fanout.py` and `workflows/factory-fanout.js`.

## Decision rule

Take N from the pending set, never from the full org:

```bash
python3 scripts/fanout.py args --stage <bootstrap|research> --workspace ~/workspaces \
  --kb ~/workspaces/knowledge --mark > /tmp/fanout-args.json   # repos = state.py pending-repos <stage> --ready
jq -r .route /tmp/fanout-args.json                             # none | task-batch | workflow
```

| Pending repos N | Path | How |
|---|---|---|
| 0 | none | Stage already complete, so move to the next phase. |
| 1-3 | `task-batch` | **One** `Task` call with N entries in `tasks` and `model: fast`. Each sub-agent runs every stage in its repo's `todo` (same prompts and schemas as the workflow) and returns one `RepoResult`. Then `fanout.py merge`. |
| >= 4 | `workflow` | `workflow_run_file` with `path: <skill>/workflows/factory-fanout.js` and `args` = the JSON above. The merged report lands in the file named by its `reportFile`. |

Never use a sequential loop, and never send one `Task` call per repo. If a
`Task` batch or workflow fails outright, fall back to the other path. Do not
fall back to a loop.

`workflow_run_file` runs at most `min(16, cpus - 2)` `tools.*` calls at once. More
repos than that is fine because extra `tools.task` calls in the `Promise.all`
queue for a slot. The run takes about `ceil(N / cap)` times the per-repo
latency.

## Stages and the one barrier

`args.stage` selects the repo stages (`state.py` `STAGES`) that one run owns:

| Run | Phase | Per-repo stages (pipeline, no barrier) | Barrier |
|---|---|---|---|
| `bootstrap` | 2 | `clone` (clone or verify an existing checkout) | none |
| `research` | 3 | `survey` (repo-setup style, read-only) -> `graph` (`repo_graph.py`) -> `draft` (KB system file) | one `intelligent` agent after all drafts reconciles cross-repo edges into `connections.md` candidates |

A repo can be in `survey` while another is already in `draft`. The connections
agent sees every draft from this run plus `contextDrafts`, which holds drafts of
repos finished in earlier runs (read from artifacts). This lets a resumed run
reconcile the whole org, not only the tail.

## Model tier policy

Tiers are the `model` values that `Task` (and `tools.task` in a workflow) accepts. The host maps a
tier to a concrete model. **Resolve the concrete ids at runtime with
`model_list`.** The ids below are the preferred choices, not the only valid
ones.

| Tier | Preferred ids (first one available) | Used for |
|---|---|---|
| `fast` | `claudecode/claude-haiku-4-5-20251001`, `forgecode/claude-haiku-4-5`, any other Haiku-class id `model_list` reports | All per-repo fan-out |
| `intelligent` | A Sonnet/Opus-class id from `model_list` (a `*claude-sonnet-*` or `*claude-opus-*` id of whichever provider is signed in; prefer Sonnet for cost) | The four exceptions below |
| `expert` | Never used by factory | |

If `model_list` shows no Haiku-class model, use the cheapest model listed for
`fast` and say so in the phase summary. Never block on this.

| Step | Tier |
|---|---|
| clone / verify checkout | `fast` |
| repo-setup survey | `fast` |
| `repo_graph.py` summary | `fast` |
| KB `systems/<system>.md` draft | `fast` |
| tracker / monitoring / infra detection (`detect_*.sh` summary) | `fast` |
| routine bodies (KB refresh, health, triage, intake) | `fast` |
| **KB contradiction reconciliation** (during `kb.py ingest`) | `intelligent` |
| **`connections.md` edge drafting** (the workflow barrier) | `intelligent` |
| **user-facing phase summaries** | `intelligent` |
| **infra change planning / classification** (`references/infra-changes.md`) | `intelligent` |

Every `Task` / `tools.task` call uses `model: 'fast'` unless it is one of the
four bold rows. A tier's reasoning effort is set where the user configures the
tier, not per call.

**Call count, phases 2+3:** there are `N x 4` fast calls (1 clone + 3 research
stages). There are at most 3 `intelligent` calls: 1 connections agent, plus the
phase 2 and phase 3 summaries, which the orchestrator can merge into one. That
stays within the limit of 4. A resumed run adds one more connections call only
if phase 3 is re-entered. In that case, skip the separate phase 2 summary so the
total stays <= 4.

## Rate-limit budget

The `gh api` core limit is **5000 requests/hour** per token. Search is 30/min
and does not count against core. Factory never plans to spend the last 500
(`GH_RESERVE`).

Ceilings for gh core calls per repo (`GH_CALLS` in `fanout.py`):

| Step | Calls/repo | What |
|---|---|---|
| clone | 1 | `gh repo view` for the default branch. `git clone` over https does not count against the REST limit. |
| `repo_graph.py` | 2 | exactly 2 when gh is used for the repo (`owner/name` target, no local checkout, or `--activity`): 1 GraphQL query (cost 1 point, graphql bucket) returning default branch, languages, topics, open PR count, merged-PR count (last 30 days, via `search`), branch heads (to find branches with commits in the last 14 days), and the text of manifests, `.gitmodules`, Dockerfile/compose and `.github/workflows/*`; plus 1 REST core call for the top 5 contributors (GraphQL has no contributors field). A local path without `--activity` makes 0 calls. `--org` enumeration adds ceil(N/100) GraphQL calls per run, not per repo. |
| tracker detection | 4 | issues/projects/labels probes |
| monitoring detection | 4 | files/secrets-names/env probes |
| **total** | **11** | |

`repo_graph.py` must report the calls it actually made as `api_calls` in its
JSON. The `graph` stage copies that value into `RepoGraphSummary.api_calls`, so
the budget can be checked after a run.

Before fanning out:

```bash
python3 scripts/fanout.py plan --repos 50 --remaining "$(gh api rate_limit --jq .resources.core.remaining)"
```

- 50 repos x 11 = 550 calls. That fits in a fresh hour (4500 usable), so use one run.
- With 720 remaining (220 usable), the chunk size is 20. The plan is `[20, 20, 10]`: run the
  first chunk now and the rest after `.resources.core.reset`. Chunk by calling
  `fanout.py args` and truncating `repos` to the chunk size. Everything else
  stays `pending` in state and is picked up by the next run.

`repo_graph.py` reads `X-RateLimit-Remaining`/`-Reset`/`-Resource` from every
`gh api --include` response and tracks core and graphql separately. When a bucket
drops below 50 (the same threshold as `detect_tracker.sh`), the next call on that
bucket sleeps until reset, capped at 60 s per call, and the graph reports
`"rate_limited": true`. A failed call (403, 404, network) is written to that repo's
`errors[]` and the batch continues.

Scripts that hit a 403/429 with `x-ratelimit-remaining: 0` must back off until
reset, or return partial results with `"rate_limited": true`. They must never
crash the fan-out.

## `--jobs N` contract for scripts

Deterministic scripts do their own internal concurrency, so the agents mostly
summarize.

| Script | Contract |
|---|---|
| `repo_graph.py`, `infra_graph.py`, `detect_*.sh` | Accept `--jobs N` (default **8**). Use a bounded thread pool or `xargs -P` with at most N concurrent network requests. Honour rate-limit headers: when `remaining < N * 2`, drop to 1 job. Write output to `--out FILE` or `-` (stdout) as JSON. Idempotent and read-only. |
| `kb.py ingest` | **Single writer.** Only the orchestrator calls it, once per phase, after the merge. Fan-out agents never write to the KB checkout. It takes a lock on the KB checkout and refuses to run concurrently. |
| `state.py` | Safe to call concurrently (file lock plus atomic rename, see `references/state.md`). Only the orchestrator calls it, through `fanout.py record`. |

When several workflow sub-agents each run a script with `--jobs 8`, the effective
concurrency is `cap x 8`. `fanout.py args` passes `jobs` through. Lower it
(`--jobs 2`) when `plan` reports a tight budget.

## Result schemas (merge is mechanical)

Each fan-out stage returns exactly one of these objects. They are enforced as
the `tools.task` `schema` (see `SCHEMAS` in `workflows/factory-fanout.js`) and
must appear verbatim in `Task` prompts. `Edge` = `{to, kind, protocol?,
evidence, source}`. `Fact` = `{text, source}`.

**`RepoClone`** (stage `clone`)
```json
{"repo": "api", "status": "cloned|present|failed", "path": "/abs", "default_branch": "main", "head": "abc1234", "error": "..."}
```

**`RepoSurvey`** (stage `survey`)
```json
{"repo": "api", "kind": "service|web-app|cli|library|monorepo|infra|docs|other", "languages": ["python"],
 "build": ["uv sync"], "test": ["pytest"], "run": [], "ci": [".github/workflows/ci.yml"],
 "guidance_files": ["AGENTS.md"], "missing_prerequisites": [], "summary": "one line"}
```

**`RepoGraphSummary`** (stage `graph`)
```json
{"repo": "api", "source": "repo_graph.py|local-scan", "languages": ["python"],
 "deps": [{"name": "acme-auth-client", "ecosystem": "pypi", "internal": true}],
 "edges": [{"to": "auth", "kind": "dep", "protocol": "http", "evidence": "pyproject.toml:12", "source": "api/pyproject.toml"}],
 "api_calls": 13}
```

**`SystemDraft`** (stage `draft`)
```json
{"system": "api", "repos": ["api"], "purpose": "...", "interfaces": [], "deps": [],
 "edges": [Edge], "facts": [Fact], "questions": []}
```
The `verified:` date is stamped from `args.verified` when the draft is
ingested. Drafts never contain secrets, and `kb.py` refuses them if they do.

**`Connections`** (the barrier, `intelligent`)
```json
{"edges": [{"from": "api", "to": "auth", "protocol": "http", "auth": "jwt", "evidence": ["..."], "confidence": "high|medium|low"}],
 "contradictions": [{"topic": "...", "claims": ["x (source a)", "y (source b)"], "resolution": "question"}],
 "questions": []}
```
Contradictions always become `question` issues. They are never resolved
silently (precedence: `user` > `infra:*` > manifest-derived).

**`RepoResult`** (what a pipeline item, or one `Task` sub-agent, returns)
```json
{"name": "api", "status": "ok|failed|budget", "failed_stage": null, "results": {"survey": RepoSurvey, "graph": RepoGraphSummary, "draft": SystemDraft}}
```

**Merged report** (written to `reportFile` by the workflow = `fanout.py merge` output)
```json
{"stage": "research", "repos": {"<name>": {"status", "results", "failed_stage"}}, "connections": Connections|null,
 "counts": {"repos": 6, "ok": 6, "failed": 0}}
```
Repos are keyed by name and sorted. `merge()` in the JS file and `merge()` in
`fanout.py` are the same function. A test asserts that both paths produce
identical output for the same fixtures.

## Orchestrator loop (resumable)

```text
1. fanout.py plan   --repos N --remaining <gh core remaining>      # chunk if needed
2. fanout.py args   --stage S ... --mark > args.json               # pending-repos S --ready; marks in_progress
3. route: task-batch -> one Task call (model fast) -> fanout.py merge --stage S task-*.json > report.json
          workflow   -> workflow_run_file {path, args: args.json}             -> report = args.json .reportFile
4. fanout.py record --stage S --result <report>                    # state.py set-repo <name> <stage> done|blocked, artifacts
5. state.py summary                                                # per-repo completion shown to the user
```

The workflow only writes its own two files (`logFile`, `reportFile`, through
`tools.shell`), so **the orchestrator writes state**.
`fanout.py record` stores each stage result under
`.agents/factory-fanout/<repo>.<stage>.json` and calls `set-repo <name>
<stage> done`. A failed stage becomes `blocked`, which stops that repo's
pipeline but not the others. A `budget` status (from reports written before
the workflow lost its token budget) goes back to `pending`.

**Killed run.** Every finished stage appends one line to `logFile`: `FANOUT
{"repo", "stage", "status", "result"}`. The run empties it when it starts.
After a crash or kill, recover it before resuming:

```bash
python3 scripts/fanout.py record --stage research --log "$(jq -r .logFile .agents/fanout-args.json)"
```

This records every stage that finished before the kill. It then resets the
remaining `in_progress` marks to `pending`. The next `fanout.py args` returns
only unfinished repos, each with a `todo` list of its remaining stages and
`prior` results loaded from the artifacts. The next run therefore re-fans-out
only those stages. `workflow_run_file` saves nothing itself, so this log is the
only way to resume.

**Cost.** A run has no token budget. It is bounded by its size: one `fast`
call per pending repo stage plus one `intelligent` connections call. Chunk a
large org with `fanout.py plan` (above) to keep each run small.
