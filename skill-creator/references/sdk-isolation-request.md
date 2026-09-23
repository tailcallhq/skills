<!-- Added by Tailcall for Forge, 2026 -->

# SDK change needed: isolate global skills for a run

Written from the skill-creator side. **No change has been made to
`forgecode-sdk`** — this note records what the eval harness needs so the work
can be picked up there.

## The problem

A behavioural eval compares a with-skill run against a baseline run. On a real
machine the baseline is not skill-free: the user's globally-installed skills
load in every run, so the comparison is actually

    candidate + user's skills   vs   user's skills

rather than "skill vs no skill". When a user-global skill overlaps the
candidate's purpose, the measured delta is meaningless. This was observed
concretely: `tailcall-project` was `skill_view`'d in 5 of 6 runs during the
`forge-project` eval, including all three baselines.

## Why nothing existing works

Checked against `forge3` 0.19.0:

| Approach | Result |
|---|---|
| `tool-skill` config `skill_dirs` | Only *appends*. `Config::effective_skill_dirs` (`crates/tool-skill/src/config.rs:80`) always starts from `default_skill_dirs()`, and `home_skill_dirs()` (`known_dirs.rs:173`) reads `dirs::home_dir()` unconditionally. No opt-out field. |
| Environment variable | None exists — `tool-skill` reads no env vars at all. |
| `extension_set_enabled` on `tool.skill` | Exists on the wire and returns success, but has **no effect**: a follow-up `extension_list` still reports `enabled: true`, and `tool_list` still contains `skill_search`/`skill_view`. Appears to be a host bug — `handle_extension_set_enabled` (`crates/core-host-sdk/src/host.rs:1056`) updates `ExtensionInfo::enabled` and invalidates routes, but tool registration is evidently not filtered on that flag. |
| Overriding `HOME` | Does hide the directories, but breaks authentication (`unauthorized: Please log in to Claude Code`), since the login lives under `HOME` too. |

## What would fix it

Either of these, in rough order of preference:

1. **An opt-out in `tool-skill`'s config.** A `use_default_skill_dirs: bool`
   (default `true`) so that setting it `false` makes `effective_skill_dirs()`
   return only the explicitly-listed `skill_dirs`. This is the smallest change
   and composes with the existing `skill_dirs` field — the harness would set
   `skill_dirs: [<temp project>/.forge/skills]` plus the flag and get exactly
   the skills it staged. Settable per process via `extension_config_set`.

2. **Make `extension_set_enabled` actually take effect** for `tool.skill`, so
   disabling it removes the skill tools from the turn. Coarser (it removes the
   candidate skill too), but a correct no-skill baseline is precisely that, and
   it fixes a mechanism that currently lies about having worked.

A per-conversation parameter would work too, but the process-scoped config is
sufficient: the harness spawns one `forge3 stdio` process per run.

## Until then

`scripts/forge_client.py` records `skills_loaded` for every run, and
`contaminating_skills()` flags any run that opened a skill it shouldn't have.
The grader writes `contamination.json`, and `scripts/eval_report.py` surfaces it
as a warning on the report. Isolation is **detected, not prevented** — every
eval report has to carry that caveat.

`run_prompt(..., isolate_global_skills=True)` already sends the
`extension_set_enabled` frame, so if fix (2) lands the harness gets real
isolation with no further change.
