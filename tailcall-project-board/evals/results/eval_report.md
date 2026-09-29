## Eval report

**Skill**: `tailcall-project-board`  
**Model/provider**: `claude-opus-5-5` / `claudecode`  
**Workspace**: `/home/forge/workspaces/skills/tailcall-project-board-workspace/iteration-2`  
**Viewer**: /home/forge/workspaces/skills/tailcall-project-board-workspace/iteration-2/review.html

### Overall

| Configuration | Pass rate | Time | Tokens |
|---|---|---|---|
| old_skill | 92% ± 10% | 53.9s | 0 |
| with_skill | 98% ± 5% | 73.4s | 0 |

**Delta (pass rate)**: -0.06

### Per-eval

| Eval | with_skill | old_skill | Delta |
|---|---|---|---|
| 1 | 100% | 100% | +0pp |
| 2 | 97% | 83% | +13pp |
| 3 | 96% | 93% | +4pp |

### Trigger eval

_No trigger eval provided._ Note that a trigger eval measures only whether the description gets the skill loaded — it says nothing about whether the skill helps once loaded.

### Failed assertions

- **eval 2** (old_skill, run 1): Mentions a rollback or reversibility approach for the schema change
  - Evidence: No mention of rollback, revert, undo, down-migration or reversibility in issues_full.json or final_answer.md (grep for rollback|revert|undo|reverse|down-migration returns nothing).
- **eval 2** (old_skill, run 1): Every new leaf issue body contains its own acceptance criteria and a concrete verify command (judge from issues_full.json)
  - Evidence: Leaves have acceptance but no concrete command: storage 'Existing tests still pass'; core 'unit tests in tests/test_core.py ... existing test_add_list unchanged and passing'; CLI 'tests calling main([...])'. The only command `python3 -m unittest discover` is in the umbrella, not in any leaf.
- **eval 2** (old_skill, run 2): Mentions a rollback or reversibility approach for the schema change
  - Evidence: No rollback/revert/reversibility mention anywhere in issues_full.json or final_answer.md.
- **eval 2** (old_skill, run 2): Every new leaf issue body contains its own acceptance criteria and a concrete verify command (judge from issues_full.json)
  - Evidence: Leaf acceptance lacks a concrete command: storage 'Existing tests still pass'; core 'Unit tests: ... Existing tests still pass'; CLI 'Test calling main([...]) against a temp DB'. `python3 -m unittest discover` appears only in the umbrella's shared design.
- **eval 2** (old_skill, run 3): Mentions a rollback or reversibility approach for the schema change
  - Evidence: No rollback/revert/reversibility mention in issues_full.json or final_answer.md.
- **eval 3** (old_skill, run 1): Either asks at most one consolidated message of questions, or proceeds with explicitly stated assumptions
  - Evidence: FAIL: no questions, but no explicitly stated assumptions either — final answer never states an assumed target or what 'faster' means, and presents decisions (e.g. pagination default) as settled facts rather than assumptions.
- **eval 3** (old_skill, run 1): Flags any API-compatibility impact (e.g. changed GET /notes response) as a decision or assumption rather than silently planning a breaking change
  - Evidence: FAIL: 909cbfd4 sets 'limit ... default 100' on GET /notes (callers with >100 notes silently lose results) while claiming 'Backward compatibility: the response stays a JSON array'; final answer says 'Every issue also keeps the HTTP API backward compatible'. Behaviour change not flagged.
- **eval 2** (with_skill, run 3): Flags the overlap/interaction with the existing 'Add a `done` command to the CLI' issue instead of ignoring it
  - Evidence: No issue body mentions the existing `done` command ticket (only the Task.done field); final_answer.md (truncated at 'Nothing bl') has no mention; tool_calls contain no add_issue payload. No evidence the overlap was flagged.
- **eval 3** (with_skill, run 3): Risky concurrency work (threaded server) is blocked until the store is safe for concurrent access, or not planned
  - Evidence: FAIL: b6d13d82 'Serve requests concurrently...' (switch to ThreadingHTTPServer) has blocked_by [] and runs in parallel with the SQLite store (bcbb9f97, also unblocked). The run's own umbrella notes current reads are unlocked and can hit a half-written file, yet threading can land first.

### Contamination check

No contamination recorded. Note this is only meaningful if the harness wrote `contamination.json` for each run; absent files mean the check did not run, not that the runs were clean.

### Review gate

This report needs your sign-off before anything is committed, pushed or opened as a PR. Does the delta look right, and do the failed assertions reflect real problems?
