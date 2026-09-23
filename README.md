# tailcallhq/skills

Built-in skills shipped with Forge. Forge syncs this repository into
`~/.forge/tailcall-skills` and loads every `<skill>/SKILL.md` it contains.

## Layout

One directory per skill at the repository root, each containing a `SKILL.md`
with YAML frontmatter (`name`, `description`). The directory name must match
the `name` field.

## Third-party skills

| Skill | Source | License |
|---|---|---|
| `skill-creator` | [anthropics/skills](https://github.com/anthropics/skills/tree/main/skills/skill-creator) | Apache-2.0 (see `skill-creator/LICENSE.txt`) |
