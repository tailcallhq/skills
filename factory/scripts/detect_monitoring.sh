#!/usr/bin/env bash
# detect_monitoring.sh - guess which observability / alerting systems a set of
# local repository checkouts reports to.
#
# READ-ONLY. Never asks questions, never writes outside a private temp dir,
# never touches the network (no `gh`, no curl). Real `.env` files are never
# read: only committed examples (.env.example, .env.sample, ...). Only env var
# NAMES are reported, plus a small allowlist of non-secret values (org /
# project / service / site slugs) used to scope MCP servers and KB entries.
#
# Usage:
#   detect_monitoring.sh [options] [DIR...]
#
#   DIR      a local checkout (git or plain directory). Default: current dir.
#
# Options:
#   --jobs N      repos scanned concurrently (default 8; local I/O only)
#   -h, --help    show this help
#
# Output (stdout, JSON):
#   {
#     "candidates": [ {"system", "confidence", "evidence": [..], "repos": [..],
#                      "hints": {..}} ],                               # best first
#     "per_repo":   [ {"repo", "path", "candidates": [{"system", "confidence",
#                      "sources": [..], "hints": {..}}]} ],
#     "scanned":    [ {"target", "repo", "path"} ],
#     "warnings":   [ ".." ]
#   }
#
# Systems: sentry, datadog, grafana, prometheus, pagerduty, opentelemetry,
# posthog, github-actions. Signals (weights in references/monitoring.md):
#   - SDK deps in manifests (package.json, Cargo.toml, pyproject/requirements,
#     go.mod, Gemfile, pom.xml/build.gradle); lockfiles are ignored
#   - config files (sentry.properties, .sentryclirc, datadog.yaml,
#     service.datadog.yaml, grafana/, prometheus.yml, otel collector configs,
#     Prometheus Operator CRDs, Terraform providers)
#   - env var names in .env.example-style files and CI configs
#   - docker-compose images and GitHub Actions `uses:` steps
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

# Paths that are not the repo's own runtime: vendored code, fixtures, examples.
SKIP_PATHS='(^|/)(node_modules|vendor|third_party|\.forge|\.git|target|dist|build|testdata|fixtures|examples?)/'

