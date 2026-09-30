#!/usr/bin/env bash
# detect_infra.sh - guess which infrastructure / deploy platforms a set of local
# repository checkouts runs on, so factory can offer read-only discovery.
#
# READ-ONLY and OFFLINE. Never asks questions, never writes outside a private
# temp dir, never touches the network and never runs a cloud CLI (no aws,
# gcloud, az, kubectl, helm, terraform, flyctl, vercel, wrangler, gh, curl).
# Real `.env` files, `*.tfvars` and `*.tfstate` are never read. Only env var
# NAMES are reported, plus a small allowlist of non-secret values (region,
# project, app, namespace, backend type, environment names) used to scope the
# read-only credential and the KB `runtime.environments[]`.
#
# Usage:
#   detect_infra.sh [options] [DIR...]
#
#   DIR      a local checkout (git or plain directory). Default: current dir.
#
# Options:
#   --jobs N      repos scanned concurrently (default 8; local I/O only)
#   -h, --help    show this help
#
# Output (stdout, JSON):
#   {
#     "candidates": [ {"platform", "confidence", "evidence": [..], "repos": [..],
#                      "hints": {..}} ],                               # best first
#     "per_repo":   [ {"repo", "path", "candidates": [{"platform", "confidence",
#                      "sources": [..], "hints": {..}}]} ],
#     "scanned":    [ {"target", "repo", "path"} ],
#     "warnings":   [ ".." ]
#   }
#
# Platforms (see references/infra.md): kubernetes, aws, gcp, azure, terraform,
# compose, cloudflare, vercel, fly, render, github-environments. Signals:
#   - IaC / deploy config files (*.tf, k8s/, helm/, Chart.yaml, kustomization,
#     docker-compose*.yml, serverless.yml, fly.toml, vercel.json, render.yaml,
#     wrangler.toml, cloudbuild.yaml, cdk.json, *.bicep)
#   - Kubernetes resource kinds, Terraform providers and backends
#   - .github/workflows deploy steps (aws-actions/, google-github-actions/,
#     azure/, kubectl, helm, terraform, flyctl, vercel, wrangler) and
#     `environment:` keys
#   - provider SDKs in manifests (lockfiles ignored)
#   - env var names in .env.example-style files, CI configs and compose files
#
# Exit 0 on any partial failure (reported in `warnings`), 2 on bad arguments.

set -euo pipefail

JOBS=8
TARGETS=()

usage() { sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; }

while (($#)); do
  case "$1" in
    --jobs) JOBS=${2:?}; shift 2 ;;
    -h | --help) usage; exit 0 ;;
    --) shift; TARGETS+=("$@"); break ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *) TARGETS+=("$1"); shift ;;
  esac
