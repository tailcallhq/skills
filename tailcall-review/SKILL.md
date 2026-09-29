---
name: tailcall-review
description: Evidence-verified code review of a change before it ships — a GitHub PR, a branch, a worktree, uncommitted edits, or a path in a monorepo. Use whenever the user says "review this PR", "review my changes", "check this before I merge", "second opinion on this diff", "any bugs in what I just wrote?", or when another workflow needs a review gate. Finds candidate defects, verifies each against the code, and reports CONFIRMED defects separately from unverified risks. Read-only by default; fixing code or posting PR comments needs the user's explicit go-ahead.
---

# Evidence-verified code review

The goal is a short list of defects the user can trust, not a long list they
have to re-check. Every reported defect needs file evidence, a concrete way it
fails, and why it matters. "No defects found" is a valid, useful result — do
not invent findings to fill a quota.

**Default is read-only.** Don't edit the code, commit, push, or post GitHub
comments unless the user asked for that in this request. A reviewer that
silently fixes things produces unreviewed edits; a reviewer that posts
publicly speaks for the user. If they want fixes or comments, see step 7.

## 1. Resolve the scope

Figure out exactly what "the change" is, then run the bundled resolver so the
base, file list and diff are explicit:

```bash
python3 <skill-dir>/scripts/review_scope.py --cwd <repo>                 # branch + all local edits
python3 <skill-dir>/scripts/review_scope.py --cwd <repo> --base origin/main
python3 <skill-dir>/scripts/review_scope.py --cwd <repo> --path services/api   # monorepo slice
python3 <skill-dir>/scripts/review_scope.py --cwd <repo> --pr 123        # needs gh
```

Add `--json` for metadata only, `--stat` to omit the diff body. Branch mode
diffs the merge-base to the **working tree**, so committed, staged, unstaged
and untracked files are all included; it handles a repo with no commits, no
upstream, and `master`-style defaults. It never fetches or writes.

Use the user's explicit scope (PR number, branch, base, path) when given.
Otherwise take the resolver's default and **say which base it chose and why**
in the report — a review of the wrong range looks exactly like a clean review.
If the packet says nothing changed, stop and ask rather than reviewing the
whole repo. For a PR, if the notes say local HEAD differs from the PR head,
read code at the PR head (`git show <head>:<path>`) or check it out in a
separate worktree with the user's OK.

Also collect the **intent**: the user's request, PR description, linked issue.
A change can be internally consistent and still not do what was asked; that
is often the most valuable finding.

## 2. Understand the surrounding contracts

Read the changed files in full, then trace outward — the diff alone hides most
real bugs:

- callers of changed functions, readers/writers of changed state, config keys,
  schemas, CLI flags, API routes (`search` for the names; `sem_search` if it
  is available and authenticated);
- the repo's own rules: `AGENTS.md`, `CONTRIBUTING`, README, CI config, nearby
  code patterns. They override generic checklists.

For each changed path, trace input → decision → side effect. Ask which value
wins when several sources exist, whether the path the feature actually uses is
the path that changed, and what happens on empty/error/concurrent inputs.
`references/checklist.md` has a longer prompt list; use it as a reminder, not
a script.

## 3. Gather candidate findings

Write each candidate as: `file:line`, the mechanism, and a concrete failure
scenario (input/state → wrong result). If you can't state a scenario, it isn't
a candidate yet.

In scope: correctness, broken callers/contracts, security, data loss,
concurrency, error handling, missing tests for changed behavior, and things
that shouldn't ship (debug output, secrets, stray artifacts). **Out of scope:**
formatting, naming taste, and style opinions the repo's tooling or docs don't
require — they drown out real defects.

**Budget.** Keep at most ~10 candidates for verification, choosing by
potential impact; list any you dropped in one line each. For a large change
(say >1,500 diff lines or >30 files), split by area and give each area to a
separate reviewer (step 5) instead of skimming.

