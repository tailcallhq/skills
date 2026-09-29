# Posting a review to GitHub (only on explicit request)

Posting speaks publicly for the user, so it needs their explicit go-ahead in
this conversation. Show the payload before sending unless they already
approved its exact contents.

- Post **CONFIRMED** findings only. Unverified risks stay in chat.
- One short, actionable comment per finding, on a **changed** line of the PR
  head (`side=RIGHT`). No headings, no severity labels, no praise.
- Keep `body` empty unless the user asked for a summary.
- Use `event: COMMENT` unless the user asked to approve or request changes.

Payload:

```json
{"body": "", "event": "COMMENT",
 "comments": [{"path": "src/x.ts", "line": 42, "body": "`limit` is never applied when `offset` is 0; pass it in both branches."}]}
```

Send it from inside the repo (needs `gh` and `jq`):

```bash
PAYLOAD=$(mktemp) && cat > "$PAYLOAD"   # write the JSON
bash <skill-dir>/scripts/post-review.sh <PR_NUMBER> "$PAYLOAD"; rm -f "$PAYLOAD"
```

The script validates the payload shape and prints the review URL. If GitHub
rejects a line (not part of the diff), move the comment to the nearest changed
line or report it in chat instead.
