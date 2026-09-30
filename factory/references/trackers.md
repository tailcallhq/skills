# Task trackers: detection and MCP servers

Used by factory phase 4 (tracker MCP). `scripts/detect_tracker.sh` produces
ranked candidates; the skill shows the top one (with its evidence) and asks
the user to confirm or correct it in the same batched prompt as the other
phase-4 questions. Nothing here is installed without that confirmation and the
`mcp_add` approval gate.

Every server below was checked against the vendor's own docs on 2026-09-30.
Re-check the **Source** link before changing an entry; never add a server that
has no vendor or canonical-repo source.

## How Forge stores MCP credentials

Verified in `forgecode-sdk` (`crates/svc-mcp`):

| `mcp_add` field | `{{env.VAR}}` expanded? | Use for |
|---|---|---|
| `url` + no token | n/a | **OAuth**: Forge detects the server's OAuth metadata and offers a login (`mcp.login_<alias>`); supports dynamic client registration. Tokens live in Forge's store, not `mcp.json`. |
| `bearer_token` | yes (`resolve_bearer_token`) | Remote servers that take `Authorization: Bearer <token>`. Write `{{env.VAR}}`, never the token. |
| `headers` | yes (`resolve_http_templates`) | Any other auth header, e.g. `Authorization: Basic {{env.VAR}}`. |
| stdio `env` | **no**, passed literally | Non-secret settings only. The child process inherits the host environment, so a secret the server reads (e.g. `GITHUB_PERSONAL_ACCESS_TOKEN`) must be exported in the user's shell, not written into `env`. |

An unset `{{env.VAR}}` expands to an empty string and the server shows as
"Unconfigured", which is recoverable; a pasted literal token is not. Factory
therefore never passes a secret value to `mcp_add`.

## Summary

| Tracker | Detection signals (weight) | MCP server | Transport / `mcp_add` | Auth | Ask the user |
|---|---|---|---|---|---|
| GitHub Issues / Projects | open issues (0.25, 0.4 if >= 10); `.github/ISSUE_TEMPLATE` (0.3); `fixes #N` closing keywords (0.3); Projects v2 on repo (0.3) / org (0.2) | **None needed**: `gh` is already authenticated in phase 0. Optional: GitHub MCP Server | `url: https://api.githubcopilot.com/mcp/` (append `readonly` or `x/<toolset>` to narrow) | PAT via `bearer_token: {{env.GITHUB_PERSONAL_ACCESS_TOKEN}}`; remote OAuth needs a host-registered GitHub App, so do not rely on it | Which Project (number) is the board of record, if any. Whether to add the MCP at all (default: no, use `gh`). |
| Linear | `linear.app/<ws>/issue/KEY-1` URLs in commits (0.45) / PR bodies (0.5) / docs (0.3); keys whose prefix appears in a Linear URL (0.35-0.5); `user/key-123-title` branch names (0.35); bare `ABC-123` keys (0.2-0.3, shared with Jira) | Linear remote MCP | `url: https://mcp.linear.app/mcp` (`/mcp/readonly` for read-only) | OAuth 2.1 (default). Alternative: API key via `bearer_token: {{env.LINEAR_API_KEY}}` | Workspace; which team key(s) map to which repos. |
| Jira | `*.atlassian.net/browse/KEY-1` or self-hosted `/browse/KEY-1` URLs (0.3-0.5); keys whose prefix appears in such a URL (0.35-0.5); smart commits `KEY-1 #comment/#time/#done` (0.4); Confluence links (0.15, site only); bare keys (0.2-0.3, shared with Linear) | Atlassian Rovo MCP Server (v2) | `url: https://mcp.atlassian.com/v2/mcp` | OAuth 2.1 (default). If the org admin enabled API-token auth: `headers: {"Authorization": "Basic {{env.ATLASSIAN_BASIC_AUTH}}"}` (base64 of `email:api_token`) or `bearer_token: {{env.ATLASSIAN_API_KEY}}` (service-account key) | Site (`<site>.atlassian.net`); project key(s) per repo. Cloud only: a self-hosted Jira (Server/Data Center) is **not** served by this endpoint; say so and stop. |
| Notion | `notion.so` / `notion.site` URLs in commits, PR bodies, docs (0.3-0.5) | Notion MCP (hosted) | `url: https://mcp.notion.com/mcp` | OAuth only (Notion: no non-interactive auth yet) | Which database is the task list. Notion is often docs, not tracking: confirm it is really the tracker. |
| Asana | `app.asana.com` URLs (0.3-0.5) | Asana MCP V2 | `url: https://mcp.asana.com/v2/mcp` | OAuth. Some clients need a pre-registered client id/secret; if Forge's DCR login fails, stop and point the user to Asana's integration guide | Workspace and project(s). |
| Trello | `trello.com/b/` or `/c/` URLs (0.3-0.5) | Trello MCP (Atlassian, hosted) | `url: https://mcp.trello.com/v1` | OAuth 2.0 only (no API-token auth for Trello MCP) | Workspace and board(s). |

