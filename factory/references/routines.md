# Routines (phase 6)

The catalog that phase 6 shows the user. Each routine is a cron-scheduled
`automation_create` prompt that runs as a fresh conversation on the cloud
machine. It has no memory of setup, so every prompt is standalone.
`automation_*` is cron-only: there are no webhooks today
(`machine.push_triggers: false`), so everything event-like is **polling**.

Invariants for every routine (enforced by the shared blocks below and by
`scripts/test_routines.py`):

- **Writes only board issues or KB pull requests.** It never merges, never
  approves, never pushes to a default branch, never calls `project_run`, never
  closes or removes issues, and never changes infrastructure. Infra drift is at
  most an `infra-change` issue.
- **Access check first, fail loudly.** Before any other step, the prompt checks
  that `gh auth status` works and that the KB repo is reachable and `PRIVATE`.
  If not, it files one `routine-failure` issue on the work board and stops.
- **Ids come from state.** The prompt reads org, KB repo/checkout/board and the
  work board from `.agents/factory-state.json` (`state.py get`). The ids that
  `routines.py render` writes into the prompt are only a snapshot, and are used
  only to report a failure when state cannot be read.
- **No credentials** anywhere in a prompt. Env var names are fine, values are not.
- **Deduped.** Every filed issue carries a `<!-- routine:<name>:<key> -->`
  marker. A rerun updates the open issue with that marker instead of adding
  another one.

## Model tier

