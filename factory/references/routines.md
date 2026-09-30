# Routines (placeholder; issue 5 replaces this file)

Cron + polling only (`automation_*`, cloud machines). Every routine files board issues or opens KB PRs; none merges or touches infra.
Names: `kb_refresh` (stale facts -> KB PR), `board_sync` (KB questions -> "Knowledge base" issues), `repo_health` (CI/deps via `repo-health`),
`ci_failure_intake` (failed default-branch runs -> issues), `alert_intake` (monitoring MCP alerts -> issues, cursor in state), `infra_drift` (probe + read -> `infra-change` issues).
Schedules, prompts and allowlists: TBD by issue 5.
