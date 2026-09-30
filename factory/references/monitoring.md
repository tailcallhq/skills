# Monitoring: detection, MCP servers, KB entries

Used by factory phase 4b (monitoring MCP, optional). It follows the same
detect -> confirm -> connect -> ingest pattern as trackers
(`references/trackers.md`). Monitoring is a **read-only source in v1**: factory
reads alerts, errors, deploys and SLOs, and never creates or changes monitors,
alert rules, incidents or silences.

`scripts/detect_monitoring.sh` ranks candidates from the repos cloned in phase
2. Their confirmation goes into the **same batched phase-4 prompt** as the
tracker and infra questions: never one question per repo or per system.
Nothing is installed without that confirmation and the `mcp_add` approval gate.

Every server and endpoint below was checked against the vendor's docs or
canonical repo on 2026-09-30. Each remote URL returned `401` with an auth
challenge to an unauthenticated MCP `initialize`, so it is live and needs
login. Re-check the **Source** link before you change an entry. Never add a
server that has no vendor source or canonical-repo source.

## Credentials

`mcp_add` credential handling is the same as for trackers. See "How Forge
stores MCP credentials" in `references/trackers.md`. In short:

- a remote `url` with no token uses **OAuth** (Forge login flow, tokens kept
  in Forge's store);
- `bearer_token` and `headers` values expand `{{env.VAR}}`;
- stdio `env` values are passed **literally**. Put only non-secret settings
  there. A secret has to be exported in the user's shell so that the child
  process inherits it.

Factory never passes a secret value to `mcp_add`, `state.py` or `kb.py`.

## Summary

| System | Detection signals (weight) | MCP server | `mcp_add` | Auth | Answers | Outbound webhooks |
|---|---|---|---|---|---|---|
| Sentry | SDK dep (0.6): `@sentry/*`, `sentry-sdk`, `sentry` crates, `sentry-go`, `io.sentry`; `sentry.properties` / `.sentryclirc` / `sentry.*.config.[jt]s` (0.55); `getsentry/*` Action (0.55); Terraform `jianyuan/sentry` (0.6); `SENTRY_*` names in CI (0.45), `.env.example` (0.4), compose (0.35) | Sentry MCP (hosted) | `url: https://mcp.sentry.dev/mcp/<org>/<project>` (or `/mcp/<org>`, or `/mcp`) | OAuth (default). Alt: `headers: {"Authorization": "Sentry-Bearer {{env.SENTRY_ACCESS_TOKEN}}"}` | errors / issue groups, stack traces, releases, performance traces, Seer analysis | **Yes**: Integration Platform webhooks (`issue`, `error`, `event_alert`, `metric_alert` resources) |
| Datadog | `dd-trace`, `ddtrace`, `dd-trace-go`, `com.datadoghq` (0.6); `service.datadog.yaml` (0.6); `datadog*.yaml` (0.55); Terraform `DataDog/datadog` (0.6); `datadog/*` Action (0.5); agent image in compose (0.4); `DD_*` / `DATADOG_*` names (0.35-0.45) | Datadog MCP Server (hosted, per site) | `url: https://mcp.<site>/v1/mcp` (e.g. `mcp.datadoghq.com`, `mcp.datadoghq.eu`, `mcp.us5.datadoghq.com`), optional `?toolsets=core,alerting,incidents,apm` | OAuth (default). Alt: `headers: {"DD_API_KEY": "{{env.DD_API_KEY}}", "DD_APPLICATION_KEY": "{{env.DD_APPLICATION_KEY}}"}` | monitors + monitor groups, SLOs, incidents, events (incl. deploys), logs, metrics, traces, RUM | **Yes**: Webhooks integration (`@webhook-<name>` in monitor messages) |
| Grafana (+ Loki/Tempo/Mimir, Grafana OnCall/Incident) | `@grafana/faro-*` (0.6); `grafana/` dir, `grafana.ini`, `*.grafana.json` (0.5); Terraform `grafana/grafana` (0.6); `grafana/*` image in compose (0.4); `GRAFANA_*` names (0.35-0.45) | `mcp-grafana` (Grafana Labs, stdio) | `command: uvx`, `args: ["mcp-grafana"]`, `env: {"GRAFANA_URL": "<url>"}`; the user exports `GRAFANA_SERVICE_ACCOUNT_TOKEN` | Service-account token (Viewer role is enough for reads), inherited from the shell | alert rules + firing state, OnCall alert groups + on-call, incidents, dashboards, PromQL / LogQL queries, annotations (deploy markers) | **Yes**: webhook contact point (Grafana Alerting) |
| Prometheus / Alertmanager | `prom-client`, `prometheus_client`, `client_golang`, `metrics-exporter-prometheus`, micrometer (0.6); `prometheus.yml`, `alertmanager.yml`, `*.rules.yml` (0.5); `ServiceMonitor` / `PodMonitor` / `PrometheusRule` CRDs (0.55); `prom/*` image in compose (0.4); `PROMETHEUS_*` / `ALERTMANAGER_*` names | **Prefer Grafana** when Prometheus sits behind it. Otherwise the Prometheus MCP server (stdio) | `command: docker`, `args: ["run","--rm","-i","-e","PROMETHEUS_MCP_SERVER_PROMETHEUS_URL","ghcr.io/tjhop/prometheus-mcp-server:latest"]`, `env: {"PROMETHEUS_MCP_SERVER_PROMETHEUS_URL": "<url>"}` | None by default. For a secured Prometheus, use the server's `--http.config` file, which the user maintains outside mcp.json | PromQL (instant/range), active alerts, rules, targets, metadata | **Yes**: Alertmanager `webhook_configs` receiver |
| PagerDuty | `@pagerduty/pdjs`, `pdpyras`, `go-pagerduty` (0.6); Terraform `PagerDuty/pagerduty` (0.6); `*pagerduty*.yml/json/tf` (0.5); `pagerduty/*` Action (0.5); `PAGERDUTY_*` names | PagerDuty Remote MCP Server (hosted) | `url: https://mcp.pagerduty.com/mcp` (EU: `https://mcp.eu.pagerduty.com/mcp`) | API token: `headers: {"Authorization": "Token token={{env.PAGERDUTY_API_KEY}}"}`. OAuth needs a pre-registered OAuth client (see entry) | incidents, alerts, services, on-call + schedules, escalation policies, change events (deploys) | **Yes**: v3 webhook subscriptions (`incident.triggered`, `incident.resolved`, ...) |
| OpenTelemetry (collector / SDK) | `@opentelemetry/*`, `opentelemetry-*`, `go.opentelemetry.io/otel`, `tracing-opentelemetry` (0.6); `otel*.yaml` / `otelcol*.yaml` collector config (0.55); `otel/opentelemetry-collector*` or `jaegertracing/*` image (0.4); `OTEL_*` names (0.35-0.45) | **None**. OTel has no official query MCP: the project scopes MCP work to collector configuration and does not include querying backends. The answer lives in the **backend** the collector exports to | n/a | n/a | nothing on its own. Use the exporter target (Grafana Tempo/Loki/Mimir, Datadog, Sentry, ...) | No (a pipeline, not an alerting system) |
| GitHub Actions status | `.github/workflows/*.yml` (0.9) | **None needed**: `gh` is already authenticated in phase 0 (`gh run list`, `gh run view --log-failed`) | n/a | `gh` auth from phase 0 | CI runs, failures, deploy workflows, environments | **Yes**: repo/org webhooks `workflow_run`, `check_suite` |
| PostHog (seen in the wild, not required) | `posthog-js` / `posthog-node` / `posthog-rs` / `posthog` (0.6); `POSTHOG_*` names; `posthog/posthog` image | PostHog MCP (hosted) | `url: https://mcp.posthog.com/mcp` | OAuth (default). Alt: `bearer_token: {{env.POSTHOG_PERSONAL_API_KEY}}` | error tracking issues, product analytics / HogQL, feature flags | **Yes**: CDP webhook destination |

Confidence per system is a noisy-OR over its strongest signal of each kind in
each repo: `1 - prod(1 - w)`. With the weights above, one env var name alone
stays below 0.5, and an SDK dependency plus a config file reaches about 0.8.

How the skill reads the result:

- **Skip without asking**: `github-actions` (covered by `gh`) and
  `opentelemetry` (a pipeline, not a data source). Instead, ask which backend
  the collector exports to. `OTEL_EXPORTER_OTLP_ENDPOINT` hosts in CI help with
  that question.
- **Offer as the default** any other system with confidence >= 0.6, together
  with its evidence and the repos it came from ("Detected **Sentry** (0.97) in
  `acme/shop`, `acme/api`: SDK + sentry.properties. Connect it read-only?").
- **List systems between 0.3 and 0.6** as "maybe" in the same prompt.
- **Nothing detected**: ask once whether the org uses a monitoring system not
  listed here. "None" is a valid answer, and phase 4b is then `skipped`.
- **Prometheus next to Grafana**: install only Grafana and query Prometheus
  through its datasource. That is one credential instead of two.
- Several systems may be connected. Each one is a separate `mcp_add` inside
  the same batched approval.

## Entries

### Sentry

- **Source**: [docs.sentry.io/product/sentry-mcp](https://docs.sentry.io/product/sentry-mcp/),
  [getsentry/sentry-mcp](https://github.com/getsentry/sentry-mcp) (README
  "Remote with an Explicit Sentry Token").
- **Server**: `https://mcp.sentry.dev/mcp`. Scope it to an org and project
  with `/mcp/<org>/<project>`, which Sentry recommends because it hides
  discovery tools. Use the detector's `hints.org` / `hints.project`, which come
  from `sentry.properties` `defaults.org` / `defaults.project`, or
  `SENTRY_ORG` / `SENTRY_PROJECT` in CI. Several projects in one org: scope
  the server to `/mcp/<org>` only.
- **Auth**: OAuth. For a non-interactive routine without OAuth, use
  `Sentry-Bearer` (not `Bearer`, which is reserved for MCP OAuth) with a user
  auth token that has read scopes only.
- **Self-hosted Sentry**: the hosted endpoint serves sentry.io only. The
  documented alternative is stdio
  [`@sentry/mcp-server`](https://www.npmjs.com/package/@sentry/mcp-server)
  (`npx @sentry/mcp-server@latest --host=<host>`, with `SENTRY_ACCESS_TOKEN`
  exported in the user's shell). Its AI-powered search tools also need an LLM
  provider key, so say so before offering it.
- **Webhooks**: [Integration Platform webhooks](https://docs.sentry.io/organization/integrations/integration-platform/webhooks/),
  delivered by an internal integration. Resources include `issue`, `error`,
  `event_alert` and `metric_alert`.

### Datadog

- **Source**: [Datadog MCP Server setup](https://docs.datadoghq.com/mcp_server/setup/),
  [tools reference](https://docs.datadoghq.com/mcp_server/tools/),
  [datadog-labs/mcp-server](https://github.com/datadog-labs/mcp-server).
- **Server**: `https://mcp.<site>/v1/mcp`. `<site>` is the org's Datadog site
  (`datadoghq.com` = US1, `us3.datadoghq.com`, `us5.datadoghq.com`,
  `datadoghq.eu`, `ap1.datadoghq.com`, ...). Take it from the detector's
  `hints.site` (`DD_SITE`) or ask. **Not available on the GovCloud sites**
  (`ddog-gov.com`): say so and skip. Add `?toolsets=core,alerting,incidents`
  to keep the tool list small. The `alerting` toolset covers monitors and SLOs.
- **Auth**: OAuth 2.0 (default). Datadog documents API key and application
  key headers as the fallback. Factory writes those headers only as
  `{{env.VAR}}` templates, and the application key should be scoped read-only.
- **Webhooks**: [Webhooks integration](https://docs.datadoghq.com/integrations/webhooks/).
  A monitor notifies `@webhook-<name>` with a templated payload (`$ALERT_ID`,
  `$ALERT_STATUS`, ...).

### Grafana (and Prometheus behind it)

- **Source**: [grafana/mcp-grafana](https://github.com/grafana/mcp-grafana),
  published on PyPI as [`mcp-grafana`](https://pypi.org/project/mcp-grafana/)
  and as the `grafana/mcp-grafana` Docker image.
- **Server**: stdio, `uvx mcp-grafana` (requires `uv`). Alternatives are the
  `mcp-grafana` binary from the GitHub releases, or Docker
  (`grafana/mcp-grafana -t stdio`). Pass `--disable-write` so the server is
  read-only, which matches the v1 policy. It also removes the raw-SQL query
  tools.
- **Settings**: `GRAFANA_URL` goes in `env`. It is not a secret, and the
  detector reports it as `hints.url` when a template sets it. The user exports
  `GRAFANA_SERVICE_ACCOUNT_TOKEN` in their shell: a service account with the
  Viewer role (plus OnCall read, if OnCall is used). Do not write the token
  into `env`, because stdio `env` is not templated.
- **Answers**: alert rules and their state, OnCall alert groups and who is on
  call, Grafana Incident, dashboards, Prometheus/Loki/Tempo queries through
  datasources, and annotations (often deploy markers).
- **Webhooks**: [webhook contact point](https://grafana.com/docs/grafana/latest/alerting/configure-notifications/manage-contact-points/integrations/webhook-notifier/).

### Prometheus / Alertmanager (standalone)

- **Source**: [prometheus/prometheus-mcp](https://github.com/prometheus/prometheus-mcp),
  the official Prometheus MCP server, adopted into the `prometheus` org from
  `tjhop/prometheus-mcp-server`. Its README still publishes the image as
  `ghcr.io/tjhop/prometheus-mcp-server`.
- **Server**: stdio via Docker (above) or the release binary, configured with
  `PROMETHEUS_MCP_SERVER_PROMETHEUS_URL` / `--prometheus.url`. Use it only
  when no Grafana fronts the Prometheus: through Grafana, the same data is
  reachable with one fewer credential.
- **Auth**: none for an open Prometheus. For a secured instance, use
  `--http.config <file>` (Prometheus HTTP client config, maintained by the
  user outside `mcp.json`). A Prometheus that is reachable only inside a
  cluster is often not reachable from the Forge machine. Probe it with
  `curl -s <url>/-/ready` before installing, and skip with a note if it fails.
- **Webhooks**: Alertmanager [`webhook_config`](https://prometheus.io/docs/alerting/latest/configuration/#webhook_config).

### PagerDuty

- **Source**: [PagerDuty MCP Server](https://support.pagerduty.com/main/docs/pagerduty-mcp-server),
  [MCP tooling & remote server](https://docs.pagerduty.com/developer/mcp-tooling-remote-server).
  The local server [`PagerDuty/pagerduty-mcp-server`](https://github.com/PagerDuty/pagerduty-mcp-server)
  (`uvx pagerduty-mcp`) is **deprecated and read-only**. Do not install it.
- **Server**: `https://mcp.pagerduty.com/mcp` (US) or
  `https://mcp.eu.pagerduty.com/mcp` (EU). Ask the region when the
  service-region URL is not in the repo.
- **Auth**: a User API token as
  `Authorization: Token token={{env.PAGERDUTY_API_KEY}}`. PagerDuty's OAuth
  needs a pre-registered OAuth client (client id/secret and a static callback
  port). If the user has one, try Forge's OAuth login first. If the login
  fails, fall back to the token header and do not work around it. Scoped OAuth
  or a read-only API key is preferred for routines.
- **Webhooks**: [v3 webhook subscriptions](https://developer.pagerduty.com/docs/webhooks-overview)
  (`incident.triggered`, `incident.acknowledged`, `incident.resolved`, ...).

### OpenTelemetry collectors

- **Source**: [OpenTelemetry Collector](https://opentelemetry.io/docs/collector/).
  The OTel community's MCP project is scoped to collector configuration and
  explicitly excludes querying data in a backend. Community servers exist,
  but none is an OpenTelemetry project, so none is suggested.
- **Use**: detection tells factory that telemetry exists and where it is
  configured. Record it in the KB (`kind: opentelemetry`, the collector config
  path as source), then find the backend from the collector's `exporters:` or
  from `OTEL_EXPORTER_OTLP_ENDPOINT`, and connect **that** system instead.
- **Webhooks**: none. A collector is a pipeline and has no alerting.

### GitHub Actions status

- **Source**: [`gh run`](https://cli.github.com/manual/gh_run). Factory
  already requires `gh auth status` with the `repo` scope in phase 0.
- **Use**: no MCP. CI failure intake (a phase-6 routine) polls
  `gh run list --status failure --json ...` per repo. If the user wants the
  GitHub MCP anyway, see the GitHub entry in `references/trackers.md`
  (`actions` toolset).
- **Webhooks**: [`workflow_run`, `check_suite`](https://docs.github.com/en/webhooks/webhook-events-and-payloads).

### PostHog

Not in the required list. It is included because the detector finds it in
real repos (`forgecode-sdk` ships `svc-posthog`).

- **Source**: [posthog.com/docs/model-context-protocol](https://posthog.com/docs/model-context-protocol)
  (server source in `PostHog/posthog`, `services/mcp`).
- **Server**: `https://mcp.posthog.com/mcp`. The auth server routes US or EU
  by account.
- **Auth**: OAuth. For clients without OAuth, a personal API key as a bearer
  token: `bearer_token: {{env.POSTHOG_PERSONAL_API_KEY}}`.
- **Scope note**: PostHog is often product analytics, not alerting. Offer it
  as a monitoring source only when error tracking is used, and ask to confirm.
- **Webhooks**: [CDP webhook destination](https://posthog.com/docs/cdp/destinations/webhook).

## Outbound webhooks and push triggers

Every system marked **Yes** above can push. Forge has **no inbound webhook
endpoint today** (verified constraint: `automation_*` is cron-only). v1
therefore turns alerts into board issues with the **polling** alert-intake
routine (issue 7), which uses the cursor in
`routines.alert_intake.cursor`. The webhook column records what becomes
possible once a `webhook_*` tool exists. Factory probes for that tool at
runtime and must not promise push delivery before then.

## Knowledge base integration

Each confirmed system becomes a `monitoring:` entry on every KB system file it
covers. The detector's `per_repo` output maps repo -> system -> `sources`
(repo-relative files) and `hints` (org / project / service / site / url).
Phase 3 maps each repo to a KB system. For each (KB system, monitor) pair,
call:

```bash
kb.py add-monitor <system> --kind sentry --project <slug> \
  --source <owner/repo>:<path> [--source ...] [--org <org>] [--site <site>] [--url <url>]
```

- `<system>`: the KB system id (`systems/<system>.md`) that owns the repo.
- `--kind`: the detector's `system` (`sentry`, `datadog`, `grafana`,
  `prometheus`, `pagerduty`, `opentelemetry`, `posthog`). Do not write a
  `github-actions` entry, because CI is recorded under the repo.
- `--project`: the vendor-side identifier. That is the Sentry project slug,
  the Datadog `service`, the OTel `service.name`, the PagerDuty service name,
  or the Grafana folder/dashboard. Take it from `hints`. If there is no hint,
  ask in the phase-4 batch or omit it: it is optional and never guessed.
- `--source`: each file from `per_repo[].candidates[].sources`, as
  `owner/repo:path`. At least one is required, because every KB fact is
  source-cited. A value the user supplied is recorded as `--source user`.

`kb.py` (issue 1a) writes the entry with `verified: <today>`, refuses any
value that matches a token pattern, and dedupes on
(`system`, `kind`, `project`). Resulting block in `systems/<system>.md`:

```yaml
monitoring:
  - kind: sentry
    org: acme
    project: shop-web
    mcp: sentry            # mcp_add alias, once connected (phase 4b)
    source: [acme/shop:sentry.properties, acme/shop:web/package.json]
    verified: 2026-09-30
```

The `mcp` alias is set only after `mcp_add` succeeds. A detected but
unconnected monitor stays in the KB without it, so later routines know it
exists.

## State

Phase 4b records its outputs in `.agents/factory-state.json` (see
`references/state.md`). Never record tokens there.

- `outputs.detected`: comma-separated systems.
- `outputs.connected`: the `mcp_add` aliases that were added.
- `outputs.skipped`: systems the user declined.

A blocker such as "export `GRAFANA_SERVICE_ACCOUNT_TOKEN`" makes the phase
`blocked`. On resume, the phase re-checks the env var **name** only
(`[ -n "${VAR:-}" ]`) and never echoes its value.

## What the script does not detect

- Systems configured only in a vendor UI or in infra repos that were not
  cloned, for example a Datadog agent that is installed by the platform team.
  The user is the source for these (`source: user`).
- New Relic, Honeycomb, Splunk, Elastic/Kibana, Rollbar, Bugsnag,
  Opsgenie/incident.io: there is no detection rule and no entry. Add one only
  when a vendor MCP source has been verified.
- Values in real `.env` files. The script never reads them: it reads only
  committed templates (`.env.example`, `.env.sample`, `.env.template`, ...).

## Script reference

`scripts/detect_monitoring.sh [--jobs N] [DIR...]`

- Targets: local checkouts (phase 2 has cloned them). Default: the current
  directory. A missing directory is reported in `warnings` and skipped.
- Read-only and offline: `git ls-files`, then `grep`/`awk` over manifests,
  configs, CI, compose files and env templates. No `gh` and no network, so the
  script has no rate-limit cost. It never prompts.
- Scope: git-tracked and untracked-but-not-ignored files. It skips
  `node_modules/`, `vendor/`, `third_party/`, `.forge/`, `target/`, `dist/`,
  `build/`, `examples/`, `fixtures/`, `testdata/`, and lockfiles.
- Secrets: only env var **names** are emitted. The `hints` values come from an
  allowlist of non-secret keys (`SENTRY_ORG`, `SENTRY_PROJECT`, `DD_SITE`,
  `DD_SERVICE`, `DD_ENV`, `OTEL_SERVICE_NAME`, `GRAFANA_URL`,
  `POSTHOG_HOST`, `defaults.org` / `defaults.project`, `dd-service`). They
  must also look like a slug or a bare `http(s)://host[:port]` origin.
- Concurrency: `--jobs 8` repos at a time (local I/O only). One file-list
  pass and one batched `grep` per rule per repo: about 2 s for a 1,300-file
  Rust workspace.
- Output: `{candidates, per_repo, scanned, warnings}`. Exit 0 on any partial
  failure (reported in `warnings`), 2 only on bad arguments.