Then **deduplicate by root cause**: several symptoms of one mistake are one
finding, keep the most concrete scenario and mention the other locations.

## 4. Verify each candidate

Try to prove each candidate wrong before believing it. Re-read the exact code,
check guards elsewhere (callers, validation, types, framework behavior), and
where cheap and safe, reproduce: a targeted existing test, a small temporary
test or script, or the repo's test/browser adapters if present. Keep
reproduction local, bounded by timeouts, and delete temporary files afterwards
unless the user asks to keep them. Never hit production services.

Assign exactly one state:

- **CONFIRMED** — the code path is traced end to end and the failure follows,
  or a reproduction showed it. Note which.
- **UNVERIFIED** — plausible, but depends on something you couldn't check
  (runtime config, external service, unknown caller, unexercised platform).
  Say what would settle it.
- **REFUTED** — something prevents the failure. Record the reason in one line;
  this shows the ground was covered.

Don't attach numeric confidence scores — they imply precision you don't have.
The state plus the evidence is the signal.

## 5. Independent reviewers (optional, bounded)

A fresh reader catches what the author (or first pass) talked itself past. Use
the `task` tool when the change is non-trivial and it's available:

- **Verifier**: for important candidates (anything you'd rate critical/major),
  give one read-only sub-agent the packet, the candidate, and the relevant
  paths, and ask it to return CONFIRMED / UNVERIFIED / REFUTED with evidence.
  Group several candidates per verifier rather than one agent each.
- **Area reviewers**: for large changes, 2–4 parallel reviewers each owning a
  slice.
- **Gap sweep**: after verification, one fresh reviewer gets the diff and the
  verified list and looks *only* for defects not already listed, returning at
  most 5 and an empty list if there are none. Verify anything it returns the
  same way.

Paste the packet text into the prompt — read-only agents may have no shell, so
"run this command" won't work for them. Pass the repo path as `cwd` and
include the intent. Tell them not to edit anything. Reserve the `workflow`
tool for very large reviews and only with the user's opt-in; it is not
available on every build.

A reviewer or verifier agreeing is evidence, not a guarantee: don't write that
the change "is correct" because nothing was found.

## 6. Report

Use this shape:

```markdown
## Review: <scope> (base <ref> — <reason>; N files, M commits, incl. uncommitted: yes/no)

### Confirmed defects
1. **<one-line title>** — `path/file.ext:42` (severity: critical|major|minor)
   - Evidence: <what the code does, cross-file effects, quote ≤3 lines>
   - Failure: <concrete input/state → wrong outcome>
   - Impact: <who/what is hurt>
   - Fix: <smallest change that addresses the root cause>
   - Verified by: <trace | test `cmd` → output | sub-agent>

### Unverified risks
- **<title>** — `path:line`: <scenario>. Would be settled by: <check>.

### Checked and ruled out
- <candidate> — <why it can't happen> (one line each)

### Coverage
What was read/traced, what ran, what couldn't be checked, candidates dropped by budget.
```

Write "None." under an empty section rather than omitting it, so "looked and
found nothing" is distinguishable from "didn't look". End with one sentence of
overall assessment; no praise, no restating the diff.

## 7. Only when asked: fix or post

- **Fix**: only on explicit request. Fix confirmed defects only, smallest
  change, in the user's worktree; rerun the relevant checks; show the diff. If
  a fix was substantive, offer a follow-up review of the new state.
- **Post to GitHub**: only on explicit request. Post CONFIRMED findings only,
  as short inline comments on changed lines of the PR head; see
  `references/github-posting.md` and `scripts/post-review.sh`. Show the user
  the payload first unless they already approved exact contents.

## Used by other workflows

`scripts/review_scope.py` is the shared scope resolver: testing, security and
PR-handoff workflows should call it (`--json`) rather than re-deriving the
base, so they agree on what "the change" is. When used as a gate inside a ship
workflow, review the exact state that will merge (post-squash/pushed), and if
review can't be obtained, say so instead of passing the gate silently.
