#!/usr/bin/env bash
# detect_tracker.sh - guess which task tracker(s) a set of repositories uses.
#
# READ-ONLY. Never asks questions, never writes outside a private temp dir,
# never calls a mutating API (the only non-GET request is a read-only GraphQL
# query). The calling skill shows the top candidate to the user to confirm.
#
# Usage:
#   detect_tracker.sh [options] [TARGET...]
#
#   TARGET   a local git checkout (directory) or an `owner/name` GitHub slug.
#            Default: the current directory.
#
# Options:
#   --commits N   recent commit messages to scan per repo      (default 200)
#   --prs N       recent pull requests to scan per repo        (default 50)
#   --jobs N      repos scanned concurrently                   (default 4)
#   --no-gh       skip every `gh` call (local signals only)
#   -h, --help    show this help
#
# Output (stdout, JSON):
#   {
#     "candidates": [ {"tracker", "confidence", "evidence": [..]} ],  # best first
#     "per_repo":   [ {"repo", "candidates": [{"tracker", "confidence"}]} ],
#     "scanned":    [ {"target", "repo", "local"} ],
#     "warnings":   [ ".." ]
#   }
#
# Signals (see references/trackers.md for the weights and their rationale):
#   - issue keys (ABC-123) in commit messages, PR titles and branch names
#   - tracker URLs (linear.app, *.atlassian.net, notion.so, app.asana.com,
#     trello.com) in commits, PR bodies and repo docs
#   - Linear-style branch names (user/abc-123-title), Jira smart commits
#   - GitHub: .github/ISSUE_TEMPLATE, open issue count, `fixes #N` closing
#     keywords, Projects (v2) linked to the repo or owned by the org
#
# Concurrency and rate limits: at most --jobs repos run at once, and each repo
# costs about 4 `gh` calls (3 when scanned from a local checkout). Before
# starting, the script reads `gh api rate_limit` (free) and drops to local-only
# signals when fewer than 50 core or GraphQL requests remain, with a warning.
# A failing `gh` call degrades to a warning; it never fails the run.

set -euo pipefail

COMMITS=200
PRS=50
JOBS=4
USE_GH=1
TARGETS=()

usage() { sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; }

while (($#)); do
  case "$1" in
    --commits) COMMITS=${2:?}; shift 2 ;;
    --prs) PRS=${2:?}; shift 2 ;;
    --jobs) JOBS=${2:?}; shift 2 ;;
    --no-gh) USE_GH=0; shift ;;
    -h | --help) usage; exit 0 ;;
    --) shift; TARGETS+=("$@"); break ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *) TARGETS+=("$1"); shift ;;
  esac
