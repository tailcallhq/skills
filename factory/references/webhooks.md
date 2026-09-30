# Push triggers (design note; not active)

**Status:** design only. v1 polls, see [triggers](triggers.md). Forge
`automation_*` is cron-only, and forgecode-sdk has no inbound webhook endpoint
that factory can call. (The `automation_create` tool text mentions webhooks
"configured from the Automations page". That is a UI path, not something a
skill can set up or verify, so it does not change v1.) This note fixes the
design so that the switch is a configuration change, not a rewrite.

## Runtime detection: factory upgrades itself

Factory never assumes push. It probes for it:

1. Phase 0, and every phase 6 resume: run `tool_search webhook`. Look for a
   tool family that can **create an inbound endpoint and return its URL and
   signing secret reference** (expected names: `webhook_create`,
   `webhook_list`, `webhook_delete`, or an `automation_create` trigger of type
   `webhook`).
2. None found: `state.py set machine.push_triggers false`. Routines poll. Do
   not mention webhooks to the user beyond the phase 0 capability line.
3. Found: `state.py set machine.push_triggers true`, and on the next phase 6
   (or `state: complete` maintenance) offer the upgrade: "Push triggers are
   now available: switch `ci_failure_intake` / `alert_intake` from polling
   every 15-30 min to instant delivery?" After approval, follow "Switch-over"
   below. Never switch without asking, because it needs vendor-side
   configuration.
4. The probe never errors. A tool that exists but refuses (for example on a
   non-cloud machine) is treated as absent.

## `svc-webhook` endpoint (what the SDK would provide)

One endpoint per (workspace, source):
`POST https://<forge-host>/hooks/<workspace>/<source>/<endpoint-id>`.

- **HMAC verification is mandatory.** Each endpoint has a signing secret held
  in Forge's secret store and referenced as `{{env.VAR}}`. It is never in
  state, the KB or chat. The endpoint verifies before anything reaches a
  model:
  | source | header | scheme |
  |---|---|---|
  | GitHub | `X-Hub-Signature-256` | `sha256=` HMAC-SHA256 of the raw body |
  | Sentry | `Sentry-Hook-Signature` | HMAC-SHA256 of the body, client secret |
  | PagerDuty v3 | `X-PagerDuty-Signature` | `v1=` HMAC-SHA256, possibly several (rotation) |
  | Datadog | custom header set in the webhook config | shared-secret HMAC or token comparison |
  | Grafana | contact point `Authorization` / HMAC header (`X-Grafana-Alerting-Signature`) | shared secret |

  Use constant-time comparison. Reject a timestamp older than 5 minutes where
  the vendor sends one (replay protection). Reject bodies over 1 MB. A bad or
  missing signature gets `401` and **no** agent run.
- Deliver verified events to a queue, not straight to an agent. A run then
  drains the queue with the same `intake.py`, reading the event on stdin, so
  dedupe, KB mapping, redaction and the allowlist stay the single code path.
- Rate-limit per endpoint and debounce flapping alerts. The same fingerprint
  inside 10 minutes is folded into one run.

## Event -> issue mapping

| source | events subscribed | maps to | dedupe key (same as polling) |
|---|---|---|---|
| GitHub | `workflow_run` with `action: completed`, `conclusion: failure` | `intake.py ci` payload, label `ci-failure` | repo + workflow + branch + head_sha |
| Sentry | `issue` (`created`, `unresolved`), `event_alert`, `metric_alert` (`critical`) | `alerts --source sentry` | issue id |
| Datadog | `@webhook-factory` in monitor messages, `$ALERT_TRANSITION` = Triggered | `alerts --source datadog` | `$AGGREGATE_KEY` or monitor id + group |
| PagerDuty | `incident.triggered`, `incident.reopened` | `alerts --source pagerduty` | `incident_key` |
| Grafana | webhook contact point, `status: firing` | `alerts --source grafana` | alert `fingerprint` |

Resolved, recovered and acknowledged events are not filed. A later version
may comment on the existing issue instead. Label, system mapping, `auto_run`
and redaction are exactly as in [triggers](triggers.md).

## Switch-over

1. Create one endpoint per source with the webhook tool. Show the user the
   vendor-side step: the GitHub org webhook (`workflow_run`), the Sentry
   internal integration, the Datadog webhook integration, the PagerDuty
   subscription or the Grafana contact point. Each one includes the URL and
   the env var name for the signing secret. The user configures the vendor
   side. Factory never holds a vendor admin credential.
2. Keep the polling routine but slow it to a **daily reconciliation**
   (`automation_update` cron `0 6 * * *`). Missed deliveries are then still
   filed, and the shared dedupe keys and board markers keep them from being
   filed twice.
3. Record `state.py set-routine <id> --automation-id <aid>` for the
   reconciliation. Endpoint ids go in phase 6 outputs
   (`webhook_<source>=<endpoint-id>`), never secrets.
4. Downgrade path: if the webhook tool disappears or deliveries stop
   (reconciliation finds items with no push-filed issue three runs in a row),
   restore the original cron and tell the user.

## Non-goals

- No auto-merge, infra change or monitor mutation from an event, ever.
- No inbound endpoint run by factory itself (no tunnel, no self-hosted
  listener). Push exists only once the platform provides it.
