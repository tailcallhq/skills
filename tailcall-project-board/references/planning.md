# From a goal to a grounded plan

Read this when the user hands you a feature goal, spec or rough idea and wants
it turned into a design and a backlog ("plan X into the board", "break this
down", "how should we build X — then ticket it"). The output is a short
decision record plus issues that later agents can run without this
conversation. Planning writes to the board only; it never executes.

## 1. Ground before you design

Designs that ignore the code or earlier decisions get re-litigated in review.
Spend a few reads first, scaled to the change:

- **The code.** `search` / `read` (and `sem_search` if it works here) for the
  modules the feature touches, the nearest similar feature, the tests that
  cover them, and repo conventions (`AGENTS.md`, README, CONTRIBUTING).
- **Prior decisions.** The board itself (existing issues and blockers — so you
  extend instead of duplicate), linked conversations, and scoped memory or
  conversation recall *about this project*. Take the decision and its reason;
  don't paste transcripts.
- **The runtime.** Tool schemas differ between Forge builds (labels, saved
  views, filters). Read the schema the tools present in this session rather
  than trusting a remembered shape.

Stop reading once you can name the files that change and the pattern to
follow. A plan grounded in five files beats one grounded in fifty.

## 2. Ask only what the code can't answer

Resolve what you can from the repo and the request, then ask **one message**
with only the questions whose answers change the design or the scope —
typically: who the consumer is, compatibility promises (public API? stored
data?), and hard constraints (latency, cost, deadline). Offer your default
for each so "yes" is a valid reply. If nothing material is open, don't ask:
state your assumptions in the plan instead.

## 3. Decide, briefly

Where more than one approach is viable, compare 2–3 in a small table
(approach, cost, risk, reversibility) and pick one with a one-line reason.
Skip the table when there is one obvious approach. Record explicit choices
only where they apply:

- **API/contract**: new vs changed endpoints/types; versioning or
  backwards-compatibility.
- **Data**: schema/migration shape, backfill, whether it's reversible.
- **Non-functional**: the constraint and the number (p95, memory, cost), or
  "none stated".
- **Test strategy**: which levels (unit/integration/e2e) prove which criteria.
- **Rollback**: flag, revert, or down-migration — whichever actually undoes it.

Write decisions, not essays. No diagrams unless asked.

For a genuinely contested choice, a `task` sub-agent can independently check
the design (e.g. "find reasons approach B fails in this repo"), read-only,
with an explicit `cwd`. Use it for risk, not ceremony.

## 4. Draft the backlog

Each issue must be runnable by an agent that sees only its title, body and
attachments:

```markdown
<title: imperative, one outcome>

Context: why, and the decision it implements (1–3 lines).
Scope: files/modules it touches. Out of scope: what it must not do.
Acceptance:
- [ ] observable, checkable criteria (a test passes, an endpoint returns X)
Verify: the command(s) or checks that prove it.
Risks: only real ones, with the mitigation.
```

Every runnable (leaf) issue gets its own `Acceptance` and `Verify` lines,
even when they repeat a sibling's: the agent running it sees only that issue
and its ancestors, never its siblings. `Verify` names the exact command or
check (`python3 -m unittest tests.test_store`, `curl -X DELETE .../notes/1`
→ 204), not "add tests". Umbrella issues carry the decisions instead.

Sizing: one reviewable change per issue. Split by independently mergeable
slices (contract → implementation → consumer), not by layer-of-the-same-diff.
If two issues must land together to compile, they are one issue.

Relations — get these right, they are what makes the backlog executable:

- **Containment** (`parent` / `sub_issues`): an umbrella for grouping only.
  Running a parent runs nothing.
- **Ordering** (`set_blocked_by`): B cannot start until A's output exists.
  Add a blocker only for a real dependency (needs A's code, schema or
  decision); spurious blockers serialise work that could run in parallel.
- An **unresolved decision** the user must make becomes its own issue (or a
  question now), and the work depending on it is blocked by it.

Put the decision record in the umbrella issue body (or the project
description, if it governs every issue). Attach durable context — a spec
`file`, the `working_directory` — rather than copying history, and never put
secrets or tokens into bodies or attachments.

## 5. Write it without collateral damage

- `project_get` first; reuse and update matching issues instead of adding
  duplicates, and add to the existing board rather than creating another one
  unless the user asked for a new board.
- Don't change existing statuses, priorities, labels, views, run config or
  attachments as a side effect. If attachments must grow, resend the existing
  list plus the new entry.
- New issues go in the board's first todo status. Create the umbrella in one
  call, then children and `set_blocked_by` in the next (ids are known only
  after the first call returns), each batch atomic.
- After writing, `project_get` once and check each new leaf issue has
  Acceptance and Verify and the intended blockers; fix gaps in one more batch.
- Approval of the plan is approval to **write the plan**, not to run it. Don't
  call `project_run` and don't start implementing; end by listing the
  ready-to-run issues (no open blockers) and asking which, if any, to start.

## 6. Report

Lead with the decision (one line each), then the issues in execution order,
marking which can run in parallel and what blocks what, then the assumptions
you made and any open question.
