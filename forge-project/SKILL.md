---
name: forge-project
description: Create and run Forge project boards with the project_create, project_list, project_get, project_update and project_run tools — durable backlogs of issues with statuses, priorities, sub-issues, blockers and due dates. Use this whenever the user wants to start or set up a project, plan a chunk of work into issues or milestones, check progress or what is left, add/edit/reprioritise/close issues, reorganise a hierarchy, sequence work that depends on other work, change a board's settings (description, statuses, priorities, default filter, run defaults, attachments), or execute issues as agent runs — including phrasings like "spin up a project for X", "what's on my board", "add a ticket for", "mark that done", "what's the status of the migration", "what's blocking this", or "kick off that issue", even when the user never says the word "project". Not for tracking steps inside the current conversation; that is what todo_write is for.
---

# Forge projects

A **project** is a durable, named backlog the user owns and returns to across
conversations: a hierarchy of issues, each with a status and a priority, plus
project-level settings (description, statuses, priorities, attachments, run
defaults, due date, hidden columns, default filter). It is long-lived shared
state — the user's UI and other agents read the same board — so treat it as a
real datastore rather than scratch space. Conversation-local checklists belong
in `todo_write`; putting them on a board pollutes something the user keeps.

Five tools operate on it:

| Tool | Use for |
|---|---|
| `project_create` | Make a new board. The only tool that creates one |
| `project_list` | Find a project id by name, or show what projects exist |
| `project_get` | Read settings + a page of issues; get issue/status/priority ids |
| `project_update` | Every mutation: issues, hierarchy, settings, links, blockers |
| `project_run` | Start real agent conversations for issues (spends tokens) |

There is deliberately no delete-project tool on this surface. A board you
create stays until the user removes it in their UI, which is the main reason
to check for an existing project before making another one.

## The invariant: read before you write

Ids are per-project and user-defined. One board may call its statuses
`backlog`, `doing`, `shipped` and another `todo`, `in_progress`, `complete`.
Guessing a status id fails the whole batch; guessing an *issue* id silently
edits the wrong work item, which is worse because nothing errors.

So the sequence is always:

1. `project_list` (with a `query` when the user named one) → project id.
2. `project_get` → the real status ids, priority ids and issue ids.
3. `project_update` / `project_run` using only ids you just read.

Skip step 2 only when you read the project earlier in this same conversation
and nothing has changed since.

---

## Creating a project

`project_create` takes `name` and `run` as required, and everything else is
optional with sensible defaults: statuses default to `todo` / `in_progress` /
`complete`, priorities to `low` (1), `medium` (2), `high` (3). Reach for the
defaults unless the user's vocabulary genuinely differs — a bespoke status set
is something they then have to live with on every issue.

### Check first, then create

Run `project_list` with a search phrase before creating anything. Users often
mean a board they already have ("the billing thing"), and a near-duplicate
board is annoying to clean up given there is no delete tool here.

### The run config is a real choice

Every issue of the board executes on `run.provider_id` / `run.model_id`, so
these are not filler fields. Use `model_list` to see which providers are
actually signed in and which models they offer, rather than assuming the model
powering this conversation is the right one to spend on a hundred issue runs.
When the user named a model, use it. When they didn't, pick the obvious
signed-in option and state the choice in your summary — it is one
`set_project` to change, and nothing runs until someone calls `project_run`.
`run.agent_id` and `run.reasoning_effort` may be omitted, and then come from
the current conversation.

### The description is the highest-leverage field

Each issue is later run by an agent with **none of this conversation's
context**. It sees the issue title as its prompt, the issue body, and the
attachments resolved from the issue, its ancestors and the project. So the
project `description` is standing instructions to that future agent, not a
blurb for a human browsing a list. Short headed sections, each a rule:

```markdown
## What this is
Q4 migration of billing from Stripe Billing to our own invoicing service.

## Ground rules
- For BUGS, reproduce with a test before attempting a fix.
- Money values are integer minor units everywhere; never floats.
- Follow the AGENTS.md in the repo rather than inventing conventions.
```

Each part earns its place: one line of what the project is, so an agent reading
a terse title knows which product it is in; a procedure for a recurring class of
issue, so the user doesn't restate it on every ticket; constraints that prevent
plausible-but-wrong work; and a pointer to conventions that already exist
instead of restating them, so the two can't drift.

Keep it to rules that hold for *every* issue. Anything true of one issue
belongs in that issue's body, where it doesn't dilute the rest.

### Attachments

`attachments` are the durable context forwarded on every run:

- a `working_directory` attachment with the absolute repo path, so runs start
  in the right checkout instead of guessing. Include it when a checkout exists
  on this machine; when none does (a planning board, or a product whose repo is
  itself a task) leave it out and add it later — don't block setup on it, and
  don't clone anything to manufacture one;
- a `text` attachment carrying the standing instructions. Mirroring the
  description here is deliberate: `description` is what the user reads in the
  UI, the `text` attachment is what the agent receives;
- `file` attachments for specs or designs issues keep referring to.

