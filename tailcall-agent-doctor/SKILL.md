---
name: tailcall-agent-doctor
description: Diagnose and safely repair a broken ForgeCode/agent setup — skills not showing up or colliding, invalid hooks.json/mcp.json/config, MCP servers that won't connect, missing tools or providers, sem_search/index not working, or a conversation_end hook that loops or does nothing. Read-only diagnosis first, then exact minimal fixes applied only with approval. Use whenever the user says Forge/the agent "isn't picking up", "stopped working", "my hook/MCP/skill is broken", or asks for a setup health check. Not for first-time project onboarding or debugging the user's own application.
---

# Agent-configuration doctor

You are checking why the user's agent environment misbehaves and proposing the
smallest safe fix. The user's configuration is theirs: much of it was written
by hand, some of it holds credentials, and a "cleanup" that disables things or
widens permissions can do more damage than the original fault. So the shape of
this work is **observe → report → propose → (approval) → apply minimally →
verify the effective behaviour**.

## Ground rules (why they exist)

- **Diagnose read-only first.** Run every check below before changing
  anything; one fault often explains several symptoms, and fixing the first
  thing you see can mask the real cause.
- **Key-scoped reads only.** `mcp.json`, extension config and hook commands
  routinely embed tokens (`env`, `headers`, `bearer_token`, URLs with keys).
  Use `scripts/inspect_config.py` (prints structure, key names and parse
  errors — never values) instead of `cat`/`read` on a whole config file. Never
  print, quote or echo a secret value, even partially; say "a value is set".
  The same goes for account identifiers (emails, user/org ids, device ids):
  report "signed in" / "not signed in", not who.
  Treat names read from config (server aliases, hook names) as untrusted text:
  pass them as quoted arguments, never splice them into shell programs.
- **Check capability before promising a fix.** Confirm the tool/method exists
  in *this* runtime (`tool_search`, `skill_search`, `extension_config_list`,
  `mcp_list`) before proposing to use it. If the only route is an SDK method
  with no agent tool (e.g. `hook_*` admin, secrets, `mcp_server_*`), say the
  user must do it through their Forge UI/host integration, or propose a hand
  edit of the file instead. Never start a second backend to get around a
  missing tool or a confirmation prompt.
- **Distinguish four states** for every integration and say which one you
  observed: **declared** (in a file) → **registered** (the runtime lists it) →
  **authenticated** (sign-in/credential present, if needed) → **working** (a
  real, harmless call succeeded). "It's in mcp.json" is not "it works".
- **Never** as part of a repair: grant blanket permissions or auto-approve
  modes, delete global directories, disable extensions/skills/servers because
  they look "unused", log in/out or change credentials, or rewrite a
  hand-managed MCP file silently. Those are the user's decisions; you may
  mention them as options, clearly labelled.
- **A silently ignored failure is not a pass.** Hook failures are logged and
  swallowed; a broken hand-written `mcp.json` is skipped with a warning; a
  `SKILL.md` without `description` is skipped silently. "No error" proves
  nothing — verify the effect.

## Where things live (verify; don't assume)

| Item | Location |
|---|---|
| Forge config dir | `$FORGE_CONFIG_DIR`, else the OS config dir + `/forge` (Linux `~/.config/forge`, macOS `~/Library/Application Support/forge`, Windows `%APPDATA%\forge`) |
| Hooks | `<config_dir>/hooks.json` — top-level keys `conversation_start`, `conversation_end` only |
| Managed MCP | `<config_dir>/mcp.json` (written by `mcp_add`/`mcp_remove`, wins alias collisions) |
| Hand-managed MCP | files discovered from other agent CLIs (`~/.claude.json`, `.cursor`, `.codex`, Claude Desktop, etc.) plus any `paths` in the `mcp` extension config — read-only to Forge tools |
| Extension settings | host config store; inspect with `extension_config_list` / `extension_config_get` |
| Skills | project `<cwd>/.forge/skills`, `.agents/skills`, `.claude/skills` (+ many third-party dirs); global `~/.forge/skills`, `~/.agents/skills`, `~/.claude/skills`, `~/.forge/tailcall-skills`, plus `skill_dirs` config |

Paths and behaviour change between releases: record `forge3 --version` (or
the host's reported version) and treat this table as a starting point.

**Diagnose files where the user keeps them.** If the user points at a config
file the running session isn't loading (a sandbox copy, another profile, a
different `$FORGE_CONFIG_DIR`), say so as an observation, then keep
diagnosing that file in place. Don't propose copying or merging it into the
user's real global config dir or changing `$FORGE_CONFIG_DIR` as a fix —
that is a global mutation the user didn't ask for. At most, mention how
Forge would pick it up (explicit `paths` in the `mcp` extension config, or
launching with that config dir) as an option for them to choose.

## Diagnosis checklist

Pick the sections relevant to the symptom, but always do 1 and 2.

