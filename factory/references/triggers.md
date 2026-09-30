# Event triggers: CI failures and alerts -> board issues

Phase 6 can turn two kinds of events into issues on the org's work board
(phase 5 `project_id`): **failed GitHub Actions runs** and **firing monitoring
alerts**. v1 does this by **polling**. Forge `automation_*` is cron-only, and
forgecode-sdk has no inbound webhook endpoint. The push design, and how factory
upgrades itself to it, is in [webhooks](webhooks.md).

What stays the same whichever way events arrive:

- `scripts/intake.py` does the deterministic work: fetch or parse events,
  dedupe them, map them to KB systems, redact secrets, and emit the payloads
  and the new cursor. It **never** files an issue, never calls `project_run`
  and never writes state.
- The routine prompt does the side effects: `project_update` for the issues
  and `state.py set-routine` for the cursor. It runs `project_run` only for
  payloads marked `auto_run: true`.
- `auto_run` is `true` only when the system and label match the user's
  allowlist (below). With no allowlist, nothing runs by itself.
- Routines never merge, never touch infra, never create or silence monitors,
  and never acknowledge incidents.

## The two routines

Both need `machine.cloud: true` (probed in phase 0). The cursor lives at
`routines.<id>.cursor` in `.agents/factory-state.json`, see
[state](state.md). The `<placeholders>` are filled in at `automation_create`
time from state, so every prompt is a complete standalone instruction.

| id | schedule (UTC) | source | label | cost per run |
|---|---|---|---|---|
| `ci_failure_intake` | `*/30 * * * *` | `gh run list --status failure` per org repo | `ci-failure` | 1 `gh` call per repo (20 repos x 48 runs/day = 960 of 5000/h core) |
| `alert_intake` | `*/15 * * * *` | one read query per connected monitoring MCP (phase 4b `connected`) | `alert` | 1 MCP query per source, and no model work when nothing is new |

Use `overlap_policy: skip` for both (they write to the board). Keep the default
`wake_policy: resume`.

### Cursor semantics

`intake.py` prints `{"cursor": "<poll time>;seen=<hashes>", ...}`. Each poll
re-reads `--lookback` hours (default 6) before the stored poll time, because a
run finishes after it is created and alerts can arrive late. Items whose key
hash is in `seen` are counted as `duplicates` and not emitted again. An empty
cursor (first run) covers the last 24 hours.

Dedupe keys:

- CI: `repo + workflow + branch + head_sha`. A re-run of the same commit is
  one issue.
- Alerts: `source + fingerprint`. The fingerprint is Sentry issue `id`,
  Datadog `aggregation_key` or `monitor_id|group`, PagerDuty `incident_key`,
  or Grafana `fingerprint`.

Resolved or recovered alerts are not filed. Every payload body also ends with
`<!-- factory-intake: <hash> -->`, so the routine can check the board before
filing and stay idempotent even if the cursor is lost.

**Write the cursor only after every payload is filed.** A crashed run then
re-reads the same window, and the board-marker check stops duplicates.

### Routine: `ci_failure_intake`

```yaml
name: "factory: CI failure intake (<org>)"
trigger: {cron: "*/30 * * * *"}
overlap_policy: skip
prompt: |
  You are the factory CI-failure intake routine for the GitHub org <org>.
  Work in <workspace>. Do not merge, push, re-run workflows or change anything
  outside the board and the state file.
  1. CURSOR=$(python3 <skill>/scripts/state.py get routines.ci_failure_intake.cursor | tr -d '"')
  2. python3 <skill>/scripts/intake.py ci --org <org> --since "$CURSOR" \
       --graph .agents/graph.json --kb <kb_path> [--allowlist .agents/triggers-allowlist.json] \
       --jobs 8 > .agents/intake-ci.json
     Exit != 0: stop and report the stderr line. `warnings`: include them in your report.
  3. For each entry in `.issues`: project_get <project_id>. If an issue already
     contains the body's `factory-intake: <hash>` marker, skip it. Otherwise file
     ONE project_update batch that creates every remaining issue with `title`,
     `body`, `labels` and `priority` exactly as given. Do not rewrite, summarize
     or add log contents.
  4. Only for payloads with "auto_run": true, run project_run on the created issue.
     Never run any other issue.
  5. After step 3 succeeded: python3 <skill>/scripts/state.py set-routine ci_failure_intake \
       --cursor "$(jq -r .cursor .agents/intake-ci.json)"
  6. Report in <= 3 lines: new issues (links), duplicates, auto-run issues, warnings.
```

### Routine: `alert_intake`

The MCP query is the only step that needs the model. Everything after it is
`intake.py`. Use one block per connected source, and read the query shape from
the source's entry in [monitoring](monitoring.md):

| source | read-only query in the prompt |
|---|---|
| `sentry` | unresolved issues seen since the cursor (Sentry MCP issue search, `is:unresolved lastSeen:-<window>`) |
| `datadog` | monitors in `Alert`/`Warn` state plus incidents (Datadog MCP, `alerting`/`incidents` toolsets) |
| `pagerduty` | incidents with `status` triggered or acknowledged, `since` = cursor |
| `grafana` | firing alert instances (`mcp-grafana` alerting tools, `--disable-write`) |

