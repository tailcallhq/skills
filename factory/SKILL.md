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

## Phase 2: Bootstrap (parallel)

Contract: [parallelism](references/parallelism.md). The orchestrator plans,
dispatches and records; sub-agents clone. Never iterate over repos yourself.

1. `state.py pending-repos clone --ready` -> N repos. N = 0: go to verify.
2. **Gate, once**: list the N repos with target paths `~/workspaces/<name>`,
   and ask in the same message: "Clone these N repos (existing checkouts are
   verified, not re-cloned)? Also write a missing `AGENTS.md` per repo via
   `repo-setup` in phase 3? (yes / no / pick)". Record the second answer as
   `--output agents_md=all|none|<csv>`.
3. `fanout.py plan --repos N --remaining "$(gh api rate_limit --jq .resources.core.remaining)"`;
   chunk if it says so.
4. `fanout.py args --stage bootstrap --workspace ~/workspaces --kb <kb.path>
   --skill-dir <skill> --mark > .agents/fanout-args.json`, then route on `.route`:
   - `task-batch` (N < 4): **one** `Task` call, `model: fast`, one task per repo
     carrying the clone prompt and `RepoClone` schema from the reference.
     `fanout.py merge --stage bootstrap task-*.json > .agents/report.json`.
   - `workflow` (N >= 4): `workflow {path: <skill>/workflows/factory-fanout.js,
     args: <args.json>, budget: <.budget>}`; the report is its result.
   - Either path failing outright: use the other one, never a loop.
5. `fanout.py record --stage bootstrap --result .agents/report.json` (writes
   `set-repo <name> clone done|blocked`). Then one shell command creates the
   agent dirs for every cloned path: `mkdir -p <path1>/.agents <path2>/.agents ...`.
   If `AGENTS.md` was approved: delegate to `repo-setup` for the approved repos
   that lack one, as **one** `Task` call (`model: fast`, effort `low`, at most 16
   tasks per call): "Load skill `repo-setup`; survey `<path>` and write only
   `AGENTS.md` (no other setup, no commit); return `{repo, written, verdict}`".
6. Verify: `state.py pending-repos clone` is `[]` (blocked repos: report the
   first error line each and ask retry/skip; skip = `set-repo <n> clone skipped`).
7. `set-phase 2 done --output repos=<count>`.

## Phase 3: Research (parallel)

Stages `survey -> graph -> draft` per repo, one `intelligent` barrier for
`connections.md`. Contract: [parallelism](references/parallelism.md); KB
semantics: [knowledge-base](references/knowledge-base.md).

1. Graph first (deterministic, one command, internal concurrency):
   `repo_graph.py <paths of state.py pending-repos survey --ready> --org <org>
   --jobs 8 --activity --out .agents/graph.json`. Keep its activity (open PRs,
   recent merges, active branches) for phase 5.
2. Fan out: `fanout.py args --stage research ... --mark`, route exactly as in
   phase 2 (`task-batch` / `workflow`); sub-agents on `fast` return
   `RepoResult` (`RepoSurvey`, `RepoGraphSummary`, `SystemDraft`) and never
   write to the KB. The workflow runs the `intelligent` connections barrier
   itself; on the `task-batch` path make that one `intelligent` `Task` call
   yourself with the drafts (+ `contextDrafts`) and pass it to
   `fanout.py merge --connections`. Then `fanout.py record --stage research`.