`automation_create` has no model parameter: the automation runs on the model
of the conversation that created it. To keep routines cheap, each prompt does
its work with scripts plus `Task` sub-agents on the tier listed below (`fast`
unless stated, effort `low`), per
[parallelism](parallelism.md#model-tier-policy). If you can, create the
automations from a conversation that runs on the `fast` model. Token costs
below are estimates per run for a 20-repo org.

## Catalog

| routine | default cron (UTC) | tier | est. tokens/run | writes |
|---|---|---|---|---|
| [kb-refresh](#kb-refresh) | `0 5 * * 1` (Mon 05:00) | fast | ~30k | one KB PR; KB-board issues (questions, stale facts) |
| [repo-health](#repo-health) | `0 6 * * 1` (Mon 06:00) | fast | ~6k/repo + 10k (~130k) | work-board issues |
| [board-triage](#board-triage) | `30 2 * * *` (nightly 02:30) | fast | ~15k | one triage issue (updated in place) |
| [workflow-suggestions](#workflow-suggestions) | `0 7 * * 5` (Fri 07:00) | intelligent | ~80k | proposal issues |
| [infra-drift](#infra-drift) | `0 4 * * 3` (Wed 04:00) | fast | ~25k | KB-board `question` issues; work-board `infra-change` issues |
| [alert-intake / ci-intake](#alert-intake--ci-intake) | owned by issue 7 | | | |

## How phase 6 uses this file

```bash
python3 <skill>/scripts/routines.py list                                   # the catalog as JSON
python3 <skill>/scripts/routines.py cron-check kb-refresh [--cron "0 5 * * 1"]  # validate a (user-changed) schedule
python3 <skill>/scripts/routines.py render kb-refresh --state .agents/factory-state.json \
  [--cron EXPR] [--timezone Europe/Berlin] > /tmp/kb-refresh.json         # automation_create input
```

`render` prints `{name, prompt, trigger: {cron[, timezone]}, overlap_policy: "skip"}`.
Pass it to `automation_create` as it is. `render` exits `1` and names the missing
field when state has no org, `kb.url`, `kb.path`, `kb.project_id`, or phase 5
`project_id` (the work board), so no routine is created before phases 1 and 5
are done. After `automation_create`, record the automation with
`state.py set-routine <name> --automation-id <id>`.

The prompts are assembled from the blocks below. `render` fills the
`{{placeholders}}` and refuses if any placeholder is left unfilled. The final
prompt is `preamble` + the routine body + `rules`.

## Shared prompt blocks

```text preamble
You are the factory routine "{{name}}" for the GitHub org {{org}}. You run unattended on a schedule, with no earlier context. Factory scripts: {{skill_dir}}/scripts. State file: {{state_path}}. Workspace root: {{workspace}}.

STEP 0: access check. Do this first, and do nothing else until it passes.
1. Run `gh auth status`. Then run `gh repo view {{kb_repo}} --json visibility -q .visibility`, which must print PRIVATE.
2. Read the ids from state, never from memory: `python3 {{skill_dir}}/scripts/state.py --file {{state_path}} get`. Use org = `org`, KB repo = `kb.url`, KB checkout = `kb.path`, KB board = `kb.project_id`, work board = `outputs.project_id` of the phase with id "5" in `phases[]`. The ids written in this prompt are a snapshot from when the routine was created. Use them ONLY to report a failure in step 3. When state and this prompt disagree, state wins.
3. FAIL LOUDLY. Stop all work if any of these is true: `gh auth status` fails, the repo view fails, the visibility is not PRIVATE, or the state file is unreadable or missing those ids. File one issue on the work board (the state value, else {{board_project_id}}) with `project_update` `add_issue`: title `Routine {{name}} blocked: <one-line reason>`, content = the failing command and the last lines of its stderr (redact anything that looks like a token), labels ["routine-failure"], and the line `<!-- routine:{{name}}:blocked -->` at the end. First `project_get` the board and skip the add if an open issue already carries that marker. Then end the run and state the reason. Never fall back to a public or local-only KB. Never run `gh auth login`. Never ask for or print a credential.
```

```text rules
RULES (these apply to every step above):
- Your only outputs are Forge board issues via `project_update` (`add_issue`, or `update_issue` on content you filed earlier) and knowledge-base pull requests via `kb.py propose`. Never merge, approve or close a PR. Never push to a default branch. Never call `project_run` or start work on an issue. Never change an issue's status, never close or remove an issue.
- Never write to infrastructure: no apply, create, delete, patch, scale or restart on any platform. Read infrastructure only through `infra_graph.py`, which probes first. Anything that needs changing becomes an `infra-change` issue for a human.
- Never print, store or file credentials or env var values. Env var names are fine.
- Dedupe. Before each `add_issue`, `project_get` the target board with filter {"kind":"all","exprs":[]} and read every page. If an open issue already has the same `<!-- routine:... -->` marker, `update_issue` its content instead of adding a new one.
- Degrade, don't fail. A missing tool, skill, env var or failing sub-step becomes a line in your summary (and at most one `routine-failure` issue with the marker `<!-- routine:{{name}}:degraded -->`). It never aborts the other steps.
- Sub-agents: `Task` with `model: "{{tier}}"`, effort low, and a structured answer. Never use `expert`.
- End with a summary of at most 10 lines: what you checked, the issues you filed or updated (ids), the PRs you opened (urls), and what you skipped and why.
```

## kb-refresh

| field | value |
|---|---|
| cron | `0 5 * * 1` |
| tier | fast |
| tokens | ~30k (scripts do the scan; the agent summarizes JSON) |
| reads | org repos via `gh api` + local checkouts (`repo_graph.py --org --activity`); live infra for platforms whose phase-4c probe passed (`infra_graph.py`, probe first); the KB checkout |
| writes | one KB PR `factory/kb-refresh-<date>` (`kb.py propose`); KB-board issues from `kb.py board-ops` (questions, stale facts); a `routine-failure` issue when blocked |
| never | merges its PR, opens a second PR while a `factory/*` PR is open, reads infra with a credential that can write |

Why weekly: `repo_graph.py` costs about 24 `gh api` calls per repo, so a
50-repo org uses about a quarter of the hourly core budget. Weekly keeps the
KB at most 7 days behind, which is well inside `stale --days 30`.

```text kb-refresh
TASK: refresh the knowledge base (KB) from the org's repos and read-only infra, then open ONE pull request. Use the KB checkout path from state as KB below. `kb.py` below means `python3 {{skill_dir}}/scripts/kb.py`; pass `--kb KB` after the subcommand on every call.

1. Sync: `python3 {{skill_dir}}/scripts/kb.py ensure-repo <org> --path KB`. Exit 2 means the repo is not PRIVATE: treat it as a STEP 0 failure. If `git -C KB status --porcelain` is not empty, stop and file a `routine-failure` issue (marker `<!-- routine:{{name}}:dirty -->`). Never discard anyone's changes.
2. One open PR at a time: `gh pr list -R <KB repo> --state open --json number,url,headRefName`. If any `headRefName` starts with `factory/`, SKIP the rest of the run. File (or update) one issue on the KB board titled `KB refresh skipped: factory PR still open`, list the PR urls, ask a human to merge or close them, and add the marker `<!-- routine:{{name}}:pr-open -->`. Then go to the summary.
3. Repo graph: `python3 {{skill_dir}}/scripts/repo_graph.py --org <org> --activity --workspaces {{workspace}} --jobs 8 --refresh --out {{workspace}}/.agents/graph.json`. Check `rate_limited` and `errors[]` in the output and note them in the summary. A partial graph is fine.
4. Infra, read-only, only for platforms that phase 4c recorded as passed. Read the outputs of phase "4c" from state. For each key `infra_<platform>` whose value is `passed`, with platform in kubernetes, aws or terraform:
   a. The env var named in {{skill_dir}}/references/infra.md for that platform must be set. Check presence only (`[ -n "${VAR+x}" ]`), never print it. If it is not set, skip the platform and note it.
   b. Run `python3 {{skill_dir}}/scripts/infra_graph.py --platform <platform> --jobs 8 --repos {{workspace}}/.agents/graph.json --org <org> --out {{workspace}}/.agents/infra.json`, and add `--context`, `--region`, `--namespace` or `--dir` from the matching `infra_<platform>_*` outputs. The script re-runs the verification probe (an attempted write that must be denied) before it reads anything. For a terraform remote backend, first run the backend platform's probe exactly as infra.md describes, and add `--probe-passed` only if that write was denied in THIS run.
   c. Exit 3 means the probe was REFUSED: the credential can now write. Read nothing more from that platform, and file a `routine-failure` issue on the work board titled `Infra credential for <platform> can write; read-only discovery refused`. Say which read role to use instead (infra.md), and add the marker `<!-- routine:{{name}}:probe-<platform> -->`. Exit 4 means inconclusive: note the stderr tail and skip the platform. Never retry without the probe.
5. Ingest (single writer, one command at a time): `kb.py ingest --kb KB {{workspace}}/.agents/graph.json`, then, if step 4 produced it, `kb.py ingest --kb KB {{workspace}}/.agents/infra.json`. Exit 3 means a secret-looking value was refused: note which file, never copy the value. Exit 6 means another writer holds the lock: wait 60 s, retry once, then note it and skip.
6. Stale report: `kb.py stale --kb KB --days 30`. Keep the counts for the summary.
7. KB board: save the full `project_get` result of the KB board to /tmp/kb-board.json (filter {"kind":"all","exprs":[]}, every page merged). Run `kb.py board-ops --kb KB --project-id <KB board> --existing /tmp/kb-board.json --with-index --days 30 > /tmp/kb-ops.json`, then apply those operations with one `project_update` call. They are already deduped by their `kb:` markers.
8. Propose: `kb.py propose --kb KB "KB refresh <YYYY-MM-DD>" --body "<counts from steps 3-6: repos scanned, infra platforms read, new candidates, questions, stale facts>"`. "nothing to propose" is a normal outcome. Exit 5 means a factory PR appeared meanwhile: treat it as step 2. Do NOT merge the PR, and do not pass --allow-multiple.
```

## repo-health

| field | value |
|---|---|
| cron | `0 6 * * 1` |
| tier | fast |
| tokens | ~6k per repo + ~10k for the run (~130k for 20 repos) |
| reads | repos in state (`repos.*.path/url`); per repo: default-branch CI runs, Dependabot alerts, open PRs, root docs, via the `repo-health` skill when installed, otherwise the built-in checks in `workflows/routine-repo-health.js` |
| writes | at most one work-board issue per repo with findings, updated in place (marker `routine:repo-health:<repo>`) |
| never | fixes anything, opens PRs, comments on PRs, re-runs CI |

Fan-out follows [parallelism](parallelism.md#decision-rule): 1-3 repos use one
`Task` batch, and 4 or more use the saved workflow
`workflows/routine-repo-health.js` (read-only agents on `fast`, structured
`HEALTH` results). The orchestrating conversation files the issues, so the
sub-agents never touch the board.

```text repo-health
TASK: weekly read-only health check of every repo in state. File one board issue per repo that has findings.

1. Repos: from the state JSON you read in STEP 0, take `repos` (name -> path, url). Skip repos whose `stages.clone` is not `done`, or whose path does not exist, and note them. No repos left means: summarize and stop.
2. Fan out, never loop over repos one at a time:
   - 1-3 repos: ONE `Task` call with one entry per repo in `tasks`, `model: "{{tier}}"`. Each entry gets the check list from step 3 and returns JSON {repo, status: ok|findings|failed, findings: [{check, severity: high|medium|low, title, evidence}]}.
   - 4 or more repos: the `workflow` tool with `path: {{skill_dir}}/workflows/routine-repo-health.js`, args {"org": "<org>", "date": "<today YYYY-MM-DD>", "repos": [{"name", "path", "url"}, ...]}, budget "+<6k x repos + 50k>". It returns {results, counts}. If the `workflow` tool is missing or the run fails, use the `Task` batch instead (several batches of at most 8 entries, sent together in one message).
3. Checks, read-only, per repo: if the `repo-health` skill exists (`skill_view repo-health`), follow it in report-only mode. Otherwise: failing latest default-branch CI run (high); open critical/high Dependabot alerts (high; a 403/404 is a note, not a finding); open PRs with no update in 30+ days (low, one finding); no AGENTS.md/README.md at the root (low). Sub-agents never push, open PRs, comment or edit the board.
4. File on the work board (`project_update`, one batch): for each repo with findings, one issue titled `Repo health: <repo> (<n> findings)`, with the findings as a checklist (severity, title, evidence link) and the marker `<!-- routine:{{name}}:<repo> -->`. Set priority `high` when any finding is high and the board has that priority. If an open issue already carries the marker, `update_issue` its content instead of adding one. A repo whose findings disappeared gets its content updated to "no findings as of <date>"; leave its status alone.
5. Failed repos (agent error or budget): list them in the summary. If more than half of the repos failed, also file a `routine-failure` issue with the marker `<!-- routine:{{name}}:degraded -->`.
```

## board-triage

| field | value |
|---|---|
| cron | `30 2 * * *` |
| tier | fast |
| tokens | ~15k (one `project_get` sweep and a deterministic pass; no sub-agents under 200 issues) |
| reads | the work board and the KB board (`project_get`, every page, filter `all`) |
| writes | one triage issue per board, `Board triage` (marker `routine:board-triage:<board>`), whose content is replaced on every run |
| never | changes status, priority, blockers or parents; closes, removes, merges or runs issues; comments on others' issues |

Board issues cannot take comments (`project_update` has no comment
operation), so "one triage comment" means one pinned triage issue per board,
rewritten each night. When nothing is flagged, its content says so. It is not
closed.

```text board-triage
TASK: nightly board triage. Report only: flag problems in ONE triage issue per board and change nothing else.

1. For each board (work board, then KB board): `project_get` with filter {"kind":"all","exprs":[]}, and merge every page (follow the cursor with the same filter). Read the status set: its `category` (todo | in_progress | complete) is what counts, not the status name.
2. Flag, open issues only (category not `complete`), skipping the triage issue itself:
   a. STALE IN PROGRESS: category `in_progress`, and `updated` more than 7 days ago.
   b. READY BUT IDLE: category `todo`, every `blocked_by` issue is in a `complete` status (or there are none), no linked conversation or run history, and created more than 3 days ago. List these as candidates for a human to run. Never run them.
   c. BLOCKED BY CLOSED: `blocked_by` points at an issue that was removed or does not exist.
   d. LIKELY DUPLICATES: open issues whose titles match after lowercasing and stripping punctuation and a `KB question:` / `Repo health:` prefix, or that carry the same `<!-- routine:... -->` or `<!-- kb:... -->` marker. Give pairs, oldest first.
   e. ROUTINE FAILURES: open `routine-failure` issues older than 2 days, since a human should look at them.
   For boards over 200 open issues, split the duplicate check (d) across one `Task` batch (entries of at most 100 issues each, titles and ids only), and do a-c and e yourself.
3. Write the triage issue: title `Board triage`, content = today's date, then one section per flag type (a-e) with `issue://<full id>` links and a one-line reason each (e.g. "in progress 12 days, last update 2026-09-18"), then counts, then the marker `<!-- routine:{{name}}:<board project id> -->`. If an open issue with that marker exists, `update_issue` its content; otherwise `add_issue` it (labels ["triage"] when the board has that label). With nothing flagged, the content is "Nothing to triage as of <date>".
4. You must not call `update_issue` on any other issue, and you must not use `set_blocked_by`, `move_issue` or `remove_issue`.
```

## workflow-suggestions

| field | value |
|---|---|
| cron | `0 7 * * 5` |
| tier | **intelligent** (it has to judge patterns across conversations); the transcript reads are `fast` sub-agents |
| tokens | ~80k: 1 intelligent pass (~20k) + up to 30 conversation digests at ~2k on `fast` |
| reads | the last 7 days of conversations (`list_conversations`, `read_conversation`, `list_conversation_citations`); installed skills (`skill_search "*"`); existing automations (`automation_list`) |
| writes | at most 3 proposal issues on the work board (label `proposal`), deduped by marker `routine:workflow-suggestions:<slug>` |
| never | creates a skill, an automation, or a board run; copies transcript text containing secrets; proposes anything that merges, deploys or changes infra without an approval gate |

```text workflow-suggestions
TASK: weekly, find manual work the user keeps repeating and PROPOSE (only propose) a skill or a routine for it, as board issues.

1. Inventory, read-only: `list_conversations` (limit 50, page until an entry is older than 7 days, and at most 100 entries). Also `skill_search` with query "*" (limit 20) for the installed skills, `automation_list` for the existing routines, and `project_get` on the work board for open issues with the `proposal` label or a `<!-- routine:{{name}}:` marker.
2. Digest in parallel: ONE `Task` batch on `model: "fast"`, one entry per conversation (at most 30, the most recent first), each told to `read_conversation <id>` and return JSON {id, goal (one line), steps: [short imperative phrases of the manual steps], tools: [tool/CLI names], repos: [owner/name], repeated_prompt: bool}. Tell each entry: never copy secrets, tokens, env var values or customer data into the answer.
3. Judge yourself (this is the {{tier}} step): cluster the digests by goal and steps. A candidate is a cluster of 3 or more conversations (or 2 with `repeated_prompt`) that no installed skill or existing automation already covers. Classify each as `skill` (on-demand, needs judgement) or `routine` (time-based, can run unattended and only files issues or PRs). Drop any candidate that would need merges, deploys, infra writes or credentials to run unattended; at most, mention it as a skill with explicit approval gates.
4. File at most 3 proposals, the strongest first, with `project_update` `add_issue` on the work board: title `Proposal: <skill|routine> <slug>: <one line>`, labels ["proposal"] if the board has that label, content = evidence (the `conversation://<full id>` links), the steps it would automate, for a routine the suggested cron plus what it reads and writes, for a skill the trigger phrases, the expected time saved, and the marker `<!-- routine:{{name}}:<slug> -->`. Where the marker already exists on an open issue, `update_issue` its evidence instead. Do NOT create the skill, the automation or a run. A human decides.
5. No candidate means no issue. Say so in the summary.
```