They replace the stored list wholesale, so when adding one later, send the
existing ones back alongside it — read them with `project_get` first. Issue-level
attachments stack on top of the project's rather than replacing them, so attach
the screenshot to the one bug that needs it and leave the board alone.

### A default filter worth copying

A board that lists every issue forever becomes unreadable once a few dozen
land in `complete`; one that hides completed work outright leaves the agent
that just closed an issue unable to see its own result. This filter means
**everything still open, plus anything finished very recently**:

```json
{ "kind": "any", "exprs": [
  { "kind": "status", "any_of": ["todo", "in_progress"] },
  { "kind": "all", "exprs": [
    { "kind": "status",  "any_of": ["complete"] },
    { "kind": "updated", "op": "is_within", "amount": 30, "unit": "minutes" }
  ] }
] }
```

The recency arm is the interesting half: freshly closed issues stay visible
long enough to confirm or undo, then fall away on their own. Set it at
creation time and reach for it as the default shape unless the user wants
something else.

### Build it when you can, ask only when you can't

Creating a board runs nothing and spends nothing, and every setting you choose
(name, description, run config, attachments, filter) can be changed afterwards
with one `set_project`. The irreversible, expensive step is `project_run`, not
`project_create`. So when the request already gives you what you need, **build
the board in this turn**: create it, add the issues and milestones, and end
with a short summary of the choices you made so the user can adjust any of
them. A user who asked for "a project with milestones" and got back a
questionnaire and an empty list has been made to wait for nothing.

Stop and ask first only when a guess would quietly point every future run at
the wrong place:

- **No usable working directory.** The named path doesn't exist, or several
  checkouts plausibly match. Don't clone or invent one to fill the gap; ask,
  or create the board without the attachment and say so.
- **No sensible run config.** The user named no model and `model_list` offers
  no obvious default (e.g. several signed-in providers and nothing they've
  used before). If the user *did* name a provider/model, use it; if one
  signed-in option is the obvious choice, pick it and say so.

When you do ask, ask everything in one message alongside the plan you intend
to load, so a single "yes" lets you finish.

Create the board *before* adding issues: issues inherit the run settings and
context that exist when they are added.

---

## Planning work into issues

After creation, `project_update` adds the issues. The value is in each issue
being *runnable later by someone without this conversation's context*:

- A title that reads as an instruction, because `project_run` uses it verbatim
  as the agent's prompt. "Backfill invoice IDs for pre-2026 subscriptions"
  works as a prompt; "invoices thing" does not.
- A **kind prefix** when the board uses one (`BUG:`, `FEAT:`, `CHORE:`). Match
  the prefixes existing issues use rather than introducing new vocabulary —
  that is what lets the description say "for BUGS, repro first" and land.
- A body carrying the context, constraints and acceptance criteria that live
  only in this conversation, and nothing the description already says.
- Hierarchy that mirrors the work, not the conversation.

Prefer many small, independently runnable issues over a few sprawling ones:
each should fit one agent run, which is what makes them executable in parallel
and reviewable one at a time.

Set `status` / `priority` explicitly when the user expressed intent; otherwise
let the defaults apply (first todo status, lowest-weight priority) rather than
inventing a priority they never expressed.

### Milestones are issues

The data model has no separate milestone type, so when the user asks for
milestones or phases, express them as **parent issues with sub-issues** —
that is the structure that actually exists, and it tracks progress naturally
because a milestone's state is the state of its children. Nest children in
`sub_issues` on the `add_issue` draft to build a milestone and its work in one
operation. Note that running a parent never runs its children, so a milestone
issue is a container, not a task.

To nest under something created in *this* batch by id, you need a second call —
new ids are only known once the call returns.

### Batches are atomic and ordered

`project_update` takes one ordered batch: either every operation applies or
none does, and an error names the failing operation by position. Group related
edits into a single call so a half-applied reorganisation is impossible.

Operations: `set_project`, `add_issue`, `update_issue`, `remove_issue`,
`move_issue` (reparent, or relocate into another project), `link_conversation`,
`unlink_conversation`, `set_blocked_by`.

Order matters. Move issues off a status or priority **before** an operation
that drops it, since both sets are replaced wholesale and a value still in use
cannot disappear.

`remove_issue` deletes the issue **and its entire subtree**. When the user says
"done" they mean a complete-category status, not deletion — prefer
`update_issue`, and confirm before ever removing.

### Blockers: order, not nesting

Two relations exist between issues and they answer different questions:

- `parent` (sub-issues) is **containment**: B belongs *inside* A.
- `blocked_by` is **ordering**: B comes *after* A.

When the user says "B depends on A", "do A first", "B is waiting on A" or
"chain these", that is `set_blocked_by`, not a sub-issue — even for siblings
under the same milestone. It replaces the whole set (an empty list unblocks),
and the store refuses a cycle, a self-reference or an unknown issue. A blocker
is released when it reaches **any** complete-category status, and nothing runs
automatically when that happens.

---

## Tracking progress