done
((${#TARGETS[@]})) || TARGETS=(.)
[[ $JOBS =~ ^[0-9]+$ ]] || { echo "numeric option expected, got: $JOBS" >&2; exit 2; }
((JOBS >= 1)) || JOBS=1

command -v jq >/dev/null || { echo '{"candidates":[],"per_repo":[],"scanned":[],"warnings":["jq not found; install jq"]}'; exit 0; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# Paths that are not the repo's own runtime: vendored code, fixtures, examples,
# Terraform module caches and vendored Helm sub-charts.
SKIP_PATHS='(^|/)(node_modules|vendor|third_party|\.forge|\.git|\.terraform|target|dist|build|testdata|fixtures|examples?)/|(^|/)charts/[^/]+/charts/'

# Files that may hold secrets or live state: never read, never listed.
NEVER_READ='(^|/)[^/]*\.(tfvars|tfvars\.json|tfstate|tfstate\.backup)$|(^|/)\.terraform\.tfstate$|(^|/)kubeconfig[^/]*$|(^|/)\.kube/'

# Provider SDK rules: PLATFORM <tab> MANIFEST-BASENAME-ERE <tab> DEPENDENCY-ERE
PY='^(pyproject\.toml|requirements[^/]*\.(txt|in)|Pipfile|setup\.(py|cfg))$'
DEP_RULES=$(cat <<EOF
aws	^package\.json$	"(@aws-sdk/[a-z0-9-]+|aws-sdk|aws-cdk-lib|@aws-cdk/[a-z0-9-]+)"
gcp	^package\.json$	"(@google-cloud/[a-z0-9-]+|firebase-admin)"
azure	^package\.json$	"@azure/[a-z0-9-]+"
cloudflare	^package\.json$	"(wrangler|@cloudflare/[a-z0-9-]+)"
vercel	^package\.json$	"(vercel|@vercel/[a-z0-9-]+)"
kubernetes	^package\.json$	"@kubernetes/client-node"
aws	^Cargo\.toml$	^[[:space:]]*(aws-sdk-[a-z0-9]+|aws-config|aws_lambda_events|lambda_runtime)[[:space:]]*[=.]
gcp	^Cargo\.toml$	^[[:space:]]*(google-cloud-[a-z0-9-]+|gcp[_-]auth)[[:space:]]*[=.]
azure	^Cargo\.toml$	^[[:space:]]*azure_[a-z0-9_]+[[:space:]]*[=.]
kubernetes	^Cargo\.toml$	^[[:space:]]*(kube|k8s-openapi)[[:space:]]*[=.]
cloudflare	^Cargo\.toml$	^[[:space:]]*worker[[:space:]]*[=.]
aws	$PY	(boto3|aws-cdk-lib|aws-lambda-powertools)
gcp	$PY	google-cloud-[a-z-]+
azure	$PY	azure-(identity|mgmt-[a-z-]+|storage-[a-z-]+|servicebus|keyvault-[a-z-]+)
kubernetes	$PY	^kubernetes
aws	^go\.mod$	github\.com/aws/(aws-sdk-go(-v2)?|aws-lambda-go|aws-cdk-go)[^[:space:]]*
gcp	^go\.mod$	cloud\.google\.com/go[^[:space:]]*
azure	^go\.mod$	github\.com/Azure/azure-sdk-for-go[^[:space:]]*
kubernetes	^go\.mod$	k8s\.io/client-go
cloudflare	^go\.mod$	github\.com/cloudflare/cloudflare-go
aws	^Gemfile$	gem ['"]aws-sdk[a-z-]*
gcp	^Gemfile$	gem ['"]google-cloud-[a-z-]+
aws	^(pom\.xml|build\.gradle(\.kts)?)$	(software\.amazon\.awssdk|com\.amazonaws)
gcp	^(pom\.xml|build\.gradle(\.kts)?)$	com\.google\.cloud
azure	^(pom\.xml|build\.gradle(\.kts)?)$	com\.azure
EOF
)

# Config-file rules (path only): PLATFORM <tab> WEIGHT <tab> PATH-ERE <tab> LABEL
PATH_RULES=$(cat <<'EOF'
terraform	0.7	\.tf$	Terraform configuration
terraform	0.4	(^|/)(\.terraform\.lock\.hcl|terragrunt\.hcl)$	Terraform lock/terragrunt file
kubernetes	0.5	(^|/)(k8s|kubernetes)/[^/]+	Kubernetes manifests dir
kubernetes	0.65	(^|/)Chart\.yaml$	Helm chart
kubernetes	0.4	(^|/)(helm|charts)/[^/]+	Helm dir
kubernetes	0.6	(^|/)(kustomization\.ya?ml|skaffold\.ya?ml|Tiltfile|helmfile\.ya?ml)$	Kustomize/Skaffold/Helmfile config
compose	0.75	(^|/)(docker-)?compose[^/]*\.ya?ml$	Compose file
aws	0.6	(^|/)serverless\.ya?ml$	Serverless Framework config
aws	0.6	(^|/)(cdk\.json|samconfig\.toml|buildspec\.ya?ml|appspec\.ya?ml|copilot/[^/]+/manifest\.yml|task-definition[^/]*\.json)$	AWS CDK/SAM/CodeBuild/ECS config
gcp	0.6	(^|/)(cloudbuild\.ya?ml|app\.yaml|\.gcloudignore)$	Google Cloud Build/App Engine config
azure	0.6	\.bicep$|(^|/)azure\.yaml$	Azure Bicep/azd config
fly	0.85	(^|/)fly(\.[a-z0-9_-]+)?\.toml$	Fly.io app config
vercel	0.85	(^|/)vercel\.json$	Vercel project config
render	0.85	(^|/)render\.ya?ml$	Render blueprint
cloudflare	0.8	(^|/)wrangler\.(toml|jsonc?)$	Cloudflare Workers (wrangler) config
EOF
)

# Content rules: PLATFORM <tab> WEIGHT <tab> FILE-ERE <tab> CONTENT-ERE <tab> LABEL
WF='^\.github/workflows/[^/]+\.ya?ml$'
CONTENT_RULES=$(cat <<EOF
kubernetes	0.6	\.ya?ml$	^kind:[[:space:]]*(Deployment|StatefulSet|DaemonSet|Service|Ingress|NetworkPolicy|CronJob|HPA|HorizontalPodAutoscaler|Gateway|HTTPRoute)[[:space:]]*$	Kubernetes resource
aws	0.6	\.tf$	source[[:space:]]*=[[:space:]]*"hashicorp/aws"	Terraform aws provider
gcp	0.6	\.tf$	source[[:space:]]*=[[:space:]]*"hashicorp/google(-beta)?"	Terraform google provider
azure	0.6	\.tf$	source[[:space:]]*=[[:space:]]*"hashicorp/azurerm"	Terraform azurerm provider
kubernetes	0.55	\.tf$	source[[:space:]]*=[[:space:]]*"hashicorp/(kubernetes|helm)"	Terraform kubernetes provider
cloudflare	0.6	\.tf$	source[[:space:]]*=[[:space:]]*"cloudflare/cloudflare"	Terraform cloudflare provider
vercel	0.6	\.tf$	source[[:space:]]*=[[:space:]]*"vercel/vercel"	Terraform vercel provider
github-environments	0.4	\.tf$	resource[[:space:]]+"github_repository_environment"	Terraform github environment
aws	0.5	\.tf$	resource[[:space:]]+"aws_[a-z0-9_]+"	Terraform aws resource
gcp	0.5	\.tf$	resource[[:space:]]+"google_[a-z0-9_]+"	Terraform google resource
terraform	0.6	\.tf$	(backend[[:space:]]+"[a-z0-9]+"|^[[:space:]]*cloud[[:space:]]*\{)	Terraform state backend
aws	0.6	$WF	uses:[[:space:]]*aws-actions/[A-Za-z0-9_.-]+	AWS GitHub Action
gcp	0.6	$WF	uses:[[:space:]]*google-github-actions/[A-Za-z0-9_.-]+	Google GitHub Action
azure	0.6	$WF	uses:[[:space:]]*[Aa]zure/[A-Za-z0-9_.-]+	Azure GitHub Action
kubernetes	0.55	$WF	(uses:[[:space:]]*([Aa]zure/(k8s-[a-z-]+|setup-kubectl|setup-helm|aks-set-context)|google-github-actions/get-gke-credentials)|(^|[[:space:]])(kubectl[[:space:]]+(apply|rollout|set|create|replace|patch|diff)|helm[[:space:]]+(upgrade|install)|kustomize[[:space:]]+build))	Kubernetes deploy step
terraform	0.5	$WF	(uses:[[:space:]]*hashicorp/(setup-terraform|tfc-workflows-github)[A-Za-z0-9_./-]*|terraform[[:space:]]+(plan|apply))	Terraform CI step
fly	0.6	$WF	(uses:[[:space:]]*superfly/[A-Za-z0-9_.-]+|flyctl[[:space:]]+deploy)	Fly deploy step
vercel	0.6	$WF	(uses:[[:space:]]*amondnet/vercel-action|vercel[[:space:]]+(deploy|--prod|build))	Vercel deploy step
cloudflare	0.6	$WF	(uses:[[:space:]]*cloudflare/[A-Za-z0-9_.-]+|wrangler[[:space:]]+(deploy|publish))	Cloudflare deploy step
render	0.5	$WF	(api\.render\.com/deploy|uses:[[:space:]]*[A-Za-z0-9_-]+/render-deploy[A-Za-z0-9_.-]*)	Render deploy hook
github-environments	0.65	$WF	^[[:space:]]+environment:	GitHub deployment environment
EOF
)

# Env var name prefixes -> platform (names only; values are never read here).
ENV_PREFIX_RULES=$(cat <<'EOF'
AWS_	aws
CDK_	aws
GOOGLE_CLOUD_	gcp
GOOGLE_APPLICATION_	gcp
GCP_	gcp
GCLOUD_	gcp
CLOUDSDK_	gcp
AZURE_	azure
ARM_	azure
CLOUDFLARE_	cloudflare
CF_API_	cloudflare
CF_ACCOUNT_	cloudflare
VERCEL_	vercel
FLY_	fly
RENDER_	render
KUBECONFIG	kubernetes
KUBE_	kubernetes
HELM_	kubernetes
TF_VAR_	terraform
TF_TOKEN_	terraform
TFE_	terraform
TF_CLOUD_	terraform
EOF
)

ENV_FILES='(^|/)(\.env\.(example|sample|template|dist|defaults)|[^/]*\.env\.(example|sample|template))$'
CI_FILES='^\.github/workflows/[^/]+\.ya?ml$|(^|/)(\.gitlab-ci\.ya?ml|Jenkinsfile|azure-pipelines\.ya?ml|bitbucket-pipelines\.ya?ml|\.circleci/config\.ya?ml)$'
COMPOSE_FILES='(^|/)(docker-)?compose[^/]*\.ya?ml$'
K8S_FILES='(^|/)(k8s|kubernetes|manifests|deploy|overlays|base)/.*\.ya?ml$|(^|/)kustomization\.ya?ml$'

# Non-secret values worth recording. PLATFORM <tab> HINT <tab> FILE-ERE <tab> KEY-ERE
# The value must follow KEY-ERE (after optional quote) at the start of a line
# and is kept only if it is a short slug (see the awk filter below).
HINT_RULES=$(cat <<EOF
aws	region	$ENV_FILES|$CI_FILES|$COMPOSE_FILES	(AWS_REGION|AWS_DEFAULT_REGION|aws-region)["']?[[:space:]]*[:=][[:space:]]*
aws	account	$ENV_FILES|$CI_FILES	AWS_ACCOUNT_ID["']?[[:space:]]*[:=][[:space:]]*
aws	region	(^|/)serverless\.ya?ml$	region:[[:space:]]*
gcp	project	$ENV_FILES|$CI_FILES|$COMPOSE_FILES	(GOOGLE_CLOUD_PROJECT|GCP_PROJECT(_ID)?|CLOUDSDK_CORE_PROJECT|project_id)["']?[[:space:]]*[:=][[:space:]]*
gcp	region	$ENV_FILES|$CI_FILES	(GCP_REGION|GOOGLE_CLOUD_REGION|CLOUDSDK_COMPUTE_REGION)["']?[[:space:]]*[:=][[:space:]]*
fly	app	(^|/)fly(\.[a-z0-9_-]+)?\.toml$	app[[:space:]]*=[[:space:]]*
fly	region	(^|/)fly(\.[a-z0-9_-]+)?\.toml$	primary_region[[:space:]]*=[[:space:]]*
cloudflare	worker	(^|/)wrangler\.toml$	name[[:space:]]*=[[:space:]]*
render	service	(^|/)render\.ya?ml$	-?[[:space:]]*name:[[:space:]]*
kubernetes	namespace	$K8S_FILES	namespace:[[:space:]]*
kubernetes	chart	(^|/)Chart\.yaml$	name:[[:space:]]*
terraform	backend	\.tf$	backend[[:space:]]+"
terraform	organization	\.tf$	organization[[:space:]]*=[[:space:]]*
github-environments	environment	$WF	environment:[[:space:]]*
EOF
)
# Whole token (up to whitespace/comment) so a URL is rejected as a whole
# instead of yielding its scheme; the awk filter keeps slugs only.
HINT_VALUE='["'\'']?[^[:space:]#,}]+'

# Signals and hints are written as TSV (one fork-free printf each) and turned
# into JSON by a single jq at the end; per-file forks made big repos slow.
# emit REPO PLATFORM KIND WEIGHT DETAIL PATH
emit() { printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "${5//$'\t'/ }" "$6"; }

# files matching a path ERE from the repo file list, NUL-separated
pick() { grep -E -- "$1" "$2" | tr '\n' '\0' || true; }

# grep_files ERE < NUL-list -> "path<TAB>match" for every match (one grep per
# batch of files, not per file). Missing/unreadable files are ignored.
grep_files() { xargs -0 -r grep -oHIE -- "$1" 2>/dev/null | sed -E 's/:/\t/' || true; }

# group "path<TAB>match" lines per file -> "path<TAB>m1,m2,.." (unique, max 4)
per_file() {
  awk -F'\t' '{ if (!seen[$1 SUBSEP $2]++ && n[$1]++ < 4) m[$1] = (m[$1] == "" ? "" : m[$1] ",") $2; if (!(($1) in o)) o[$1] = ++k }
    END { for (f in m) print o[f] "\t" f "\t" m[f] }' | sort -n | cut -f2-
}

scan_repo() { # INDEX TARGET
  local target=$2 out="$TMP/$1.tsv" hints="$TMP/$1.hints" warn="$TMP/$1.warn" meta="$TMP/$1.meta"
  local list="$TMP/$1.files" dir repo=""
  : >"$out"; : >"$hints"; : >"$warn"

  if [[ ! -d $target ]]; then
    echo "$target: not a directory, skipped (clone it first)" >>"$warn"
    jq -cn --arg t "$target" '{target:$t, repo:null, path:null}' >"$meta"
    return 0
  fi
  dir=$(cd "$target" && pwd -P)
  if git -C "$dir" rev-parse --git-dir >/dev/null 2>&1; then
    dir=$(git -C "$dir" rev-parse --show-toplevel)
    local url
    url=$(git -C "$dir" remote get-url origin 2>/dev/null || true)
    if [[ $url =~ [:/]([^/:]+)/([^/]+)$ ]]; then
      repo="${BASH_REMATCH[1]}/${BASH_REMATCH[2]%.git}"
    fi
    # Tracked + untracked-but-not-ignored: honours .gitignore, skips worktrees.
    git -C "$dir" ls-files --cached --others --exclude-standard 2>/dev/null >"$list.all" ||
      echo "${repo:-$dir}: git ls-files failed" >>"$warn"
  else
    (cd "$dir" && find . -type d \( -name node_modules -o -name .git -o -name .terraform -o -name target -o -name vendor \) -prune -o -type f -print 2>/dev/null |
      sed 's#^\./##') >"$list.all" || true
  fi
  repo=${repo:-${dir##*/}}
  jq -cn --arg t "$target" --arg r "$repo" --arg p "$dir" '{target:$t, repo:$r, path:$p}' >"$meta"

  # Drop vendored/example paths, state/tfvars/kubeconfig files and every real
  # .env file (secrets live there); committed templates (.env.example) are kept.
  grep -Ev -- "$SKIP_PATHS" "$list.all" | grep -Ev -- "$NEVER_READ" |
    KEEP=$ENV_FILES awk '!/(^|\/)\.env(\.[A-Za-z0-9_-]+)?$/ || $0 ~ ENVIRON["KEEP"]' >"$list" || true
  rm -f "$list.all"
  [[ -s $list ]] || { echo "$repo: no files to scan" >>"$warn"; return 0; }

  (
    cd "$dir"
    local platform fre dre w pre label cre f m n sample hint kre

    # ---- provider SDK dependencies in manifests ------------------------------
    while IFS=$'\t' read -r platform fre dre; do
      [[ -n $platform ]] || continue
      while IFS=$'\t' read -r f m; do
        emit "$repo" "$platform" sdk 0.5 "SDK dependency $m in $f" "$f"
      done < <(awk -F/ -v re="$fre" '$NF ~ re' "$list" | tr '\n' '\0' | grep_files "$dre" |
        sed -E 's/\t[[:space:]]*/\t/; s/[[:space:]]*[=.]$//; s/\tgem /\t/' | tr -d "\"'" | per_file)
    done <<<"$DEP_RULES"

    # ---- config files by path ------------------------------------------------
    while IFS=$'\t' read -r platform w pre label; do
      [[ -n $platform ]] || continue
      n=$(grep -cE -- "$pre" "$list" || true)
      ((n > 0)) || continue
      sample=$(grep -m1 -E -- "$pre" "$list")
      if ((n > 1)); then label+=": $sample (+$((n - 1)) more)"; else label+=": $sample"; fi
      emit "$repo" "$platform" config "$w" "$label" "$sample"
    done <<<"$PATH_RULES"

    # ---- content rules (k8s kinds, Terraform, workflow deploy steps) ---------
    while IFS=$'\t' read -r platform w fre cre label; do
      [[ -n $platform ]] || continue
      while IFS=$'\t' read -r f m; do
        emit "$repo" "$platform" "${label// /-}" "$w" "$label $m in $f" "$f"
      done < <(pick "$fre" "$list" | grep_files "$cre" |
        sed -E 's/\t[[:space:]]*((uses|kind|source)[[:space:]]*[:=][[:space:]]*)?/\t/; s/[[:space:]]*\{?$//' | tr -d "\"'" | per_file)
    done <<<"$CONTENT_RULES"

    # ---- env var names (examples + CI + compose) -----------------------------
    local src sw files
    for src in env-example ci compose; do
      case $src in
        env-example) files=$ENV_FILES; sw=0.4 ;;
        ci) files=$CI_FILES; sw=0.4 ;;
        compose) files=$COMPOSE_FILES; sw=0.3 ;;
      esac
      # one grep over every file of this kind; awk maps prefix -> platform
      pick "$files" "$list" | grep_files '[A-Z][A-Z0-9_]{2,}' |
        awk -F'\t' -v rules="$ENV_PREFIX_RULES" '
          BEGIN { n = split(rules, r, "\n"); for (i = 1; i <= n; i++) { split(r[i], kv, "\t"); if (kv[1] != "") { p[++np] = kv[1]; s[np] = kv[2] } } }
          { for (i = 1; i <= np; i++) if (index($2, p[i]) == 1 && (length($2) > length(p[i]) || p[i] !~ /_$/)) { print $1 "\t" s[i] "\t" $2; break } }' |
        awk -F'\t' '!seen[$0]++ { k = $1 "\t" $2; if (c[k]++ < 4) m[k] = (m[k] == "" ? "" : m[k] ",") $3 }
          END { for (k in m) print k "\t" m[k] }' | sort |
        while IFS=$'\t' read -r f platform m; do
          emit "$repo" "$platform" "env-$src" "$sw" "env var names $m in $f" "$f"
        done
    done

    # ---- allowlisted non-secret values -> hints ------------------------------
    while IFS=$'\t' read -r platform hint fre kre; do
      [[ -n $platform ]] || continue
      pick "$fre" "$list" | grep_files "^[[:space:]]*$kre$HINT_VALUE" |
        sed -E "s/\t[[:space:]]*${kre}[\"']?/\t/" |
        awk -F'\t' -v OFS='\t' -v repo="$repo" -v p="$platform" -v h="$hint" '
          { v = $2; sub(/["'\'']$/, "", v)
            # only short slugs; anything else may be a secret or an expression
            if (length(v) > 64 || v !~ /^[A-Za-z0-9][A-Za-z0-9._-]*$/) next
            print repo, p, h, v, $1 }'
    done <<<"$HINT_RULES" >>"$hints"
  ) >>"$out" 2>>"$warn" || echo "$repo: scan partially failed" >>"$warn"
}

# ---- fan out over targets, at most $JOBS at once --------------------------------
i=0
for t in "${TARGETS[@]}"; do
  while (($(jobs -rp | wc -l) >= JOBS)); do wait -n 2>/dev/null || sleep 0.05; done
  scan_repo "$i" "$t" &
  i=$((i + 1))
done
wait

# ---- score -----------------------------------------------------------------------
# Per platform, keep the strongest signal of each kind per repo, then combine
# with noisy-OR: confidence = 1 - prod(1 - w). One weak signal stays below 0.5.
# `candidates` pools every repo (what factory asks the user to confirm);
# `per_repo` keeps sources + hints per repo for the KB `runtime` section.
cat "$TMP"/*.tsv 2>/dev/null | jq -Rs \
  --slurpfile scanned <(cat "$TMP"/*.meta 2>/dev/null | jq -s '.') \
  --rawfile hintsraw <(cat "$TMP"/*.hints 2>/dev/null) \
  --rawfile warn <(cat "$TMP"/*.warn 2>/dev/null) '
  def rows: split("\n") | map(select(length > 0) | split("\t"));
  def hintmap($h): $h | group_by(.hint) | map({key: .[0].hint, value: (map(.value) | unique)}) | from_entries;
  def conf: (1 - (map(1 - .weight) | reduce .[] as $x (1; . * $x))) * 100 | round / 100;
  ($hintsraw | rows | map({repo: .[0], platform: .[1], hint: .[2], value: .[3], path: .[4]}) | unique) as $H
  | rows | map({repo: .[0], platform: .[1], kind: .[2], weight: (.[3] | tonumber), detail: .[4], path: .[5]})
  | . as $all
  | ($scanned[0] // []) as $s
  | {
      candidates: ($all | group_by(.platform) | map(
          (group_by([.repo, .kind]) | map(max_by(.weight))) as $sig
          | .[0].platform as $p
          | {
              platform: $p,
              confidence: ($sig | conf),
              evidence: ($sig | sort_by(-.weight) | map(.repo + ": " + .detail)),
              repos: ($sig | map(.repo) | unique),
              hints: hintmap($H | map(select(.platform == $p)))
            }) | sort_by(-.confidence)),
      per_repo: ($s | map(select(.repo != null) | .repo as $r | {
          repo: $r,
          path: .path,
          candidates: ($all | map(select(.repo == $r)) | group_by(.platform) | map(
              . as $every
              | (group_by(.kind) | map(max_by(.weight))) as $sig
              | .[0].platform as $p
              | {
                  platform: $p,
                  confidence: ($sig | conf),
                  sources: ($every | map(.path) | map(select(length > 0)) | unique),
                  hints: hintmap($H | map(select(.repo == $r and .platform == $p)))
                }) | sort_by(-.confidence))
        })),
      scanned: $s,
      warnings: ($warn | split("\n") | map(select(length > 0)))
    }'
