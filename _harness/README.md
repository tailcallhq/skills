# Shared compatibility & evaluation harness

Enabling infrastructure for skills in this repository. **Not a skill**: the
directory has no `SKILL.md`, and the loader is verified to ignore it
(`dist:non_skill_dir_ignored`). It complements, and does not replace,
`forge-skills`'s eval loop: use these fixtures and helpers as the
disposable sandboxes for its with-skill/baseline runs.

```bash
python3 _harness/run.py --out /tmp/evidence.json             # no model tokens, no mutations
python3 _harness/run.py --only changes distribution           # subset
python3 _harness/run.py --prompt-smoke --model M --provider P # optional: one real turn (spends tokens)
```

Exit code is non-zero only on `fail`. `blocked` means a capability is absent
or unverified; it is reported and **never counted as a pass**. Requires Python 3.10+, git,
and `forge3` on `PATH` (or `FORGE_BIN`). Node ≥ 22.6 and cargo are needed for
their fixtures; without them those scenarios report `blocked`.

## What runs

| Area | Scenarios |
|---|---|
| Runtime inventory | `rpc.discover`, `info`, `extension_list`, `tool_list`, `skill_list`, `command_list` against the *installed* binary; classifies callable tools vs SDK-only methods vs registered services vs gates |
| Positive fixtures | `ts-web` (TS HTTP API + page; `node:test`; launch with free port, readiness poll, POST, process-group reap), `py-cli` (unittest + stdin run), `rust-lib` (`cargo test --offline`) |
| Negative fixtures | `gui-desktop`, `android-app`, `container-svc` → `blocked` unless a driver is present *and* exercised |
| Helpers | explicit-cwd enforcement, timeout kills grandchildren, redaction of synthetic secrets |
| Changed files | initial commit (no HEAD), dirty index (staged/unstaged/untracked), no upstream + non-main default branch, explicit base, fallback base, monorepo sub-scope, not-a-repo |
| Distribution | lint (dir id == `name`, required fields, one-or-two-word id, Claude-only keys flagged as unenforced, mutating dynamic markers, missing resources); clone `--depth 1` from a local bare repo into a disposable `<fixture>/.forge/skills`; `skill_view`/`skill_search` via `tool_call`; resources; workspace shadowing of global copies; ff-only update; reload in a live session; diverged update refused with local edit preserved; baseline contamination count |
| Guards | user-global `~/.forge/tailcall-skills` and this repo's HEAD/status unchanged; temp root removed |

Evidence JSON records platform, binary/version, model/provider (or "none"),
per-scenario timings, skills visible to baselines, and (with `--prompt-smoke`)
skills loaded, tool calls and whether the fixture was mutated. All output
passes through `lib/redact.py`; child processes get `safe_env()` (secret-like
variables removed).

## Observed on forge3 0.21.0 (2026-09-29, Linux)

- 87 RPC methods, 141 `ExtensionRequest` variants, 71 enabled extensions, 49 tools, 34 commands.
- **Skill id is the directory name**, not `name:` (`id_is_directory_not_name`). A `SKILL.md` without `description` is skipped silently.
- Workspace skills under `<cwd>/.forge/skills` shadow global skills of the same id. Only the cwd is a root; ancestor directories are not scanned (SDK TODO in `tool-skill/src/state.rs`).
- **Reload requires the streaming call.** `command_execute` (non-stream) for `skill.reload` returns only the first "reloading" frame and the reload **does not happen**; `command_execute/xstream` completes it. Clients/hosts that call the plain method will see stale skills.
- Skills are cached per session: a skill added on disk is invisible until reload.
- `skills.install` is hard-wired to `~/.forge/tailcall-skills` and the public URL (`with_dest` exists only in Rust). The harness therefore **reproduces its git semantics** (`clone --depth 1`, `pull --ff-only`) against a local repo instead of invoking the command, which would mutate the user's real installation.
- **Baseline contamination:** 16 global skills are visible from an empty cwd. `skill_dirs` config only appends to defaults. `extension_set_enabled(tool.skill,false)` *did* take effect within the same stdio session on 0.21.0 (the tool disappeared from `tool_list`), unlike the 0.19.0 note in `forge_client.py`. It was not adopted for isolation because persistence across processes is unverified and it would hide the candidate skill as well. Contamination remains detected, not prevented.
- Frontmatter parsed: `name`, `description`, `icon` only.

## Unsupported / gated (report as blockers)

| Gate | Kind | Status here | Fallback |
|---|---|---|---|
| `workflow` tool | platform-gated (not on musl / Windows-ARM builds) | present | sequential `task` sub-agents, or inline sequential steps; require explicit opt-in either way |
| `sem_search` | auth-gated (gateway sign-in) | listed, unverified | `search` (ripgrep) + `read` |
| relay | transport-gated (`forge3 relay` only) | absent | none: local transports only |
| browser automation | external | absent | HTTP-level checks (curl/urllib) + hand-off to the user for visual checks |
| mobile driver (adb/xcrun) | external | absent | build/unit tests only; device checks handed to the user |
| container runtime | external | absent | run the service natively if the project supports it; otherwise blocker |
| GUI display | external | absent | headless tests only; blocker for UI verification |
| terminal (PTY), routines, secrets, MCP admin | SDK-only methods, no agent tool | n/a | agent `shell` for non-interactive commands; hand off to UI |

## Rules for skills that build on this

- Probe with `lib/runtime.inventory()` + `require()`; never infer support from SDK source or a different build.
- Always pass explicit cwd; bound every wait; use `proc.background()` so servers are reaped.
- Use `lib/changes.discover()` for review/diff scope; it never fetches or mutates.
- Do not put installation, publishing or other mutations in dynamic `!` markers; lint flags obvious cases.
- No secrets in fixtures; synthetic values only.

## Provenance

Original code written for this repository. No material was copied from
Piebald-AI/claude-code-system-prompts or other third-party prompt extractions.
Protocol details derive from observing `forge3` and from `forgecode-sdk` at
`36ca5e509926d8e3e6d8dc2c13519e74e8b900e1` (`svc-skills-core`, `tool-skill`).

### Prompt smoke (2026-09-29)

`--prompt-smoke --model claude-haiku-4-5-20251001 --provider claudecode`: pass in 15.9s;
9 tool calls (`read` ×6, `shell` ×3), no skills loaded, fixture not mutated, answer
correctly reported 1 passing test. Token counts are unavailable through `forge_client`.
