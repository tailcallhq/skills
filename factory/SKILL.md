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