Confidence per tracker = noisy-OR over its strongest signal of each kind per
repo: `1 - prod(1 - w)`. One weak signal alone stays below 0.5. Suggested
reading for the skill:

- top candidate >= 0.6 and >= 0.2 ahead of the next: present it as the
  default ("Detected **Linear** (0.73): <evidence>. Use it?").
- otherwise: list the top two or three with evidence and ask the user to pick.
- no candidates: ask which tracker they use (or "none": GitHub Issues via `gh`).
- `per_repo` disagrees across repos (mixed org): ask once, listing the repos
  per tracker; installing more than one tracker MCP is allowed.

## Entries

### GitHub Issues / Projects

- **Source**: [github/github-mcp-server](https://github.com/github/github-mcp-server)
  (README "Remote GitHub MCP Server"; `docs/remote-server.md` for per-toolset
  URLs and `/readonly`).
- **Default**: no MCP. Factory already requires `gh auth status` with `repo`
  scope; issues and PRs are read with `gh`. Projects v2 additionally need the
  `read:project` scope (`gh auth refresh -s read:project`, user-run). The
  script warns when it is missing.
- **If the user wants the MCP** (e.g. for other agents): remote,
  `mcp_add {alias: "github", url: "https://api.githubcopilot.com/mcp/", bearer_token: "{{env.GITHUB_PERSONAL_ACCESS_TOKEN}}"}`.
  Ask them to export a fine-grained PAT under that name. The README says OAuth
  on the remote server requires the MCP host to ship a GitHub App, so do not
  promise an OAuth login. GitHub Enterprise Server is not supported remotely;
  the local server (`ghcr.io/github/github-mcp-server`, stdio, Docker) is the
  documented path there.
- **Ask**: board-of-record Project number (if Projects are used), or "issues only".

### Linear

- **Source**: [linear.app/docs/mcp](https://linear.app/docs/mcp).
- **Server**: `https://mcp.linear.app/mcp` (Streamable HTTP); read-only at
  `https://mcp.linear.app/mcp/readonly`. SSE `/sse` is deprecated; do not use.
- **Auth**: OAuth 2.1 with dynamic client registration (Forge login flow).
  Alternative per Linear's FAQ: `Authorization: Bearer <api key>` via
  `bearer_token: "{{env.LINEAR_API_KEY}}"`, e.g. a read-only key for routines.
- **Detection note**: Linear's "copy git branch name" produces
  `user/abc-123-title`; issue keys are shared with Jira, so bare keys only
  count strongly once a `linear.app/.../issue/KEY-n` URL confirms the prefix.
- **Ask**: workspace (one OAuth session per workspace), team keys per repo.

### Jira (Atlassian Cloud)

- **Source**: [Get started with the Atlassian MCP server](https://support.atlassian.com/atlassian-rovo-mcp-server/docs/getting-started-with-the-atlassian-remote-mcp-server/),
  [Configure authentication via API token](https://support.atlassian.com/atlassian-ai-gateway/docs/configure-authentication-via-api-token/),
  [Supported tools](https://support.atlassian.com/atlassian-ai-gateway/docs/supported-tools/).
- **Server**: `https://mcp.atlassian.com/v2/mcp` (v2; v1 auto-migrates on
  2027-03-01). Covers Jira, Confluence, Compass, Bitbucket; tool calls consume
  Rovo credits, so mention the cost when the user opts into polling routines.
- **Auth**: OAuth 2.1 by default. API-token auth only if the org admin enabled
  it: personal token as `Basic base64(email:api_token)` in a header
  (`headers: {"Authorization": "Basic {{env.ATLASSIAN_BASIC_AUTH}}"}`) or a
  service-account key as `bearer_token: "{{env.ATLASSIAN_API_KEY}}"`. The user
  computes and exports the base64 value; factory never sees it.
- **Limits**: Cloud sites only. Self-hosted Jira (the script's `/browse/KEY-n`
  match also fires on e.g. `issues.apache.org/jira`) has no vendor MCP here:
  record the tracker in state as `jira-server`, skip the install, and tell the
  user. No community server is suggested.
- **Ask**: site URL, project keys per repo, OAuth vs admin-enabled API token.

### Notion

- **Source**: [Connect to Notion MCP](https://developers.notion.com/docs/get-started-with-mcp).
- **Server**: `https://mcp.notion.com/mcp` (Streamable HTTP). The open-source
  `@notionhq/notion-mcp-server` ([makenotion/notion-mcp-server](https://github.com/makenotion/notion-mcp-server))
  is "no longer actively maintained" per Notion; do not install it.
- **Auth**: OAuth only; Notion states non-interactive authorization is not yet
  supported, so cron routines can only use it after the user has logged in on
  that machine.
- **Ask**: which database holds tasks; confirm Notion is the tracker and not
  just the wiki (URLs alone cannot tell).

### Asana

- **Source**: [Using Asana's MCP Server](https://developers.asana.com/docs/using-asanas-mcp-server).
- **Server**: `https://mcp.asana.com/v2/mcp` (Streamable HTTP). The beta
  `https://mcp.asana.com/sse` is deprecated with an announced shutdown; never use it.
- **Auth**: OAuth. Asana notes some clients need a client id/secret from their
  [integration guide](https://developers.asana.com/docs/integrating-with-asanas-mcp-server);
  if Forge's login fails with "client not found", surface that link rather
  than inventing a workaround. Enterprise admins may block MCP clients.
- **Ask**: workspace, project(s).

### Trello

- **Source**: [atlassian/trello-mcp-server](https://github.com/atlassian/trello-mcp-server)
  ("Official remote MCP server for Trello").
- **Server**: `https://mcp.trello.com/v1`.
- **Auth**: OAuth 2.0 consent per workspace. The README states Trello MCP does
  not support API-token auth (the Rovo API-token setting does not apply).
- **Not used**: the npm package `atlassian-trello-mcp` is a community fork
  (agrath/Trello-Desktop-MCP), not published by Atlassian despite its name.
- **Ask**: workspace, board(s) that hold active work.

## What the script does not detect

- Trackers with no footprint in git/PRs/docs (e.g. a team that never links
  tickets). The user is the source; record `source: user` in state.
- Shortcut, YouTrack, GitLab issues, Azure Boards, ClickUp, Monday: no
  detection and no entry. Add one only with a verified vendor MCP source.
- Private-repo PR bodies when `gh` lacks access: signals degrade to local
  commits and docs, with a warning.

## Script reference

`scripts/detect_tracker.sh [--commits N] [--prs N] [--jobs N] [--no-gh] [TARGET...]`

- Targets: local checkouts (preferred: commits and docs are free) and/or
  `owner/name` slugs. Default: current directory.
- Read-only: `git log`, `find`/`cat` of README/CONTRIBUTING/.github, `gh pr
  list`, `gh api` GETs and one read-only GraphQL query. Never prompts.
- Cost: about 3 `gh` calls per local repo, 4 per slug; `--jobs 4` repos at a
  time. Reads `gh api rate_limit` first and falls back to local-only signals
  below 50 remaining core/GraphQL requests.
- Output: `{candidates, per_repo, scanned, warnings}`; exit 0 on any partial
  failure (reported in `warnings`), 2 only on bad arguments.
