# tailcallhq/skills

Built-in skills shipped with Forge. Forge syncs this repository into
`~/.forge/tailcall-skills` and loads every `<skill>/SKILL.md` it contains.

## Layout

One directory per skill at the repository root, each containing a `SKILL.md`
with YAML frontmatter (`name`, `description`). The directory name must match
the `name` field.

## Third-party skills

| Skill | Source | License | Modified |
|---|---|---|---|
| `skill-creator` | [anthropics/skills](https://github.com/anthropics/skills/tree/main/skills/skill-creator) @ `34040c9` | Apache-2.0 (see `skill-creator/LICENSE.txt`) | Yes — see NOTICE below |

## NOTICE

`skill-creator` is a derivative work of `skill-creator` from
[anthropics/skills](https://github.com/anthropics/skills) (commit `34040c9`),
used under the Apache License, Version 2.0. The original license text is
retained at `skill-creator/LICENSE.txt`.

This copy has been **modified by Tailcall for Forge, 2026**. As required by
Apache-2.0 §4(b), every changed file carries a prominent notice to that effect.
The changes are:

- `SKILL.md` — agent-neutral and Forge-specific wording; removed the
  Claude.ai-specific and Cowork-specific sections in favour of a single
  "Adapting to your environment" section; documented Forge's skill discovery
  paths (`~/.forge/skills`, and project `.forge/skills`, `.agents/skills`,
  `.claude/skills`); rewrote the description-optimization instructions for the
  Forge runner. The skill's `name` is unchanged.
- `scripts/forge_client.py` — **added.** A stdlib-only client that drives the
  `forge3` runner over its newline-delimited JSON-RPC 2.0 stdio protocol,
  replacing the upstream subprocess calls to the Anthropic CLI.
- `scripts/run_eval.py` — trigger evaluation now runs through `forge3`, stages
  the candidate skill in a temp project's `.forge/skills/<name>/SKILL.md`
  rather than a slash-command file, and detects triggering from a `skill_view`
  tool call. Dropped the upstream CLI's environment handling.
- `scripts/improve_description.py` — text completion now goes through
  `forge_client`; agent-neutral prompt wording.
- `scripts/run_loop.py` — updated for the `run_eval` signature change (no
  `project_root`) and the new required `--provider`.
- `scripts/generate_report.py`, `eval-viewer/viewer.html` — user-facing copy
  refers to Forge and "the agent".
- `references/schemas.md` — the `executor_model` example is now generic.

The model and provider are required arguments throughout, with no default: the
calling session passes the pair powering it, so a trigger measurement reflects
what the user actually runs and never silently bills them for a model they
didn't choose.
