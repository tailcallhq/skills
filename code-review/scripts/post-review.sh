#!/bin/bash
# Post a PR review with inline comments using gh CLI
# Usage: ./post-review.sh <pr-number> <payload-file>
# The payload file should be a JSON file with the following structure:
# {
#   "body": "Overall review body",
#   "event": "COMMENT" | "APPROVE" | "REQUEST_CHANGES",
#   "comments": [
#     {
#       "path": "relative/file/path.rs",
#       "line": 42,
#       "body": "Comment body with markdown"
#     }
#   ]
# }

set -euo pipefail

PR_NUMBER="${1:?Usage: $0 <pr-number> <payload-file>}"
PAYLOAD_FILE="${2:?Usage: $0 <pr-number> <payload-file>}"

if [[ ! -f "$PAYLOAD_FILE" ]]; then
    echo "Error: Payload file not found: $PAYLOAD_FILE" >&2
    exit 1
fi

# Get repo info from git remote
if ! REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner); then
    echo "Error: Could not determine repository. Make sure you're in a git repo with gh configured." >&2
    exit 1
fi
if [[ -z "$REPO" ]]; then
    echo "Error: Could not determine repository. Make sure you're in a git repo with gh configured." >&2
    exit 1
fi

# Validate JSON is valid
if ! jq empty "$PAYLOAD_FILE" 2>/dev/null; then
    echo "Error: Invalid JSON in payload file: $PAYLOAD_FILE" >&2
    exit 1
fi

# Validate exactly one review object and all required comment fields.
if ! jq -se '
    length == 1 and (.[0] |
        type == "object" and
        (.body | type == "string") and
        (.event == "COMMENT" or .event == "APPROVE" or .event == "REQUEST_CHANGES") and
        (.comments | type == "array" and all(.[];
            type == "object" and
            (.path | type == "string" and length > 0) and
            (.body | type == "string" and length > 0) and
            (.line | type == "number" and . > 0 and . == floor)
        ))
    )
' "$PAYLOAD_FILE" >/dev/null 2>&1; then
    echo "Error: Payload must be one review object with a string body, a valid event, and a comments array containing nonempty path/body strings and positive integer lines." >&2
    exit 1
fi

# Post the review
REVIEW_URL=$(gh api "repos/${REPO}/pulls/${PR_NUMBER}/reviews" \
    --method POST \
    --input "$PAYLOAD_FILE" \
    --jq '.html_url' 2>&1) || {
    echo "Error posting review: $REVIEW_URL" >&2
    exit 1
}

echo "Review posted successfully: $REVIEW_URL"
