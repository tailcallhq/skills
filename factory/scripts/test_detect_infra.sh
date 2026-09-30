#!/usr/bin/env bash
# test_detect_infra.sh - regression test for detect_infra.sh.
# Builds a throwaway fixture repo with one signal per platform plus planted
# secrets / state files, runs the detector (with every cloud CLI shadowed by a
# tripwire so any network/CLI call fails the test), and asserts on the JSON.
# Needs bash, git, jq.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
F=$(mktemp -d)
BIN=$(mktemp -d)
trap 'rm -rf "$F" "$BIN"' EXIT

# Tripwires: detect_infra.sh must never invoke a cloud CLI or the network.
for c in aws gcloud az kubectl helm terraform flyctl fly vercel wrangler gh curl wget; do
  printf '#!/bin/sh\necho "%s called" >>"%s/tripwire"\nexit 1\n' "$c" "$BIN" >"$BIN/$c"
  chmod +x "$BIN/$c"
done

cd "$F"
git init -q
git remote add origin git@github.com:acme/platform.git
mkdir -p .github/workflows k8s helm/api infra web svc node_modules/x examples .terraform/modules/m

cat >web/package.json <<'EOF'
{"dependencies":{"@aws-sdk/client-s3":"3","@google-cloud/storage":"7","@azure/identity":"4","@vercel/analytics":"1"}}
EOF
printf '[dependencies]\naws-config = "1"\nkube = { version = "0.9" }\n' >svc/Cargo.toml
cat >infra/main.tf <<'EOF'
terraform {
  backend "s3" {}
  required_providers {
    aws = { source = "hashicorp/aws" }
    google = { source = "hashicorp/google" }
    cf = { source = "cloudflare/cloudflare" }
  }
}
resource "aws_sqs_queue" "jobs" {}
EOF
printf 'aws_secret_key = "PLANTED_TFVARS"\n' >infra/prod.tfvars
printf '{"resources":[{"secret":"PLANTED_STATE"}]}\n' >infra/terraform.tfstate
printf 'provider "hashicorp/azurerm" PLANTED_MODULE_CACHE\n' >.terraform/modules/m/main.tf
cat >k8s/deploy.yaml <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  namespace: shop
EOF
printf 'apiVersion: v2\nname: api-chart\n' >helm/api/Chart.yaml
printf 'services:\n  db:\n    image: postgres:16\n' >docker-compose.yml
printf 'service: api\nprovider:\n  name: aws\n  region: eu-west-1\n' >serverless.yml
printf 'app = "acme-web"\nprimary_region = "ams"\n' >fly.toml
echo '{"framework":"nextjs"}' >vercel.json
printf 'services:\n  - type: web\n    name: acme-render\n' >render.yaml
printf 'name = "edge-router"\n' >wrangler.toml
cat >.github/workflows/deploy.yml <<'EOF'
jobs:
  deploy:
    environment: production
    steps:
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          aws-region: us-east-1
      - uses: google-github-actions/auth@v2
      - run: kubectl apply -f k8s/
      - run: helm upgrade --install api helm/api
      - uses: hashicorp/setup-terraform@v3
EOF
printf 'AWS_ACCESS_KEY_ID=AKIAPLANTEDPLANTED12\nAWS_REGION=eu-central-1\nGOOGLE_CLOUD_PROJECT=acme-prod\nCLOUDFLARE_API_TOKEN=PLANTED_CF\nKUBECONFIG=/tmp/kc\nGCP_PROJECT=https://user:PLANTED@evil\n' >.env.example
printf 'AWS_SECRET_ACCESS_KEY=PLANTED_REAL_ENV\n' >.env
printf 'VERCEL_TOKEN=PLANTED_LOCAL\n' >.env.local
echo '{"dependencies":{"aws-sdk":"2"}}' >node_modules/x/package.json
printf 'app = "example-app"\n' >examples/fly.toml

out=$(PATH="$BIN:$PATH" "$HERE/detect_infra.sh" --jobs 4 "$F" /nonexistent-dir)
fail=0
check() { # DESCRIPTION JQ-EXPR
  if jq -e "$2" >/dev/null <<<"$out"; then echo "ok   $1"; else echo "FAIL $1"; fail=1; fi
}

for p in kubernetes aws gcp terraform compose cloudflare vercel fly render github-environments; do
  check "detects $p" "any(.candidates[]; .platform == \"$p\" and .confidence >= 0.6)"
done
check "azure is weak (SDK only, module cache ignored)" 'all(.candidates[]; .platform != "azure" or .confidence < 0.6)'
check "repo slug from origin" '.scanned[0].repo == "acme/platform"'
check "aws region hints (serverless, env, workflow)" '(.candidates[] | select(.platform=="aws") | .hints.region) == ["eu-central-1","eu-west-1","us-east-1"]'
check "gcp project hint, URL-shaped value dropped" '(.candidates[] | select(.platform=="gcp") | .hints.project) == ["acme-prod"]'
check "fly app + region hints" '(.candidates[] | select(.platform=="fly") | .hints) == {app:["acme-web"], region:["ams"]}'
check "k8s namespace + chart hints" '(.candidates[] | select(.platform=="kubernetes") | .hints) == {chart:["api-chart"], namespace:["shop"]}'
check "terraform backend hint" '(.candidates[] | select(.platform=="terraform") | .hints.backend) == ["s3"]'
check "github environment hint" '(.candidates[] | select(.platform=="github-environments") | .hints.environment) == ["production"]'
check "cloudflare worker hint" '(.candidates[] | select(.platform=="cloudflare") | .hints.worker) == ["edge-router"]'
check "kubectl deploy step is evidence" 'any(.candidates[].evidence[]; test("Kubernetes deploy step .*kubectl apply"))'
check "env var names come from .env.example" 'any(.candidates[].evidence[]; test("AWS_ACCESS_KEY_ID,AWS_REGION in .env.example"))'
check "real .env / tfvars / tfstate never scanned" '[.. | strings | select(test("(^|/)\\.env(\\.local)?$|tfvars|tfstate"))] | length == 0'
check "vendored/example/.terraform paths skipped" '[.per_repo[].candidates[].sources[] | select(test("node_modules|examples/|\\.terraform/"))] | length == 0'
check "missing dir is a warning, not a failure" 'any(.warnings[]; test("nonexistent-dir: not a directory"))'
check "no warnings for the fixture repo" '[.warnings[] | select(test("acme/platform"))] | length == 0'
if grep -qE 'PLANTED|AKIA' <<<"$out"; then echo "FAIL secret value leaked"; fail=1; else echo "ok   no secret values in output"; fi
if [[ -e $BIN/tripwire ]]; then echo "FAIL external CLI invoked: $(cat "$BIN/tripwire")"; fail=1; else echo "ok   no cloud CLI / network call"; fi

# --jobs 1 and --jobs 4 give the same result
out1=$("$HERE/detect_infra.sh" --jobs 1 "$F")
out4=$("$HERE/detect_infra.sh" --jobs 4 "$F")
if [[ $out1 == "$out4" ]]; then echo "ok   deterministic across --jobs"; else echo "FAIL --jobs changes output"; fail=1; fi

exit "$fail"
