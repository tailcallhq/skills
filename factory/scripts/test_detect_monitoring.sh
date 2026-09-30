#!/usr/bin/env bash
# test_detect_monitoring.sh - regression test for detect_monitoring.sh.
# Builds a throwaway fixture repo with one signal of every kind plus planted
# secrets, runs the detector, and asserts on the JSON. Needs bash, git, jq.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
F=$(mktemp -d)
trap 'rm -rf "$F"' EXIT
cd "$F"
git init -q
git remote add origin git@github.com:acme/shop.git
mkdir -p .github/workflows grafana/dashboards k8s infra web svc node_modules/x examples

cat >web/package.json <<'EOF'
{"dependencies":{"@sentry/nextjs":"^8","dd-trace":"^5","prom-client":"^15","@opentelemetry/api":"1","posthog-js":"1","@grafana/faro-web-sdk":"1"}}
EOF
cat >svc/Cargo.toml <<'EOF'
[dependencies]
sentry = { version = "0.34" }
tracing-opentelemetry = "0.2"
metrics-exporter-prometheus = "0.1"
EOF
printf 'module x\nrequire github.com/PagerDuty/go-pagerduty v1\n' >go.mod
printf 'defaults.org=acme\ndefaults.project=shop-web\nauth.token=sntrys_PLANTED_SECRET\n' >sentry.properties
printf 'dd-service: shop-api\n' >service.datadog.yaml
echo '{}' >grafana/dashboards/api.json
printf 'kind: ServiceMonitor\n' >k8s/sm.yaml
printf 'receivers:\n' >otel-collector.yaml
printf 'terraform { required_providers { pd = { source = "PagerDuty/pagerduty" } } }\n' >infra/main.tf
printf 'DD_API_KEY=abc123PLANTED\nDD_SITE=datadoghq.eu\nOTEL_SERVICE_NAME=shop-api\nGRAFANA_URL=https://acme.grafana.net\nSENTRY_ORG=https://user:PLANTED@evil\n' >.env.example
printf 'SENTRY_AUTH_TOKEN=PLANTED_REAL_ENV\nNEWRELIC_ONLY=1\n' >.env
printf 'DD_API_KEY=PLANTED_LOCAL\n' >.env.local
printf 'jobs:\n  a:\n    steps:\n      - uses: getsentry/action-release@v1\n' >.github/workflows/release.yml
printf 'services:\n  p:\n    image: prom/prometheus:v2\n' >docker-compose.yml
echo '{"dependencies":{"@sentry/node":"1"}}' >node_modules/x/package.json
echo '{"dependencies":{"dd-trace":"1"}}' >examples/package.json

out=$("$HERE/detect_monitoring.sh" "$F" /nonexistent-dir)
fail=0
check() { # DESCRIPTION JQ-EXPR
  if jq -e "$2" >/dev/null <<<"$out"; then echo "ok   $1"; else echo "FAIL $1"; fail=1; fi
}

for s in sentry datadog grafana prometheus pagerduty opentelemetry posthog github-actions; do
  check "detects $s" "any(.candidates[]; .system == \"$s\" and .confidence >= 0.6)"
done
check "repo slug from origin" '.scanned[0].repo == "acme/shop"'
check "sentry hints from sentry.properties" '(.candidates[] | select(.system=="sentry") | .hints) == {org:["acme"], project:["shop-web"]}'
check "datadog hints (catalog + env template)" '(.candidates[] | select(.system=="datadog") | .hints) == {service:["shop-api"], site:["datadoghq.eu"]}'
check "grafana url hint" '(.candidates[] | select(.system=="grafana") | .hints.url) == ["https://acme.grafana.net"]'
check "env var names come from .env.example" 'any(.candidates[].evidence[]; test("DD_API_KEY,DD_SITE in .env.example"))'
check "real .env files never scanned" '[.. | strings | select(test("(^|/)\\.env(\\.local)?$"))] | length == 0'
check "vendored/example paths skipped" '[.per_repo[].candidates[].sources[] | select(test("node_modules|examples/"))] | length == 0'
check "per_repo sources list every file" '(.per_repo[0].candidates[] | select(.system=="sentry") | .sources) | index("sentry.properties") != null and index("web/package.json") != null'
check "missing dir is a warning, not a failure" 'any(.warnings[]; test("nonexistent-dir: not a directory"))'
if grep -qE 'PLANTED|abc123|sntrys_' <<<"$out"; then echo "FAIL secret value leaked"; fail=1; else echo "ok   no secret values in output"; fi

exit "$fail"