3. **Ingest (single writer, orchestrator only)**: `kb.py ingest .agents/graph.json`,
   then apply the drafts' facts as pending (`kb.py add-system` / `add-fact
   --pending` / `add-connection`, scripted over `.agents/factory-fanout/*.draft.json`
   and `connections.json`). Contradictions become questions, never overwrites.
4. **Gate**: show `git -C <kb.path> diff origin/HEAD --stat` plus the new
   `connections.md` rows, and in the same message ask ONE batched confirmation
   of the pending edges: "Confirm (c), reject (r) or unsure (?) per edge:
   1. api -> auth http/jwt (high) ...". Confirmed ->
   `kb.py add-fact <system> --section dependencies --key uses --value <to> --source user`
   (and `add-connection ... --source user`); rejected -> leave pending with a
   question; unsure -> nothing.
5. `kb.py index`, then `kb.py propose "Research: <N> repos"`; exit 5 (another
   `factory/*` PR open) -> ask whether to add `--allow-multiple`.
6. Verify: the PR URL exists (`gh pr view <url> --json state`), and
   `state.py pending-repos draft` is `[]` (blocked repos reported, retry/skip).
7. `set-phase 3 done --output kb_pr=<url>`. The phase is done when the PR is
   **open**; merging is the user's call.

## Phase 4: Integrations (tracker, 4b monitoring, 4c infra read-only)

One detection pass, **one batched confirmation**, then three connection flows.
`state.py` has phases `4` and `4b` only: 4c results are recorded as outputs of
`4b` (`infra_<platform>=passed|refused|inconclusive|pending-credential`,
`infra_<platform>_id=<cluster/account/project>`), and `4b` is `done` only when
every chosen platform is resolved.

1. **Detect** (read-only, one shell call, in parallel):
   `detect_tracker.sh --jobs 8 <paths> > .agents/tracker.json &
   detect_monitoring.sh --jobs 8 <paths> > .agents/monitoring.json &
   detect_infra.sh --jobs 8 <paths> > .agents/infra-detect.json & wait`.
2. **Gate, one message**, three short lists with evidence and confidence:
   - Tracker: top candidate; if `per_repo` shows different top trackers, say
     "mixed org: Linear (api, web), Jira (billing)" and offer both.
   - Monitoring: every candidate >= 0.3, per system.
   - Infra: candidates >= 0.6 as defaults, 0.3-0.6 as "maybe", each with the
     read role to create and the env var name it will read.
   The user picks or skips each. Save the answers immediately
   (`set-phase 4 in_progress --output chosen=<csv>`, `set-phase 4b in_progress
   --output chosen=<csv> --output infra_chosen=<csv>`) so a resume never re-asks.

### 4 Tracker

Per [trackers](references/trackers.md): `mcp_list` first (an existing alias
= collision: reuse it or pick a new alias, ask). Then `mcp_add` with the
reference's exact entry, after approval:
OAuth-only servers (Notion, Trello) get `url` and no token; Atlassian: warn it
consumes Rovo credits; `bearer_token`/`headers` use `{{env.VAR}}`; for stdio
servers tell the user which variable to **export in their shell** (stdio `env`
is not templated), never paste it in chat. Verify with `mcp_list` (connected,
or "Unconfigured" -> `blocked --blocker "export <VAR>"`).
`set-phase 4 done --output tracker=<name> --output mcp=<alias>` (or `skipped`).

### 4b Monitoring

Same flow per [monitoring](references/monitoring.md), one `mcp_add` per chosen
system. After each connects, `kb.py add-monitor <system> --kind <k> ... --mcp
<alias> --source <evidence>` per KB system it covers; batch the KB changes into
one `kb.py propose "Monitoring"`. Outputs `detected`, `connected`, `skipped`.

### 4c Infra (read-only)

Per [infra](references/infra.md#how-phase-4c-uses-this), per chosen platform:
1. Show the least-privilege read role/policy and the env var name
   (`{{env.AWS_PROFILE}}`, `KUBECONFIG`, ...); **wait** for "done". Check only
   that the variable is set; missing -> `pending-credential`, move on.
2. Run the platform's probe (an attempted write that must be denied). Allowed
   -> **REFUSE the platform**: record `refused`, read nothing, tell the user
   which read role to create instead. Inconclusive -> report stderr tail,
   retried on resume. Never "try the read anyway".
3. Passed: `infra_graph.py --platform <p> --probe-passed --jobs 8 --repos
   .agents/graph.json --org <org> --out .agents/infra.json` (or the CLI reads
   the reference lists for other platforms), then `kb.py ingest .agents/infra.json`.
4. Ask now, one question: "Which ids are staging and prod for <system>?" ->
   `kb.py add-env <system> staging --id <id> --platform <p> --source user`
   (same for prod). Unknown is fine; infra changes will stop and ask later.
5. `kb.py propose "Infra discovery (read-only)"`. Record platform + identifier
   in outputs, **never the credential**. Re-probe on every resume.

## Phase 5: Board

1. Seed list (no side effects): phase 3 activity from `.agents/graph.json`
   (open PRs, active branches, recent merges) + open tracker items via the phase 4
   MCP (assigned/in-progress only; skip if no tracker) + KB open questions.
2. Delegate to `project-board` (`skill_view project-board`): find-or-create the
   org's work project, `working_directory` = the KB checkout, with the seed list;
   it owns dedupe and its own approval gate before `project_update`.
3. Verify with `project_get`; `set-phase 5 done --output project_id=<id>`.

## Phase 6: Routines

Gate on `machine.cloud`. If false: `set-phase 6 skipped --output reason=non-cloud`
and say: "Routines need a cloud machine (`automation_*` is unavailable here). Run
factory on a cloud machine later to enable them; nothing else is affected."
Otherwise:
1. Present the catalog from [routines](references/routines.md) (name, schedule,
   what it files; all cron + polling, since `machine.push_triggers` is false).
   Ask which to enable, one message. Routines only file issues / open KB PRs;
   they never merge, never touch infra (drift = an `infra-change` issue).
2. After approval, `automation_create` each; verify with `automation_get`;
   `state.py set-routine <id> --automation-id <aid>` right after each one.
3. `set-phase 6 done --output routines=<csv>`.

## Infra changes (on explicit ask only; not a phase)

Setup and routines never change infrastructure. When the user explicitly asks
for a concrete change, follow [infra-changes](references/infra-changes.md) in
order, starting with its "Resume first" (`state.py change-next <id>`). Never
delegate an approval or an apply, never skip staging.

## Done

`state.py summary`, the final `intelligent` summary (KB PR, board, routines,
anything `blocked` with its exact fix), and tick the checklist above.