Progress questions ("where are we", "what's left", "what shipped this week")
are read questions, and the honest answer depends on seeing the whole board.

`project_get` applies the project's `default_filter` when you pass no `filter`,
so **the first page may not be the whole board**. The response echoes
`default_filter` so you can see what was applied. To see everything:

```json
{ "kind": "all", "exprs": [] }
```

Filtering happens before paging, so a page holds up to `limit` *matching*
issues. When the result says more are available, call again with the cursor
**and the same filter** — the cursor does not carry it. Issues come back flat
with a `parent` reference, and a parent can sit on a later page or not match
the filter at all, so fetch every page before describing the tree or counting
work. Long bodies are clipped in the listing; if a decision depends on an
issue's full body, say so rather than reasoning from the preview.

### Push the question into the filter

Rather than paging the whole board and filtering in your head, ask a sharper
question. Combinators are `all` / `any` (with `exprs`) and `not` (with `expr`);
leaves are `status` / `priority` (`any_of`) and `created` / `updated` (with a
`condition`).

Open, high-priority work:

```json
{ "kind": "all", "exprs": [
  { "kind": "status",   "any_of": ["todo", "in_progress"] },
  { "kind": "priority", "any_of": ["high"] }
] }
```

What moved recently — note `is_within` measures elapsed time from now, so
`{ "amount": 3, "unit": "days" }` is 72 hours, not three calendar days,
whereas `is_relative_to_today` counts calendar days either side of today:

```json
{ "kind": "updated", "condition": { "op": "is_within", "amount": 3, "unit": "days" } }
```

### Recording progress

Moving work along is `update_issue` with a new `status`. Do it as the work
actually changes state, in the same turn, so the board never lies about what
is underway. Status ids come from the board's own set — read them, don't
assume `in_progress` exists.

When a conversation is genuinely about an issue, `link_conversation` records it
so the user can trace the work later. Linking is idempotent; unlinking a
conversation that was never linked is refused and takes the batch down with
it, so only unlink what you saw in `project_get`. Neither counts as editing the
issue. Don't record runs by hand — the host writes run history itself.

### Statuses and priorities

Both sets are replaced wholesale by `set_project`, in display order. Every
status carries a `category` of `todo`, `in_progress` or `complete`, and each
category must appear at least once. The category is what filters and tooling
reason about, so several statuses may share one — which is how a board can
distinguish *done* from *abandoned* while keeping both out of the open-work
view:

```json
"statuses": [
  { "id": "todo",        "name": "To Do",       "category": "todo" },
  { "id": "in_progress", "name": "In Progress", "category": "in_progress" },
  { "id": "complete",    "name": "Complete",    "category": "complete" },
  { "id": "won_t_fix",   "name": "Won't Fix",   "category": "complete" }
]
```

A `won_t_fix`-style status is the honest answer to work the user decides
against: closing it as complete erases the decision, and deleting it erases
the issue. Suggest adding one the first time the user abandons something.

Priorities carry a `weight` instead, higher being more important, with ids and
weights both unique. `hidden_columns` (`status`, `priority`, `runs`, `created`,
`updated`; title can never be hidden) trims the issue table for every client —
useful for a board that triages by status and sequence rather than by priority.

---

## Running issues

`project_run` starts a **real, detached agent conversation per issue**, with
the issue title as the prompt and its body and attachments as context, using
run settings resolved from the issue, its ancestors, then the project. That
costs real tokens and can change real files.

So: run issues only when the user explicitly asks for work to be executed, and
never run one to inspect it — `project_get` is how you read.

The request is validated as a whole (an unknown issue rejects the batch), then
each issue starts independently, and the tool returns once conversations are
*started*, not finished. Follow up with `read_conversation` to see what
happened.

**Check blockers before you run.** `project_get` marks each blocker `OPEN` or
`settled` — trust the marker, not the status name. If a blocker reads "not on
this page", read it with `filter: {"kind":"all","exprs":[]}`, since the board's
default filter usually hides exactly the completed issues you're looking for.
If any blocker is still OPEN, don't run the issue: say which issues block it
and ask whether to wait, run the blocker first, or run anyway. The tool will
not stop you — the run starts and the agent inside it asks the same question —
so running without asking just burns a conversation. Same rule when a chain's
earlier issue finishes: report that it is now unblocked and ask, rather than
starting the next link on your own.

Running does not move the issue on the board. If the board should reflect that
work is underway, include a `project_update` moving those issues to an
in-progress status in the same turn.

Before running anything, re-read the project's description and attachments —
they usually name the workflow the run is expected to follow, and that is the
whole reason the project context exists.

---

## Reporting back

The user is usually looking at this board in a UI, so a wall of ids helps
nobody. Summarise by title, grouped the way they asked (by status, by
milestone, by priority), and mention ids only when they'll need one.

For progress questions, lead with the shape of the answer — how much is done,
what is in flight, what is blocked and on what — rather than dumping the list.
And if a filter or the board's `default_filter` meant you didn't see every
issue, say so: an incomplete count presented as complete is the failure mode
that actually costs the user something.
