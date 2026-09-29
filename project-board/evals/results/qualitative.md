# Iteration 2 — qualitative comparison (with_skill vs old_skill)

All 18 runs loaded only `project-board`. None called `project_create` or `project_run`, and none modified tracked repo files (the only untracked files were `__pycache__`). Board settings and the seeded issue were preserved in every run. Issue-content checks used `issues_full.json`.

| Eval | with_skill (3 runs) | old_skill (3 runs) |
|---|---|---|
| 1 small-api-feature | 30/30 | 30/30 |
| 2 multi-module-change | 29/30 | 25/30 |
| 3 ambiguous-requirement | 26/27 | 25/27 |
| **Total** | **85/87** | **80/87** |

## Eval 1 — small API feature
The configs are tied on score, and both are strong. All 6 runs found the `len(notes)+1` id bug and ordered the fix before the endpoint with `set_blocked_by`. All 6 chose a 204/404 contract. The differences are in quality, not in pass/fail:
- **with_skill:**
  - Each leaf has a labelled `Verify:` line, often with a manual `curl -X DELETE` check.
  - Acceptance items are `- [ ]` checklists.
  - The umbrella has a `## Decisions` block covering idempotency, the id-reuse trade-off and rollback.
  - with_skill run-1 checked the duplicate-id bug empirically in a shell call.
  - Runs took about 1.7x longer (58–83s vs 39–45s).
- **old_skill:** leaves put "`python3 -m unittest` passes" inside Acceptance, with no separate Verify step. old_skill run-1 did not use an umbrella issue.
- **Verify-step assertion:** it passed for old_skill because a concrete command is present in each leaf. The assertion does not require a labelled Verify section, so as written it does not tell the two configs apart here.

## Eval 2 — multi-module change
with_skill is clearly better.
- **Rollback:** all 3 with_skill umbrellas have a `Rollback:` decision ("revert; extra column is harmless because old code selects explicit columns"). None of the 3 old_skill runs mention rollback or reversibility.
- **Verify step:** every with_skill leaf ends with `Verify: python3 -m unittest discover`. In old_skill run-1 and run-2, the command appears only in the umbrella. Their leaves say "existing tests still pass" with no command, so they fail. old_skill run-3 passes.
- **`done`-ticket overlap:** old_skill flagged it inside a leaf body in all 3 runs. with_skill flagged it in run-1 (leaf body) and run-2 (final answer only). with_skill run-3 did not flag it anywhere I could see; its final answer is truncated, so this fails on burden of proof. This is with_skill's only miss.
- **Both configs:**
  - Sensible storage → core → CLI blocker chains.
  - An append-only migration with a DEFAULT backfill.
  - A v1→v2 upgrade test.

## Eval 3 — ambiguous requirement
Both configs grounded their plans in the code: every run chose SQLite plus ThreadingHTTPServer plus keyset pagination, and every run included a baseline and a re-benchmark.
- **API compatibility:**
  - with_skill run-2 kept `GET /notes` fully compatible (pagination only when parameters are sent).
  - with_skill run-1 and run-3 raised the default cap of 100 as a question for the user.
  - old_skill run-2 and run-3 flagged the breaking change.
  - old_skill run-1 silently capped `GET /notes` at 100 while claiming "every issue keeps the HTTP API backward compatible". It fails.
- **Stated assumptions:** with_skill runs had explicit "Assumptions" or "Decision for you" sections. old_skill run-2 and run-3 had "Decisions to check" or "For you to change". old_skill run-1 stated none, so it fails.
- **Concurrency ordering:** in with_skill run-3, the ThreadingHTTPServer issue has no blocker and runs in parallel with the SQLite store, even though the run's own umbrella notes that current reads are unlocked. It fails. All 3 old_skill runs blocked threading on the store.

## Eval critique
- **Eval 1 verify-step assertion:** it can't separate a labelled Verify step from a unittest bullet inside Acceptance. If the labelled step is what the skill should teach, the assertion should require it explicitly.
- **Leaf vs umbrella:** "each new issue" (Eval 1 #4/#5, Eval 3 #4) was read as leaf issues, since the container umbrellas only hold decisions. Both configs were graded the same way; the wording should say so.
- **Eval 2 `done`-overlap assertion:** it doesn't say whether the flag must be on the board or can be in chat only. with_skill run-2 was passed on a chat-only flag.
- **No assertion covers:**
  - The id-reuse trade-off of max+1 after deleting the newest note (every run chose max+1; the with_skill runs acknowledged the trade-off).
  - Priority assignment. old_skill run-2 in eval 3 set High/Medium without being asked, and no assertion caught it.
