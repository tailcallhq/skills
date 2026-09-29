# tailcallhq/skills

Built-in skills shipped with Forge. Forge syncs this repository into
`~/.forge/tailcall-skills` and loads every `<skill>/SKILL.md` it contains.

## Layout

One directory per skill at the repository root, each containing a `SKILL.md`
with YAML frontmatter (`name`, `description`). The directory name must match
the `name` field.

## Naming

- `name` is one or two lowercase words joined by a hyphen (`code-review`,
  `debug`). No `tailcall-`/vendor prefix: the install path
  (`~/.forge/tailcall-skills`) already scopes them.
- Names must not collide with each other, nor with well-known third-party
  skills Forge also discovers (`~/.claude/skills`, `~/.codex/skills`,
  `~/.agents/skills`, ...). E.g. Anthropic's upstream `skill-creator` ships
  with Claude Code and Codex, so ours is `forge-skills`.
- `description` states what the skill does and when to use it in third
  person, ideally under ~300 characters (hard limit 1,024). Trigger phrases
  belong in the body or evals, not the description.

Skills fall into two groups (flat on disk — Forge only scans one directory
level):

| Group | Skills |
|---|---|
| Forge setup & operations | `forge-doctor`, `repo-setup`, `project-board`, `forge-skills` |
| SDLC | `code-review` (more on unmerged branches: `debug`, `performance`, `security-review`, `test-software`, `repo-health`) |

## Third-party skills

| Skill | Source | License | Modified |
|---|---|---|---|
| `forge-skills` | [anthropics/skills](https://github.com/anthropics/skills/tree/main/skills/skill-creator) @ `34040c9` | Apache-2.0 (see `forge-skills/LICENSE.txt`) | Yes — see NOTICE below |

## NOTICE

`forge-skills` is a derivative work of `skill-creator` from
[anthropics/skills](https://github.com/anthropics/skills) (commit `34040c9`),
used under the Apache License, Version 2.0. The original license text is
retained at `forge-skills/LICENSE.txt`.

This copy has been **modified by Tailcall for Forge, 2026**. As required by
Apache-2.0 §4(b), every changed file carries a prominent notice to that effect.
The changes are:

- `SKILL.md` — agent-neutral and Forge-specific wording; removed the
  Claude.ai-specific and Cowork-specific sections in favour of a single
  "Adapting to your environment" section; documented Forge's skill discovery
  paths (`~/.forge/skills`, and project `.forge/skills`, `.agents/skills`,
  `.claude/skills`); rewrote the description-optimization instructions for the
  Forge runner. Added a numbered mandatory eval loop at the top, an explicit
  pre-publishing review gate, headless/sub-agent handling, a "Running test
  prompts in Forge" section (sub-agents vs `forge_client`, and the token-count
  caveat), baseline-contamination guidance, side-effect safety for skills whose
  tools mutate real state, and the in-chat eval report template. The skill's
  `name` is unchanged.
- `scripts/forge_client.py` — **added.** A stdlib-only client that drives the
  `forge3` runner over its newline-delimited JSON-RPC 2.0 stdio protocol,
  replacing the upstream subprocess calls to the Anthropic CLI. Records the
  skills each run loaded, distinguishes timeouts from failures via
  `ForgeTimeout`, offers `isolate_global_skills`, and always reaps the child
  process.
- `scripts/eval_report.py` — **added.** Renders the in-chat eval report
  (per-eval pass rates, delta, trigger accuracy, failed assertions with
  evidence, contamination warnings) from `benchmark.json`, `grading.json` and
  trigger results.
- `scripts/test_eval_stats.py` — **added.** Stdlib-only unit checks for the
  confusion-matrix statistics, timeout handling, subprocess termination and
  contamination detection.
- `references/sdk-isolation-request.md` — **added.** Records the
  `forgecode-sdk` change needed to isolate user-global skills during evals.
  (No SDK change was made.)
- `scripts/run_eval.py` — trigger evaluation now runs through `forge3`, stages
  the candidate skill in a temp project's `.forge/skills/<name>/SKILL.md`
  rather than a slash-command file, and detects triggering from a `skill_view`
  tool call. Dropped the upstream CLI's environment handling. `--model` and
  `--provider` are now required, and timeouts are reported as errors and
  excluded from scoring instead of counting as non-triggers.
- `scripts/improve_description.py` — text completion now goes through
  `forge_client`; agent-neutral prompt wording.
- `scripts/run_loop.py` — updated for the `run_eval` signature change (no
  `project_root`) and the new required `--provider`. Added `--binary`, raised
  the default `--timeout` to 120s to match `run_eval`, fixed the
  precision/recall/accuracy computation so the correct count and the rates are
  derived from the same run-level counts, and the test split now reports its
  real elapsed time.
- `scripts/package_skill.py` — no longer writes the `.skill` file into the
  current directory; `-o/--output` selects a destination and the default is a
  temp directory whose path is printed.
- `agents/grader.md` — added a contamination check that records which skills
  each run loaded to `contamination.json`.
- `scripts/generate_report.py`, `eval-viewer/viewer.html` — user-facing copy
  refers to Forge and "the agent"; the viewer now shows each run's
  `transcript.md` in a collapsible section.
- `eval-viewer/generate_review.py` — embeds each run's transcript so
  `--static` output carries it too.
- `references/schemas.md` — the `executor_model` example is now generic.

The model and provider are required arguments throughout, with no default: the
calling session passes the pair powering it, so a trigger measurement reflects
what the user actually runs and never silently bills them for a model they
didn't choose.
