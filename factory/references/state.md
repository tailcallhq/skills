# factory state: `.agents/factory-state.json`

The single source of truth for "how far has factory got". Every phase reads it
before acting and writes it after each side effect, so a new conversation can
say "continue factory" and pick up exactly where the last one stopped, including
partially completed per-repo fan-out.

Never edit the file by hand or with ad-hoc `jq`: always go through
`scripts/state.py`, which validates, locks and writes atomically.

## Location

`.agents/factory-state.json` relative to the workspace root factory runs in
(the directory that holds the cloned repos, e.g. `~/workspaces`). Override with
`--file PATH` or `FACTORY_STATE=PATH`. Next to it `state.py` keeps:

| File | Purpose |
|---|---|
| `factory-state.json.lock` | `flock` target; serializes writers across threads and processes |
| `factory-state.json.bak` | last valid version before the most recent write; used for recovery |
| `factory-state.json.corrupt-<ms>` | a quarantined unreadable/invalid file; never deleted automatically |

The file holds progress and identifiers only. **Never put secrets in it**
(tokens, env var values, MCP headers): record the env var *name* if needed.

## Schema (version 2)

Version 2 added `infra_changes`. A version-1 file is upgraded in place on load
(empty `infra_changes`, persisted on the next write); a file with a newer
version than the script supports is still refused and left untouched.

```jsonc
{
  "version": 2,                         // int; state.py refuses files with a newer version
  "started_at": "2026-09-30T06:00:00Z", // UTC, set by init
  "org": "acme",                        // GitHub org/user; null until phase 0 asks
  "kb": {
    "url": "https://github.com/acme/knowledge", // private org repo (phase 1)
    "path": "/home/me/workspaces/knowledge",    // local checkout
    "project_id": "…"                           // Forge "Knowledge base" project id
  },
  "machine": {
    "cloud": true,          // automation_* available (probed in phase 0); null = not probed
    "push_triggers": false  // webhook_* tools present; false today
  },
  "phases": [               // always all 8, in this order
    {
      "id": "0",            // "0" | "1" | "2" | "3" | "4" | "4b" | "4c" | "5" | "6"
      "name": "preflight",
      "status": "done",     // pending | in_progress | done | skipped | blocked
      "outputs": {"gh_user": "octo"}, // string map; ids/urls later phases need
      "blocker": null,      // required string when status == blocked, else null
      "updated_at": "2026-09-30T06:01:00Z"
    }
  ],
  "repos": {
    "api": {
      "path": "/home/me/workspaces/api",
      "url": "https://github.com/acme/api",
      "stages": {"clone": "done", "survey": "done", "graph": "in_progress", "draft": "pending"},
      "updated_at": "…"
    }
  },
  "routines": {
    "alert_intake": {"automation_id": "…", "cursor": "2026-09-30T06:00:00Z", "updated_at": "…"}
  },
  "infra_changes": {        // on-demand flow, references/infra-changes.md; never created by phases/routines
    "chg-20260930-api-db-size": {
      "platform": "terraform",          // terraform | k8s | helm | imperative:<cli>
      "system": "api",                  // KB system id the change targets
      "classification": "mutating",     // additive | mutating | destructive (can only be raised)
      "env_scope": "staging+prod",      // staging | staging+prod (what the user asked for)
      "plan_hash": "sha256:…",          // staging plan hash; fixed once approved_staging is done
      "inverse_plan_path": ".agents/infra-plans/chg-…/staging/inverse.json",
      "steps": {
        "classified": "done", "planned_staging": "done", "approved_staging": "done",
        "applied_staging": "done", "verified_staging": "in_progress",
        "planned_prod": "pending", "approved_prod": "pending", "applied_prod": "pending",
        "verified_prod": "pending", "recorded": "pending"
      },
      "blocker": null,
      "updated_at": "…"
    }
  },
  "recovered_from": "factory-state.json.corrupt-…" // present only after a fresh-start recovery
}
```

### Phases and the repo stages they own

| id | name | per-repo stages |
|---|---|---|
| `0` | preflight | – |
| `1` | knowledge base | – |
| `2` | bootstrap repos | `clone` |
| `3` | research | `survey` (repo-setup), `graph` (repo_graph.py), `draft` (KB system file) |
| `4` | tracker MCP | – |
| `4b` | monitoring MCP | – |
| `5` | board | – |
| `6` | routines | – |

### Status semantics

| status | meaning | `next` treats it as |
|---|---|---|
| `pending` | not started | resume point |
| `in_progress` | started, not finished (the session may have died) | resume point |
| `blocked` | cannot proceed until `blocker` is resolved | resume point; re-check the blocker first |
| `done` | finished; outputs recorded | finished, never re-run without asking |
| `skipped` | deliberately not done (user declined, non-cloud machine for 6, no monitoring for 4b) | finished |

Rules enforced by `state.py`:

- `done -> anything else` is refused (exit 1) unless `--force`. Use `--force`
  only after the user explicitly asked to redo that phase or repo.
- `blocked` requires `--blocker MSG`; any other status clears `blocker`.
- `--output k=v` merges into `outputs` (existing keys are overwritten, others kept).
- Unknown phase ids, stages or statuses are refused.

A phase with per-repo stages is only marked `done` once `pending-repos <stage>`
is empty for each of its stages (every repo `done` or `skipped`).

## `scripts/state.py`

Stdlib only, Python 3.8+. Exit 0 on success, 1 on a refused/invalid operation
(message on stderr), 2 on usage error.