# SDK dependency rules: SYSTEM <tab> MANIFEST-BASENAME-ERE <tab> DEPENDENCY-ERE
# The dependency ERE is matched with `grep -oE` against the manifest text; the
# matched text is the evidence, so keep it tight.
DEP_RULES=$(cat <<'EOF'
sentry	^package\.json$	"@sentry/[a-z0-9-]+"
datadog	^package\.json$	"(dd-trace|@datadog/[a-z0-9-]+)"
prometheus	^package\.json$	"prom-client"
opentelemetry	^package\.json$	"@opentelemetry/[a-z0-9-]+"
posthog	^package\.json$	"posthog-(js|node|react-native)"
grafana	^package\.json$	"@grafana/faro-[a-z0-9-]+"
pagerduty	^package\.json$	"@pagerduty/pdjs"
sentry	^Cargo\.toml$	^[[:space:]]*sentry(-[a-z0-9-]+)?[[:space:]]*[=.]
datadog	^Cargo\.toml$	^[[:space:]]*(datadog|dd-trace)[a-z0-9_-]*[[:space:]]*[=.]
prometheus	^Cargo\.toml$	^[[:space:]]*(prometheus(-client)?|metrics-exporter-prometheus|autometrics)[[:space:]]*[=.]
opentelemetry	^Cargo\.toml$	^[[:space:]]*(opentelemetry[a-z0-9_-]*|tracing-opentelemetry)[[:space:]]*[=.]
posthog	^Cargo\.toml$	^[[:space:]]*posthog(-[a-z0-9-]+)?[[:space:]]*[=.]
sentry	^(pyproject\.toml|requirements[^/]*\.(txt|in)|Pipfile|setup\.(py|cfg))$	sentry-sdk
datadog	^(pyproject\.toml|requirements[^/]*\.(txt|in)|Pipfile|setup\.(py|cfg))$	(ddtrace|datadog-api-client|datadog)
prometheus	^(pyproject\.toml|requirements[^/]*\.(txt|in)|Pipfile|setup\.(py|cfg))$	(prometheus[-_]client|prometheus-fastapi-instrumentator|django-prometheus)
opentelemetry	^(pyproject\.toml|requirements[^/]*\.(txt|in)|Pipfile|setup\.(py|cfg))$	opentelemetry-[a-z-]+
posthog	^(pyproject\.toml|requirements[^/]*\.(txt|in)|Pipfile|setup\.(py|cfg))$	posthog
pagerduty	^(pyproject\.toml|requirements[^/]*\.(txt|in)|Pipfile|setup\.(py|cfg))$	(pdpyras|pagerduty)
sentry	^go\.mod$	github\.com/getsentry/sentry-go
datadog	^go\.mod$	(gopkg\.in|github\.com)/DataDog/(dd-trace-go|datadog-go)[^[:space:]]*
prometheus	^go\.mod$	github\.com/prometheus/client_golang
opentelemetry	^go\.mod$	go\.opentelemetry\.io/otel[^[:space:]]*
posthog	^go\.mod$	github\.com/posthog/posthog-go
pagerduty	^go\.mod$	github\.com/PagerDuty/go-pagerduty
sentry	^Gemfile$	gem ['"]sentry-[a-z]+
datadog	^Gemfile$	gem ['"](ddtrace|datadog)
prometheus	^Gemfile$	gem ['"]prometheus-client
opentelemetry	^Gemfile$	gem ['"]opentelemetry-[a-z-]+
posthog	^Gemfile$	gem ['"]posthog-ruby
sentry	^(pom\.xml|build\.gradle(\.kts)?)$	io\.sentry
datadog	^(pom\.xml|build\.gradle(\.kts)?)$	com\.datadoghq
prometheus	^(pom\.xml|build\.gradle(\.kts)?)$	(micrometer-registry-prometheus|io\.prometheus)
opentelemetry	^(pom\.xml|build\.gradle(\.kts)?)$	io\.opentelemetry
posthog	^(pom\.xml|build\.gradle(\.kts)?)$	com\.posthog
EOF
)

# Config-file rules (path only): SYSTEM <tab> WEIGHT <tab> PATH-ERE <tab> LABEL
PATH_RULES=$(cat <<'EOF'
sentry	0.55	(^|/)(sentry\.properties|\.sentryclirc|sentry\.(client|server|edge)\.config\.[cm]?[jt]s)$	Sentry config file
datadog	0.6	(^|/)service\.datadog\.ya?ml$	Datadog Software Catalog entry
datadog	0.55	(^|/)(datadog[^/]*\.ya?ml|datadog-ci\.json|\.datadog-ci\.json)$	Datadog config file
grafana	0.5	(^|/)grafana(/|\.ini$)|(^|/)[^/]*\.grafana\.json$	Grafana config/dashboards
prometheus	0.5	(^|/)(prometheus|alertmanager)(\.[a-z0-9_-]+)?\.ya?ml$|(^|/)prometheus/|\.rules\.ya?ml$	Prometheus/Alertmanager config
opentelemetry	0.55	(^|/)(otel|otelcol|opentelemetry)[^/]*\.ya?ml$	OpenTelemetry collector config
pagerduty	0.5	(^|/)[^/]*pagerduty[^/]*\.(ya?ml|json|tf)$	PagerDuty config file
github-actions	0.9	^\.github/workflows/[^/]+\.ya?ml$	GitHub Actions workflow
EOF
)

# Content rules over non-manifest files: SYSTEM <tab> WEIGHT <tab> FILE-ERE <tab> CONTENT-ERE <tab> LABEL
CONTENT_RULES=$(cat <<'EOF'
prometheus	0.55	\.ya?ml$	^kind:[[:space:]]*(ServiceMonitor|PodMonitor|PrometheusRule)	Prometheus Operator resource
sentry	0.6	\.tf$	source[[:space:]]*=[[:space:]]*"jianyuan/sentry"	Terraform sentry provider
datadog	0.6	\.tf$	source[[:space:]]*=[[:space:]]*"(DataDog|datadog)/datadog"	Terraform datadog provider
grafana	0.6	\.tf$	source[[:space:]]*=[[:space:]]*"grafana/grafana"	Terraform grafana provider
pagerduty	0.6	\.tf$	source[[:space:]]*=[[:space:]]*"(PagerDuty|pagerduty)/pagerduty"	Terraform pagerduty provider
sentry	0.55	^\.github/workflows/	uses:[[:space:]]*getsentry/[A-Za-z0-9_.-]+	Sentry GitHub Action
datadog	0.5	^\.github/workflows/	uses:[[:space:]]*(DataDog|datadog)/[A-Za-z0-9_.-]+	Datadog GitHub Action
pagerduty	0.5	^\.github/workflows/	uses:[[:space:]]*(PagerDuty|pagerduty)/[A-Za-z0-9_.-]+	PagerDuty GitHub Action
grafana	0.4	(^|/)(docker-)?compose[^/]*\.ya?ml$	image:[[:space:]]*["']?(docker\.io/)?grafana/(grafana|loki|tempo|mimir|agent|alloy)[^[:space:]"']*	compose service image
prometheus	0.4	(^|/)(docker-)?compose[^/]*\.ya?ml$	image:[[:space:]]*["']?(docker\.io/|quay\.io/)?(prom|prometheus)/(prometheus|alertmanager)[^[:space:]"']*	compose service image
opentelemetry	0.4	(^|/)(docker-)?compose[^/]*\.ya?ml$	image:[[:space:]]*["']?[^[:space:]"']*(otel/opentelemetry-collector|jaegertracing/)[^[:space:]"']*	compose service image
datadog	0.4	(^|/)(docker-)?compose[^/]*\.ya?ml$	image:[[:space:]]*["']?[^[:space:]"']*datadog(hq)?/agent[^[:space:]"']*	compose service image
sentry	0.4	(^|/)(docker-)?compose[^/]*\.ya?ml$	image:[[:space:]]*["']?[^[:space:]"']*getsentry/[^[:space:]"']*	compose service image
posthog	0.4	(^|/)(docker-)?compose[^/]*\.ya?ml$	image:[[:space:]]*["']?[^[:space:]"']*posthog/posthog[^[:space:]"']*	compose service image
EOF
)

# Env var name prefixes -> system (names only; values are never read here).
ENV_PREFIX_RULES=$(cat <<'EOF'
SENTRY_	sentry
DD_	datadog
DATADOG_	datadog
GRAFANA_	grafana
PROMETHEUS_	prometheus
ALERTMANAGER_	prometheus
PAGERDUTY_	pagerduty
OTEL_	opentelemetry
POSTHOG_	posthog
EOF
)

ENV_FILES='(^|/)(\.env\.(example|sample|template|dist|defaults)|[^/]*\.env\.(example|sample|template))$'
CI_FILES='^\.github/workflows/[^/]+\.ya?ml$|(^|/)(\.gitlab-ci\.ya?ml|Jenkinsfile|azure-pipelines\.ya?ml|bitbucket-pipelines\.ya?ml|\.circleci/config\.ya?ml)$'
COMPOSE_FILES='(^|/)(docker-)?compose[^/]*\.ya?ml$'

# Non-secret values worth recording (scoping the MCP URL, KB `project:`).
# KEY <tab> SYSTEM <tab> HINT-NAME
HINT_KEYS=$(cat <<'EOF'
SENTRY_ORG	sentry	org
SENTRY_PROJECT	sentry	project
DD_SITE	datadog	site
DD_SERVICE	datadog	service
DD_ENV	datadog	env
OTEL_SERVICE_NAME	opentelemetry	service
GRAFANA_URL	grafana	url
POSTHOG_HOST	posthog	host
EOF
)
HINT_RE='(SENTRY_ORG|SENTRY_PROJECT|DD_SITE|DD_SERVICE|DD_ENV|OTEL_SERVICE_NAME|GRAFANA_URL|POSTHOG_HOST)["'\'']?[[:space:]]*[:=][[:space:]]*["'\'']?[A-Za-z0-9][A-Za-z0-9._:/-]*'

# Signals and hints are written as TSV (one fork-free printf each) and turned
# into JSON by a single jq at the end; per-file forks made big repos slow.
# emit REPO SYSTEM KIND WEIGHT DETAIL PATH
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
    (cd "$dir" && find . -type d \( -name node_modules -o -name .git -o -name target -o -name vendor \) -prune -o -type f -print 2>/dev/null |
      sed 's#^\./##') >"$list.all" || true
  fi
  repo=${repo:-${dir##*/}}
  jq -cn --arg t "$target" --arg r "$repo" --arg p "$dir" '{target:$t, repo:$r, path:$p}' >"$meta"

  # Drop vendored/example paths and every real .env file (secrets live there);
  # committed templates (.env.example, .env.sample, ...) are kept.
  grep -Ev -- "$SKIP_PATHS" "$list.all" |
    KEEP=$ENV_FILES awk '!/(^|\/)\.env(\.[A-Za-z0-9_-]+)?$/ || $0 ~ ENVIRON["KEEP"]' >"$list" || true
  rm -f "$list.all"
  [[ -s $list ]] || { echo "$repo: no files to scan" >>"$warn"; return 0; }

  (
    cd "$dir"
    local system fre dre w pre label cre f m n sample

    # ---- SDK dependencies in manifests ---------------------------------------
    while IFS=$'\t' read -r system fre dre; do
      [[ -n $system ]] || continue
      while IFS=$'\t' read -r f m; do
        emit "$repo" "$system" sdk 0.6 "SDK dependency $m in $f" "$f"
      done < <(awk -F/ -v re="$fre" '$NF ~ re' "$list" | tr '\n' '\0' | grep_files "$dre" |
        sed -E 's/\t[[:space:]]*/\t/; s/[[:space:]]*[=.]$//; s/\tgem /\t/' | tr -d "\"'" | per_file)
    done <<<"$DEP_RULES"

    # ---- config files by path ------------------------------------------------
    while IFS=$'\t' read -r system w pre label; do
      [[ -n $system ]] || continue
      n=$(grep -cE -- "$pre" "$list" || true)
      ((n > 0)) || continue
      sample=$(grep -m1 -E -- "$pre" "$list")
      if ((n > 1)); then label+=": $sample (+$((n - 1)) more)"; else label+=": $sample"; fi
      emit "$repo" "$system" config "$w" "$label" "$sample"
    done <<<"$PATH_RULES"

    # ---- content rules (CRDs, Terraform, Actions, compose images) ------------
    while IFS=$'\t' read -r system w fre cre label; do
      [[ -n $system ]] || continue
      while IFS=$'\t' read -r f m; do
        emit "$repo" "$system" "${label// /-}" "$w" "$label $m in $f" "$f"
      done < <(pick "$fre" "$list" | grep_files "$cre" |
        sed -E 's/\t(uses|image|kind|source):?[[:space:]]*=?[[:space:]]*/\t/' | tr -d "\"'" | per_file)
    done <<<"$CONTENT_RULES"

    # ---- env var names (examples + CI + compose) -----------------------------
    local src sw files
    for src in env-example ci compose; do
      case $src in
        env-example) files=$ENV_FILES; sw=0.4 ;;
        ci) files=$CI_FILES; sw=0.45 ;;
        compose) files=$COMPOSE_FILES; sw=0.35 ;;
      esac
      # one grep over every file of this kind; awk maps prefix -> system
      pick "$files" "$list" | grep_files '[A-Z][A-Z0-9_]{2,}' |
        awk -F'\t' -v rules="$ENV_PREFIX_RULES" '
          BEGIN { n = split(rules, r, "\n"); for (i = 1; i <= n; i++) { split(r[i], kv, "\t"); if (kv[1] != "") { p[++np] = kv[1]; s[np] = kv[2] } } }
          { for (i = 1; i <= np; i++) if (index($2, p[i]) == 1 && length($2) > length(p[i])) { print $1 "\t" s[i] "\t" $2; break } }' |
        awk -F'\t' '!seen[$0]++ { k = $1 "\t" $2; if (c[k]++ < 4) m[k] = (m[k] == "" ? "" : m[k] ",") $3 }
          END { for (k in m) print k "\t" m[k] }' | sort |
        while IFS=$'\t' read -r f system m; do
          emit "$repo" "$system" "env-$src" "$sw" "env var names $m in $f" "$f"
        done
    done

    # ---- allowlisted non-secret values -> hints ------------------------------
    # KEY=value / KEY: value in env examples, CI and compose; Sentry CLI config
    # (defaults.org / defaults.project); Datadog catalog `dd-service:`.
    {
      pick "$ENV_FILES|$CI_FILES|$COMPOSE_FILES" "$list" | grep_files "$HINT_RE" |
        sed -E 's/\t([A-Z_]+)["'\'']?[[:space:]]*[:=][[:space:]]*["'\'']?/\t\1\t/'
      pick '(^|/)(sentry\.properties|\.sentryclirc)$' "$list" |
        grep_files '^[[:space:]]*(defaults\.)?(org|project)[[:space:]]*=[[:space:]]*[A-Za-z0-9._-]+' |
        sed -E 's/\t[[:space:]]*(defaults\.)?(org|project)[[:space:]]*=[[:space:]]*/\tSENTRY_\2\t/; s/\tSENTRY_org\t/\tSENTRY_ORG\t/; s/\tSENTRY_project\t/\tSENTRY_PROJECT\t/'
      pick '(^|/)service\.datadog\.ya?ml$' "$list" |
        grep_files '^[[:space:]]*dd-service:[[:space:]]*["'\'']?[A-Za-z0-9._-]+' |
        sed -E 's/\t[[:space:]]*dd-service:[[:space:]]*["'\'']?/\tDD_SERVICE\t/'
    } | awk -F'\t' -v OFS='\t' -v repo="$repo" -v keys="$HINT_KEYS" '
      BEGIN { n = split(keys, r, "\n"); for (i = 1; i <= n; i++) { split(r[i], kv, "\t"); sys[kv[1]] = kv[2]; nm[kv[1]] = kv[3] } }
      ($2 in sys) {
        v = $3; sub(/\/$/, "", v)
        # only short slugs or bare http(s) origins; anything else may be a secret
        if (length(v) > 64) next
        if (v !~ /^[A-Za-z0-9][A-Za-z0-9._-]*$/ && v !~ /^https?:\/\/[A-Za-z0-9.-]+(:[0-9]+)?$/) next
        print repo, sys[$2], nm[$2], v, $1
      }' >>"$hints"
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
# Per system, keep the strongest signal of each kind per repo, then combine with
# noisy-OR: confidence = 1 - prod(1 - w). One weak signal alone stays below 0.5.
# `candidates` pools every repo (what factory asks the user to confirm);
# `per_repo` keeps sources + hints per repo for `kb.py add-monitor`.
cat "$TMP"/*.tsv 2>/dev/null | jq -Rs \
  --slurpfile scanned <(cat "$TMP"/*.meta 2>/dev/null | jq -s '.') \
  --rawfile hintsraw <(cat "$TMP"/*.hints 2>/dev/null) \
  --rawfile warn <(cat "$TMP"/*.warn 2>/dev/null) '
  def rows: split("\n") | map(select(length > 0) | split("\t"));
  def hintmap($h): $h | group_by(.hint) | map({key: .[0].hint, value: (map(.value) | unique)}) | from_entries;
  def conf: (1 - (map(1 - .weight) | reduce .[] as $x (1; . * $x))) * 100 | round / 100;
  ($hintsraw | rows | map({repo: .[0], system: .[1], hint: .[2], value: .[3], path: .[4]}) | unique) as $H
  | rows | map({repo: .[0], system: .[1], kind: .[2], weight: (.[3] | tonumber), detail: .[4], path: .[5]})
  | . as $all
  | ($scanned[0] // []) as $s
  | {
      candidates: ($all | group_by(.system) | map(
          (group_by([.repo, .kind]) | map(max_by(.weight))) as $sig
          | .[0].system as $sys
          | {
              system: $sys,
              confidence: ($sig | conf),
              evidence: ($sig | sort_by(-.weight) | map(.repo + ": " + .detail)),
              repos: ($sig | map(.repo) | unique),
              hints: hintmap($H | map(select(.system == $sys)))
            }) | sort_by(-.confidence)),
      per_repo: ($s | map(select(.repo != null) | .repo as $r | {
          repo: $r,
          path: .path,
          candidates: ($all | map(select(.repo == $r)) | group_by(.system) | map(
              . as $every
              | (group_by(.kind) | map(max_by(.weight))) as $sig
              | .[0].system as $sys
              | {
                  system: $sys,
                  confidence: ($sig | conf),
                  sources: ($every | map(.path) | map(select(length > 0)) | unique),
                  hints: hintmap($H | map(select(.repo == $r and .system == $sys)))
                }) | sort_by(-.confidence))
        })),
      scanned: $s,
      warnings: ($warn | split("\n") | map(select(length > 0)))
    }'
