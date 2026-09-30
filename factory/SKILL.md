---
name: factory
description: "Bootstraps a multi-repo developer workspace: private knowledge base of systems, gh auth, repo onboarding, tracker/monitoring/infra connections, project board and upkeep routines. Use to set up or resume a workspace for an org."
---

# factory: bootstrap a workspace for an org

Takes an org from nothing to a working, self-maintaining workspace: a private
knowledge base (KB) of its systems, cloned and onboarded repos, tracker,
monitoring and read-only infra connections, a project board and routines. It is
a **thin, resumable orchestrator**: scripts do deterministic work, sub-agents do
per-repo work in parallel, other skills (`repo-setup`, `project-board`,
`repo-health`, `verify`) do their own jobs. It never loops over repos itself.

`<skill>` below is this skill's directory; run commands from the workspace root
(default `~/workspaces`). References (read the one a phase names, when you get there):
[state](references/state.md), [parallelism](references/parallelism.md),
[knowledge-base](references/knowledge-base.md), [trackers](references/trackers.md),
[monitoring](references/monitoring.md), [infra](references/infra.md),
[infra-changes](references/infra-changes.md), [routines](references/routines.md).

## Non-negotiables

- **Approval gate before every side effect** (clone, repo create, push/PR,
  `mcp_add`, `project_update`, `automation_create`). Batch questions per phase;
  never batch an infra apply.
- **No credentials** in chat, state, KB, `mcp.json` or this skill: only env var
  *names* and `{{env.VAR}}`. Never echo a value.
- **KB is a private org repo**; changes land as PRs. Public/internal = stop.
- **Infra is read-only.** Changes only on explicit ask, see below.
- **Fast by default**: fan-out on the `fast` tier, effort `low`; `intelligent`
  only for reconciliation, `connections.md`, phase summaries, infra planning.
  Never `expert`. Resolve ids with `model_list` (phase 0).
- Degrade, don't fail: a missing tool/capability becomes a note and a
  `skipped`/`blocked` phase, never an error.
- Each phase ends with a short (<= 5 lines) summary written on the
  `intelligent` tier: what changed, links, what is next.

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

## Phase checklist

- [ ] Phase 0 Preflight
- [ ] Phase 1 Knowledge base
- [ ] Phase 2 Bootstrap (parallel)
- [ ] Phase 3 Research (parallel)
- [ ] Phase 4 Integrations: 4 tracker, 4b monitoring, 4c infra read-only
- [ ] Phase 5 Board
- [ ] Phase 6 Routines

Each phase below: **gate** (ask, batched) -> **act** -> **verify** ->
`state.py set-phase <id> done|skipped|blocked`. Mark `in_progress` when you start.

## Phase 0: Preflight

Nothing here has side effects except the `gh` scope refresh the user runs.

1. `gh --version` (missing = `blocked`, tell the user to install GitHub CLI).
2. `gh auth status`: token scopes must include `repo` and `read:project`.
   - Missing `read:project`: offer `gh auth refresh -h github.com -s read:project`
     (the user runs it; it opens a browser/device code).
   - Not logged in: print exactly
     `gh auth login -h github.com -p https -w -s repo,read:project` (device flow),
     ask the user to run it and say "done", then re-check. Never ask for a token.
   - Still failing: `set-phase 0 blocked --blocker "gh auth: <what is missing>"`.
3. Capabilities, probed, never assumed:
   - **Cloud**: call `automation_list`. A result = cloud; a refusal/"not
     available" = non-cloud. `state.py set machine.cloud true|false`.
   - **Push triggers**: `tool_search webhook`. No `webhook_*` tool (today's
     answer) -> `state.py set machine.push_triggers false`; routines poll.
   - **Models**: `model_list`; pick the `fast` and `intelligent` ids per
     [parallelism](references/parallelism.md#model-tier-policy).
   - `workflow` tool present? (`tool_search workflow`) If not, every fan-out
     uses the `Task` batch path.
4. **Tell the user up front**, in one message, what will be unavailable, e.g.
   "Non-cloud machine: phase 6 routines will be skipped. No webhooks: alerts are
   polled. No Haiku-class model: using <id> for fan-out."
5. `set-phase 0 done --output gh_user=<login> --output fast_model=<id>
   --output intelligent_model=<id> --output workflow=true|false`.

## Phase 1: Knowledge base

Details: [knowledge-base](references/knowledge-base.md). The org was asked once
at `state.py init`; read it with `state.py get org`, never ask again.

1. `kb.py ensure-repo <org>`:
   - exit `5` (missing): **gate** "Create private repo `<org>/knowledge`?" then
     `kb.py ensure-repo <org> --create`.
   - exit `2` (PUBLIC/INTERNAL): **STOP.** `set-phase 1 blocked --blocker "KB
     repo not private"`; tell the user to make it private. No local-only fallback.
   - Relay the branch-protection recommendation it prints; never change settings.
2. **Existing KB** (`initialized: true`): read `index.md`, run `kb.py stale` and
   summarize systems, pending items and open questions in 3 lines.
3. **New KB**: `kb.py init <path>`, then ONE batched question: "Which systems
   should the KB start with, and which repos belong to each? Default: every repo
   of `<org>` (I'll enumerate with `repo_graph.py --org <org>`)." For the
   default, run `repo_graph.py --org <org> --max-repos 50 --out .agents/graph.json`
   and show the repo list. Then `kb.py add-system <id> --repo <org>/<r>... --source user`
   per system, show the skeleton, and after approval `kb.py propose "Initial
   skeleton" --init` (the only direct push to `main` factory ever makes).
4. Register repos for phase 2: `state.py set-repo <name> clone pending --url
   <url> --path ~/workspaces/<name>` for each repo the user chose.
5. **Forge project "Knowledge base"**: delegate to the `project-board` skill
   (`skill_view project-board`) to find-or-create it (`project_list` first; never
   a duplicate). Then `kb.py board-ops --project-id <id> --with-index >
   .agents/kb-ops.json`, show the operations, and `project_update` after approval.
6. Verify: `gh repo view <org>/knowledge --json visibility` is `PRIVATE`,
   `project_get <id>` returns the project.
7. `state.py set kb.url <url>`, `set kb.path <path>`, `set kb.project_id <id>`;
   `set-phase 1 done --output kb_repo=<url> --output kb_path=<path>`.

On every resume, rerun `kb.py ensure-repo <org>` first (visibility re-check),
even when phase 1 is `done`.