| Command | Effect / output |
|---|---|
| `init [--org ORG] [--force]` | create the file if missing; if present, keep it (fills `org` if unset). A different `--org` is refused without `--force` (which starts over). Prints the `next` message |
| `next [--json]` | first phase not `done`/`skipped`, e.g. `resuming at phase 3 (research), repos web,worker pending`. `--json`: `{state: none\|resume\|complete, phase, name, status, blocker, done[], pending_repos[], message}` |
| `summary` | human table of phases, outputs, blockers and per-repo stages, then the `next` line. Use it for the resume summary to the user |
| `get [DOTTED.PATH]` | JSON of the whole state or a subtree; list elements are addressed by `id`: `get phases.1.outputs`, `get repos.api.stages`, `get kb.url` |
| `set-phase ID STATUS [--output k=v]... [--blocker MSG] [--force]` | update one phase; prints the phase |
| `set-repo NAME STAGE STATUS [--path P] [--url U] [--force]` | create/update one repo's stage; safe to call concurrently from parallel sub-agents |
| `pending-repos STAGE [--ready]` | JSON list of repos whose STAGE is not `done`/`skipped`, sorted. `--ready` also requires every earlier stage finished (e.g. only survey repos that are cloned). Feed it straight into fan-out args |
| `set KEY VALUE` | scalar fields: `org`, `kb.url`, `kb.path`, `kb.project_id`, `machine.cloud`, `machine.push_triggers` (`true\|false\|null`) |
| `set-routine ID [--automation-id A] [--cursor C]` | record a routine's automation id / polling cursor |
| `set-change ID STEP STATUS [--platform P] [--system S] [--classification C] [--env-scope E] [--plan-hash H] [--inverse-plan-path P] [--blocker MSG] [--force]` | advance one infra change step (rules below); prints the change |
| `change-next ID [--json]` | first step of the change not `done`/`skipped`. `--json`: `{id, state: none\|resume\|complete, step, status, blocker, done[], never_rerun[], message}` |

### Infra change steps

Order (none skippable): `classified` -> `planned_staging` -> `approved_staging`
-> `applied_staging` -> `verified_staging` -> `planned_prod` -> `approved_prod`
-> `applied_prod` -> `verified_prod` -> `recorded`. Enforced by `set-change`:

- a step can become `in_progress`/`done` only when every earlier step is `done`;
- only the `*_prod` steps may be `skipped`, and only with `env_scope: staging`
  (the user's ask excluded prod); `*_prod` steps require `env_scope: staging+prod`;
- `classified` done needs `--platform`, `--classification`, `--env-scope`;
  `planned_staging` done needs `--plan-hash`; `applied_staging` needs
  `--inverse-plan-path` recorded first;
- `plan_hash` is immutable after `approved_staging`; `classification` may only
  be raised (additive -> mutating -> destructive);
- **`applied_staging` / `applied_prod` are one-way**: once `done` they can never
  be changed, not even with `--force`; once `in_progress` (an apply was started)
  they may only become `done` or `blocked`, never `pending`/`skipped`. A resume
  that finds an apply `in_progress`/`blocked` inspects live state against the
  plan and records the outcome; it never re-runs the apply. Rolling back is a
  new change id driven by the inverse plan.
- other `done` steps follow the normal `--force` rule.

### Concurrency and durability

- Every mutation is: take an exclusive `flock` on `<file>.lock` (plus an
  in-process lock for threads) -> read -> modify -> write a temp file in the
  same directory -> `fsync` -> `os.replace`. Readers never see a half-written
  file and concurrent `set-repo` calls from parallel sub-agents never lose
  updates (tested with 8 threads and 8 processes).
- Before each write the current valid file is copied to `.bak`.

### Corrupt file recovery

On an unparseable or schema-invalid file (any command, including `next`):

1. the file is moved to `factory-state.json.corrupt-<ms>` (kept for inspection);
2. if `.bak` is valid it is restored and a warning is printed (at most one
   write is lost; re-verify the phase `next` reports);
3. otherwise a fresh state is written (salvaging `org` when readable) with
   `recovered_from` set, and a warning says completed work must be re-verified
   (clones on disk, KB repo, projects, automations are then detected rather than
   re-created: every phase is already idempotent against existing resources).

A file with a **newer** `version` is refused and left untouched.

## Resume protocol (SKILL.md section)

The factory `SKILL.md` includes this section verbatim near the top:

```markdown
## Resume first

On every invocation, before anything else:

1. Run `python3 <skill>/scripts/state.py next --json` from the workspace root.
2. `state: none` -> fresh run: ask for the org, then `state.py init --org <org>` and start phase 0.
3. `state: resume` -> run `state.py summary` and tell the user in 2-4 lines what is
   done (phases and repos) and where you are resuming, e.g. "Resuming at phase 3
   (research); repos web, worker pending; api done." Do not re-run finished phases
   or `done` repos; do not re-ask questions whose answers are in `outputs`.
   - If the phase has per-repo stages, fan out over `state.py pending-repos <stage> --ready`
     only.
   - If `status: blocked`, re-check the blocker yourself before asking the user
     (e.g. `gh auth status` for auth, `gh repo view <org>/knowledge --json visibility`
     for the KB, `env | grep -c VAR` style presence checks for a missing env var,
     `tool_search` for a missing tool). If it is resolved, `set-phase <id> in_progress`
     and continue; only if it is still blocked, tell the user the exact fix.
   - Phase 1 done still re-checks KB visibility on every resume; if it is no longer
     PRIVATE, mark phase 1 `blocked` and stop.
4. `state: complete` -> say so and offer the routine/board maintenance options;
   never redo a phase unless the user asks (then `--force`).
5. After every side effect: `set-phase`/`set-repo` immediately, not at the end of
   the phase. Mark a phase `done` only when its repos have no pending stages.
```