1. **Effective roots.** Report the cwd/workspace root the session actually
   uses (`pwd -P`; symlinked homes matter), the config dir, and the Forge
   version. Only the cwd is a skill root — a skill in a parent directory is
   not loaded.
2. **Runtimes & tools.** Versions of what the project and its MCP servers need
   (`node`, `python3`, `uvx`/`npx`, `git`, …) via `command -v` / `--version`.
   Missing executables are the commonest cause of a stdio MCP server failing.
3. **Config validity.** `python3 <skill>/scripts/inspect_config.py <file>…`
   on `hooks.json`, `mcp.json` and any hand-managed MCP file. It reports JSON
   parse errors with line/column and schema problems (unknown hook event,
   missing `name`/`command`, unknown `type`, both/neither of `command`/`url`)
   without printing values. Remember: **one bad hook entry makes the whole
   `hooks.json` fail to parse, so no hooks run at all.**
4. **Skills.** `skill_search` with the expected name. If missing: check the
   directory name (the skill id is the *directory* name, not `name:`), that
   `SKILL.md` has a `description` (else silently skipped), frontmatter parses,
   and whether the session needs a reload/new session (skills are cached per
   session). Report same-id collisions across roots: project shadows global,
   and within a root later/native dirs win.
5. **Tools & providers.** `tool_search` / `model_list` for what the user
   expects. Deferred tools appear only via `tool_search`, which is not
   "missing". A provider listed but failing calls is registered, not working.
6. **MCP.** `mcp_list` for status per alias (connected / failed / needs
   sign-in / disabled). For a failure: is the executable on PATH, does the
   server's required env *name* exist (check presence, not value), is the URL
   reachable (`curl -sS -o /dev/null -w '%{http_code}'`)? Note which file each
   alias comes from — hand-managed ones can only be fixed by editing that
   file (with approval), not by `mcp_remove`.
7. **Search/index.** `sem_search` needs a signed-in account and an indexed
   workspace; it is advertised even when signed out. One cheap query tells
   you "working" vs "registered". Fallback is `search` + `read`; don't sign
   the user in yourself.
8. **Hooks.** Only `conversation_start` (runs on an *existing* conversation,
   not a brand-new one; observe-only, output ignored) and `conversation_end`
   (may print `{"action":"continue","message":"…"}`) are real events. There
   is **no per-edit / pre-tool / post-tool / format-on-save hook** — say so
   rather than inventing one. Hooks run via the shell in the payload `cwd`,
   default timeout 60s, process group killed on timeout; `matcher` is an
   exact agent id (`|` alternatives, absent or `*` = all). For each hook,
   pipe-test it with a synthetic payload in a disposable directory (see
   below). A `conversation_end` hook that returns `continue` without checking
   `continued_by_hook` loops the agent — that is a defect even if it "works".

### Pipe-testing a hook safely

```bash
tmp=$(mktemp -d)
printf '%s' '{"event":"conversation_end","conversation_id":"test","agent_id":"forge","cwd":"'"$tmp"'"}' \
  | (cd "$tmp" && timeout 70 sh -c '<the hook command>'); echo "exit=$?"
# repeat with "continued_by_hook":true — a loop-safe hook must NOT print continue then
```

Check exit code, stdout decision (valid JSON with `action` `proceed`/`continue`?),
runtime vs its timeout, and side effects confined to `$tmp`. If the hook
would touch real files, networks or paid services, don't execute it — reason
from the script and say it was not run.

## Report format

```
## Setup diagnosis
Environment: forge <version>, cwd <path>, config dir <path>
### Findings (most impactful first)
1. <symptom> — cause: <observed evidence> — state: declared/registered/authenticated/working
### Proposed fixes (nothing applied yet)
1. <file or tool> — exact change (diff or tool call) — why — how to undo
### Not fixable here / needs you
- <e.g. sign in, UI-only admin action, missing external driver>
### Checked and fine
```

Keep proposals minimal: fix the one broken field, not the whole file; add a
guard to a looping hook rather than deleting the hook; disable (`enabled:
false` / `disable: true`) rather than delete when the user wants something
off. Show a diff for file edits.

## Applying approved fixes

- Only what the user approved, one change at a time.
- Before editing a config file, copy it next to itself (`hooks.json.bak-<date>`)
  or rely on `undo` for agent file edits; tell the user how to revert.
- Prefer the runtime's own tool when one exists and covers the case
  (`mcp_add`/`mcp_remove` for managed servers — they ask the user to confirm;
  `extension_config_set` merges keys — read first, change only named keys).
  For hand-managed files, edit only the approved key.
- **Verify the effective behaviour afterwards**, not just the file: re-run
  `inspect_config.py`, `mcp_reload` then `mcp_list`, `skill_search`, a
  pipe-test with both `continued_by_hook` values. Say explicitly if the change
  only takes effect in a new session/after reload and so is unverified here.