done
((${#TARGETS[@]})) || TARGETS=(.)

for n in "$COMMITS" "$PRS" "$JOBS"; do
  [[ $n =~ ^[0-9]+$ ]] || { echo "numeric option expected, got: $n" >&2; exit 2; }
done
((JOBS >= 1)) || JOBS=1

command -v jq >/dev/null || { echo '{"candidates":[],"scanned":[],"warnings":["jq not found; install jq"]}'; exit 0; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

GLOBAL_WARN="$TMP/global.warn"
: >"$GLOBAL_WARN"

# `timeout` is optional (absent on stock macOS); fall back to running directly.
if command -v timeout >/dev/null; then
  gh_() { timeout 30 gh "$@"; }
else
  gh_() { gh "$@"; }
fi

if ((USE_GH)); then
  if ! command -v gh >/dev/null; then
    echo "gh not installed: GitHub signals skipped" >>"$GLOBAL_WARN"; USE_GH=0
  elif ! gh auth status >/dev/null 2>&1; then
    echo "gh not authenticated: GitHub signals skipped" >>"$GLOBAL_WARN"; USE_GH=0
  else
    rl=$(gh_ api rate_limit --jq '[.resources.core.remaining, .resources.graphql.remaining] | @tsv' 2>/dev/null || echo "")
    core=${rl%%$'\t'*}; gql=${rl##*$'\t'}
    if [[ -z $rl ]]; then
      echo "gh rate_limit unreadable: GitHub signals may be partial" >>"$GLOBAL_WARN"
    elif ((core < 50 || gql < 50)); then
      echo "gh rate limit low (core=$core graphql=$gql): GitHub signals skipped" >>"$GLOBAL_WARN"; USE_GH=0
    fi
  fi
fi

# Keys that look like ABC-123 but are standards, hashes or versions, not issues.
KEY_DENY='^(UTF|SHA|MD|ISO|RFC|CVE|CWE|GHSA|HTTP|HTTPS|TLS|SSL|AES|RSA|ECDSA|ED|X|PR|V|UTC|GMT|IPV|WCAG|ES|ECMA|IEEE|ANSI|PEP|JSR|JEP|KEP|KIP|FLIP|SPIP|HIP|RIP|EIP|BIP|ERC|SEP|TC|PHP|GPT|GPL|LGPL|AGPL|BSD|MIT|APACHE|CC|OAUTH|OIDC|JWT|H|P|R|K|S|T|N|Q|W|WIN|MAC|ARM|X86|AMD|IE|OS|SDK|API|UI|UX|US|EU|AP|AZ|COVID|BASE|INT|UINT|FLOAT|CLAUDE|GEMINI|LLAMA|O)$'

# emit REPO TRACKER KIND WEIGHT DETAIL -> one JSONL signal line on stdout
emit() {
  jq -cn --arg repo "$1" --arg tracker "$2" --arg kind "$3" --argjson w "$4" --arg detail "$5" \
    '{repo:$repo, tracker:$tracker, kind:$kind, weight:$w, detail:$detail}'
}

# Classify tracker URLs on stdin; emit one signal per tracker found.
# Jira matches Atlassian Cloud and self-hosted instances (any host serving
# /browse/KEY-123 or /jira/browse/KEY-123).
url_signals() { # REPO SOURCE-LABEL WEIGHT
  local repo=$1 src=$2 w=$3 urls
  urls=$(grep -oE 'https?://[A-Za-z0-9.-]+(:[0-9]+)?/[^][ )>"'\''`]*' |
    grep -iE '(linear\.app|atlassian\.net|notion\.(so|site)|app\.asana\.com|trello\.com)/|/browse/[A-Z][A-Z0-9]+-[0-9]+' || true)
  [[ -n $urls ]] || return 0
  local t pat n sample
  for t in linear jira confluence notion asana trello; do
    case $t in
      linear) pat='linear\.app/' ;;
      jira) pat='atlassian\.net/(browse|jira|projects)/|/browse/[A-Z][A-Z0-9]+-[0-9]+' ;;
      confluence) pat='atlassian\.net/wiki/' ;;
      notion) pat='notion\.(so|site)/' ;;
      asana) pat='app\.asana\.com/' ;;
      trello) pat='trello\.com/[bc]/' ;;
    esac
    n=$(grep -ciE "$pat" <<<"$urls" || true)
    ((n > 0)) || continue
    sample=$(grep -iE "$pat" <<<"$urls" | head -n1 | sed -E 's#^https?://##' | cut -c1-80)
    if [[ $t == confluence ]]; then
      # Confluence proves an Atlassian site, not that Jira is the tracker.
      emit "$repo" jira "atlassian-site-$src" 0.15 "$n Confluence link(s) in $src (e.g. $sample)"
    else
      emit "$repo" "$t" "url-$src" "$w" "$n $t link(s) in $src (e.g. $sample)"
    fi
  done
}

# Issue keys (ABC-123) on stdin -> "PREFIX COUNT DISTINCT" lines, noise removed.
issue_keys() {
  grep -oE '(^|[^A-Za-z0-9_/.-])[A-Z][A-Z0-9]{1,9}-[0-9]{1,6}([^A-Za-z0-9_.-]|$)' |
    grep -oE '[A-Z][A-Z0-9]{1,9}-[0-9]+' |
    awk -F- -v deny="$KEY_DENY" '$1 !~ deny { c[$1]++; if (!seen[$0]++) d[$1]++ }
      END { for (k in c) if (d[k] >= 2) print k, c[k], d[k] }' |
    sort -k2,2nr | head -n 5 || true
}

scan_repo() { # INDEX TARGET
  local idx=$1 target=$2 out="$TMP/$1.jsonl" warn="$TMP/$1.warn" meta="$TMP/$1.meta"
  local dir="" slug="" local_flag=false commits="" prs="" branches="" docs=""
  : >"$out"; : >"$warn"

  if [[ -d $target ]]; then
    if ! git -C "$target" rev-parse --git-dir >/dev/null 2>&1; then
      echo "$target: not a git repository, skipped" >>"$warn"
      jq -cn --arg t "$target" '{target:$t, repo:null, local:true}' >"$meta"
      return 0
    fi
    dir=$(git -C "$target" rev-parse --show-toplevel)
    local_flag=true
    local url
    url=$(git -C "$dir" remote get-url origin 2>/dev/null || true)
    if [[ $url =~ github\.com[:/]([^/]+)/([^/]+)$ ]]; then
      slug="${BASH_REMATCH[1]}/${BASH_REMATCH[2]%.git}"
    fi
  elif [[ $target =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
    slug=$target
  else
    echo "$target: neither a directory nor owner/name, skipped" >>"$warn"
    jq -cn --arg t "$target" '{target:$t, repo:null, local:false}' >"$meta"
    return 0
  fi
  local repo=${slug:-${dir##*/}}
  repo=${repo:-$target}
  jq -cn --arg t "$target" --arg r "$repo" --argjson l "$local_flag" '{target:$t, repo:$r, local:$l}' >"$meta"

  # ---- commit messages -------------------------------------------------------
  if [[ -n $dir ]]; then
    commits=$(git -C "$dir" log -n "$COMMITS" --format='%B%x1e' 2>/dev/null || true)
  elif ((USE_GH)) && [[ -n $slug ]]; then
    local per=$((COMMITS > 100 ? 100 : COMMITS))
    commits=$(gh_ api "repos/$slug/commits?per_page=$per" --jq '.[].commit.message' 2>/dev/null) ||
      echo "$repo: could not list commits via gh" >>"$warn"
  fi

  # ---- PR titles, bodies, branch names --------------------------------------
  if ((USE_GH)) && [[ -n $slug ]] && ((PRS > 0)); then
    local prjson
    if prjson=$(gh_ pr list -R "$slug" --state all --limit "$PRS" --json title,body,headRefName 2>/dev/null); then
      prs=$(jq -r '.[] | .title + "\n" + (.body // "")' <<<"$prjson")
      branches=$(jq -r '.[].headRefName' <<<"$prjson")
    else
      echo "$repo: could not list pull requests via gh" >>"$warn"
    fi
  fi

  # ---- repo docs (local only; bounded) ---------------------------------------
  if [[ -n $dir ]]; then
    docs=$( (
      cd "$dir"
      find . -maxdepth 1 -type f \( -iname 'README*' -o -iname 'CONTRIBUTING*' \) -size -512k -print0
      [[ -d .github ]] && find .github -type f -size -256k -print0
    ) 2>/dev/null | (cd "$dir" && xargs -0 -r cat 2>/dev/null) | head -c 2000000 || true)
  fi

  # ---- URL signals -----------------------------------------------------------
  {
    url_signals "$repo" "commits" 0.45 <<<"$commits"
    url_signals "$repo" "PR bodies" 0.5 <<<"$prs"
    url_signals "$repo" "repo docs" 0.3 <<<"$docs"
  } >>"$out"

  # ---- issue keys ------------------------------------------------------------
  # A key prefix seen inside a tracker URL (linear.app/.../NAR-435,
  # .../browse/KAFKA-1) is attributed to that tracker; the rest stay ambiguous.
  local all keys lin_pfx jira_pfx
  all=$(printf '%s\n%s\n%s\n%s\n' "$commits" "$prs" "$branches" "$docs")
  lin_pfx=$(grep -oiE 'linear\.app/[^/ ]+/issue/[A-Za-z][A-Za-z0-9]*-[0-9]+' <<<"$all" |
    sed -E 's#.*/##; s#-[0-9]+$##' | tr '[:lower:]' '[:upper:]' | sort -u || true)
  jira_pfx=$(grep -oE '/browse/[A-Z][A-Z0-9]+-[0-9]+' <<<"$all" | sed -E 's#/browse/##; s#-[0-9]+$##' | sort -u || true)
  keys=$(printf '%s\n%s\n%s\n' "$commits" "$prs" "$branches" | issue_keys)
  if [[ -n $keys ]]; then
    local pfx cnt dist tr w amb="" amb_total=0
    while read -r pfx cnt dist; do
      tr=""
      grep -qx "$pfx" <<<"$lin_pfx" && tr=linear
      grep -qx "$pfx" <<<"$jira_pfx" && tr=jira
      if [[ -n $tr ]]; then
        w=0.5
        ((dist < 5)) && w=0.35
        emit "$repo" "$tr" "issue-keys-$pfx" "$w" "$pfx-* issue keys x$cnt ($dist distinct), prefix confirmed by a $tr URL" >>"$out"
      else
        amb+="${amb:+, }$pfx-* x$cnt"
        amb_total=$((amb_total + dist))
      fi
    done <<<"$keys"
    if [[ -n $amb ]]; then
      w=0.2
      ((amb_total >= 10)) && w=0.3
      emit "$repo" jira "issue-keys" "$w" "issue keys in commits/PRs: $amb (Jira or Linear)" >>"$out"
      emit "$repo" linear "issue-keys" "$w" "issue keys in commits/PRs: $amb (Jira or Linear)" >>"$out"
    fi
  fi

  # Linear's "copy git branch name" format: user/abc-123-short-title
  local lb
  lb=$(grep -cE '^[A-Za-z0-9._-]+/[a-z][a-z0-9]{1,9}-[0-9]+(-|$)' <<<"$branches" || true)
  ((lb >= 2)) && emit "$repo" linear "branch-names" 0.35 "$lb PR branch(es) in Linear's user/key-123-title format" >>"$out"

  # Jira smart commits: KEY-123 #comment / #time / #done / #close
  local sc
  sc=$(grep -cE '[A-Z][A-Z0-9]{1,9}-[0-9]+ .*#(comment|time|done|close|resolve|in-progress)\b' <<<"$commits" || true)
  ((sc >= 1)) && emit "$repo" jira "smart-commits" 0.4 "$sc Jira smart-commit message(s) (KEY-1 #comment/#time/#done)" >>"$out"

  # GitHub closing keywords (not squash-merge "(#17)" PR suffixes)
  local gk
  gk=$(printf '%s\n%s\n' "$commits" "$prs" | grep -ciE '\b(close[sd]?|fix(e[sd])?|resolve[sd]?) ([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#[0-9]+' || true)
  ((gk >= 1)) && emit "$repo" github "closing-keywords" 0.3 "$gk commit/PR message(s) close GitHub issues (fixes #N)" >>"$out"

  # ---- GitHub issue templates ------------------------------------------------
  local tmpl=""
  if [[ -n $dir && -d $dir/.github/ISSUE_TEMPLATE ]]; then
    tmpl=$(find "$dir/.github/ISSUE_TEMPLATE" -maxdepth 1 -type f | wc -l | tr -d ' ')
  elif [[ -n $dir && -f $dir/.github/ISSUE_TEMPLATE.md ]]; then
    tmpl=1
  elif [[ -z $dir ]] && ((USE_GH)) && [[ -n $slug ]]; then
    tmpl=$(gh_ api "repos/$slug/contents/.github/ISSUE_TEMPLATE" --jq 'length' 2>/dev/null || true)
  fi
  [[ $tmpl =~ ^[0-9]+$ ]] && ((tmpl > 0)) &&
    emit "$repo" github "issue-templates" 0.3 ".github/ISSUE_TEMPLATE has $tmpl file(s)" >>"$out"

  # ---- GitHub issues + Projects (one GraphQL call) ----------------------------
  if ((USE_GH)) && [[ -n $slug ]]; then
    local owner=${slug%%/*} name=${slug#*/} g
    # shellcheck disable=SC2016  # GraphQL variables, not shell expansions
    local q='query($o:String!,$n:String!){repository(owner:$o,name:$n){hasIssuesEnabled
      open:issues(states:OPEN){totalCount} recent:issues(first:1,orderBy:{field:CREATED_AT,direction:DESC}){nodes{createdAt}}
      projectsV2(first:1){totalCount}
      owner{__typename ... on Organization{projectsV2(first:1){totalCount}}}}}'
    if g=$(gh_ api graphql -f query="$q" -F o="$owner" -F n="$name" 2>"$TMP/$idx.gqlerr"); then
      local enabled open last rp op
      enabled=$(jq -r '.data.repository.hasIssuesEnabled' <<<"$g")
      open=$(jq -r '.data.repository.open.totalCount // 0' <<<"$g")
      last=$(jq -r '.data.repository.recent.nodes[0].createdAt // ""' <<<"$g")
      rp=$(jq -r '.data.repository.projectsV2.totalCount // 0' <<<"$g")
      op=$(jq -r '.data.repository.owner.projectsV2.totalCount // 0' <<<"$g")
      if [[ $enabled == false ]]; then
        echo "$repo: GitHub Issues disabled (tracker is likely external)" >>"$warn"
      elif ((open > 0)); then
        local w=0.25
        ((open >= 10)) && w=0.4
        emit "$repo" github "open-issues" "$w" "$open open GitHub issue(s), newest ${last:0:10}" >>"$out"
      fi
      ((rp > 0)) && emit "$repo" github "repo-projects" 0.3 "$rp GitHub Project(s) linked to the repo" >>"$out"
      ((op > 0)) && emit "$repo" github "org-projects" 0.2 "org $owner has $op GitHub Project(s)" >>"$out"
    else
      # Projects need the read:project scope; retry issues-only before giving up.
      if grep -qi 'project' "$TMP/$idx.gqlerr"; then
        echo "$repo: GitHub Projects not readable (token lacks read:project; 'gh auth refresh -s read:project' adds it); issue count only" >>"$warn"
      fi
      # shellcheck disable=SC2016
      local q2='query($o:String!,$n:String!){repository(owner:$o,name:$n){hasIssuesEnabled open:issues(states:OPEN){totalCount}}}'
      if g=$(gh_ api graphql -f query="$q2" -F o="$owner" -F n="$name" 2>/dev/null); then
        local open
        open=$(jq -r '.data.repository.open.totalCount // 0' <<<"$g")
        ((open > 0)) && emit "$repo" github "open-issues" 0.25 "$open open GitHub issue(s)" >>"$out"
      else
        echo "$repo: GitHub GraphQL query failed" >>"$warn"
      fi
    fi
  fi
  return 0
}

# ---- fan out over targets, at most $JOBS at once ------------------------------
i=0
for t in "${TARGETS[@]}"; do
  while (($(jobs -rp | wc -l) >= JOBS)); do wait -n 2>/dev/null || true; done
  scan_repo "$i" "$t" &
  i=$((i + 1))
done
wait

# ---- score ---------------------------------------------------------------------
# Per tracker, keep the strongest signal of each kind per repo, then combine
# with noisy-OR: confidence = 1 - prod(1 - w). Bounded in [0,1], monotone in
# evidence, and one weak signal alone never looks certain. `candidates` pools
# all repos (what factory asks the user to confirm); `per_repo` keeps the
# per-repo ranking so mixed orgs (one repo on Jira, one on GitHub) are visible.
cat "$TMP"/*.jsonl 2>/dev/null | jq -s \
  --slurpfile scanned <(cat "$TMP"/*.meta 2>/dev/null | jq -s '.') \
  --rawfile warn <(cat "$GLOBAL_WARN" "$TMP"/*.warn 2>/dev/null) '
  def rank: group_by(.tracker) | map(
      (group_by([.repo, .kind]) | map(max_by(.weight))) as $sig
      | {
          tracker: .[0].tracker,
          confidence: ((1 - ($sig | map(1 - .weight) | reduce .[] as $x (1; . * $x))) * 100 | round / 100),
          evidence: ($sig | sort_by(-.weight) | map(.repo + ": " + .detail))
        }
    ) | sort_by(-.confidence);
  . as $all
  | ($scanned[0] // []) as $s
  | {
      candidates: ($all | rank),
      per_repo: ($s | map(select(.repo != null) | .repo as $r
        | {repo: $r, candidates: ($all | map(select(.repo == $r)) | rank | map({tracker, confidence}))})),
      scanned: $s,
      warnings: ($warn | split("\n") | map(select(length > 0)))
    }'