```yaml
name: "factory: alert intake (<org>)"
trigger: {cron: "*/15 * * * *"}
overlap_policy: skip
prompt: |
  You are the factory alert-intake routine for <org>. Work in <workspace>.
  Monitoring is READ-ONLY: never acknowledge, resolve, silence, mute or edit
  anything; never touch infra.
  1. CURSOR=$(python3 <skill>/scripts/state.py get routines.alert_intake.cursor | tr -d '"')
  2. For each source in [<connected sources, e.g. sentry, pagerduty>]:
     a. Run the source's read-only query through MCP alias <alias> for items active
        since the time part of $CURSOR (minus 6h). Save the raw tool result JSON,
        unmodified, to .agents/alerts-<source>.json. If the MCP is unavailable,
        note it and continue with the next source.
     b. python3 <skill>/scripts/intake.py alerts --source <source> --since "$CURSOR" \
          --kb <kb_path> [--allowlist .agents/triggers-allowlist.json] \
          < .agents/alerts-<source>.json > .agents/intake-<source>.json
  3. Merge `.issues` from every intake-<source>.json. Dedupe against the board
     exactly as in the CI routine (project_get <project_id>, `factory-intake:` marker),
     then file ONE project_update batch using the payload fields as given.
     Never paste raw alert JSON into an issue: the payload body is already redacted.
  4. project_run only payloads with "auto_run": true.
  5. After step 3 succeeded, store the NEWEST cursor among the sources:
     python3 <skill>/scripts/state.py set-routine alert_intake --cursor "<cursor>".
     If any source failed in step 2a, keep the old cursor instead (the next run re-reads).
  6. Report in <= 3 lines: new issues per KB system, duplicates, auto-run, skipped sources.
```

### Creating them (phase 6)

1. Offer both routines in the phase 6 catalog. Show the schedule, the sources
   (the phase 4b `connected` aliases, plus `gh`) and the cost line from the
   table above.
2. Ask the allowlist question in the same message, with the warning below.
   The default answer is "none".
3. After approval, write `.agents/triggers-allowlist.json` (only if the user
   chose rules), `automation_create` each routine with its placeholders
   filled in, `automation_get` to verify, then
   `state.py set-routine <id> --automation-id <aid>` immediately. The cursor
   stays empty until the first run.
4. Offer `automation_control run_now` once per routine, so the user sees the
   first poll's result.

## Allowlist: `.agents/triggers-allowlist.json`

```json
{
  "version": 1,
  "auto_run": [
    {"system": "api", "label": "ci-failure"},
    {"system": "web", "label": "alert"}
  ]
}
```

- A rule is an **exact** (KB system id, label) pair. Labels are `ci-failure`
  or `alert`. Wildcards and `system: unknown` are refused: an event factory
  cannot attribute must never start work on its own.
- Anything unmatched is filed with `auto_run: false` and waits for a human.
- The file holds ids only, never credentials. It is local to the workspace,
  not the KB. Changing it takes effect on the next poll, with no
  `automation_update` needed.

> **Token-cost warning (say this before accepting any rule).** Every
> auto-run issue is a full agent run through `project_run`, with its own model
> tokens and wall time. A flapping alert or a broken main branch can file a new
> issue on every poll: `*/15` means up to 96 runs a day per noisy source.
> Allowlist only systems whose failures are cheap to investigate, and start
> with CI (dedupe per commit bounds it). Revisit the list if the board fills
> with auto-run issues.

## Payload shape (what `intake.py` emits)

```jsonc
{
  "title": "CI failure: ci on main (acme/api)",     // or "Alert (sentry): KeyError in checkout"
  "body": "... run/alert URL ...\n\nKB system: `api`\n<!-- factory-intake: 3f2a9c01de4b -->",
  "labels": ["ci-failure"],                          // or ["alert"]
  "system": "api",                                   // KB system id, or "unknown"
  "priority": "medium",                              // alerts with fatal/critical/error/high/p1/p2 -> "high"
  "url": "https://github.com/acme/api/actions/runs/1",
  "source": "github-actions",                        // or sentry | datadog | pagerduty | grafana
  "occurred_at": "2026-09-30T07:00:00Z",
  "dedupe_key": "3f2a9c01de4b",
  "auto_run": false,
  // ci:     repo, workflow, branch, head_sha
  // alerts: fingerprint, service
}
```

System mapping, from the KB: repo via `systems/*.md` `repos:`. For an alert,
the lookup order is the monitor's `project` for that vendor, then
`runtime.environments[]` ids and URL hosts, then system ids and repo names
(an environment suffix such as `-prod` or `-staging` is stripped), then
`connections.md` rows `X -> external:<host>`. No match gives
`system: unknown` plus the payload as normal. It never crashes the poll.

Redaction: every title, body, URL and fingerprint goes through `kb.py`'s
secret patterns (cloud keys, GitHub/Slack tokens, JWTs, `password=`-style
assignments, private keys), plus bearer tokens, `user:pass@` URL credentials
and secret query parameters. Hostnames are kept, since the KB is private.
Bodies are capped at 1500 characters.
