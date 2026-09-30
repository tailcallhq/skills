---
name: repo-setup
description: "Onboards to a repository: learns how it builds, runs and tests, applies an approved minimal setup, and proves it with a smoke check. Use to set up a repo or write AGENTS.md."
---

# Repo onboarding: first successful session

The goal is not "files written". The goal is that the user can build, run or
test this project **now**, and knows exactly what is still missing. The session
ends with a verdict — `pass`, `fail` or `blocked` — backed by the command you
ran and its output. Writing guidance files without running anything is not
success.

Setup touches shared state (the team's instructions, the working tree, maybe
installed tools), so the shape is: **look → ask little → propose → apply what
was approved → verify → report.**

## 1. Look before asking

Read, don't guess. Everything the repository can answer, it should answer.

1. **Where am I?** `git status --short --branch`, `git worktree list`,
   repository root vs. current directory. A dirty tree is the user's work:
   note it, never stash, reset or commit it.
2. **Existing guidance first**: `AGENTS.md` (root *and* nested), `README*`,
   `CONTRIBUTING*`, `CLAUDE.md`, `.cursor/rules`, `.github/copilot-instructions.md`,
   and project-local skills in `.forge/skills/`, `.agents/skills/`, `.claude/skills/`.
   Existing instructions are authoritative over your inferences; where they
   conflict with what the code shows (e.g. README says `npm`, lockfile is
   `pnpm-lock.yaml`), report the conflict — don't silently pick one or rewrite it.
3. **Manifests, lockfiles, runtimes**: `package.json` + lockfile, `Cargo.toml`,
   `pyproject.toml`/`requirements*.txt`/`uv.lock`, `go.mod`, `pom.xml`/`build.gradle*`,
   `Gemfile`, `*.csproj`, `Makefile`/`justfile`/`Taskfile.yml`, version pins
   (`.nvmrc`, `.tool-versions`, `rust-toolchain*`, `.python-version`).
4. **CI config** (`.github/workflows/`, `.gitlab-ci.yml`, …): the most reliable
   record of the real build/test commands and required services.
5. **Workspace layout**: monorepo markers (`workspaces`, `pnpm-workspace.yaml`,
   `[workspace]` in Cargo, `go.work`, `nx.json`, `turbo.json`). In a monorepo,
   scope everything to the package the user cares about; ask which if unclear.
6. **Prerequisites actually present**: check each needed tool with
   `command -v <tool>` and `<tool> --version`. Check whether required
   env vars / credential files **exist** (`test -n "${VAR+x}"`, `test -f .env`)
   — never print their values, never `cat .env`, never echo tokens.

Use `search`/`read` (and `sem_search` if it works — it may be auth-gated; fall
back to literal search). Use `skill_search` to see whether a project-specific
skill already covers running or testing this repo; if so, use it rather than
re-deriving its steps.

Then pick the software type and read **only** the matching reference:

| Evidence | Reference |
|---|---|
| HTTP API / backend service | `references/api-service.md` |
| Web frontend / full-stack app | `references/web-app.md` |
| CLI tool or library | `references/cli-library.md` |
| Workspace with several packages/crates | `references/monorepo.md` (plus the type of the target package) |
| Desktop GUI, mobile, container-only, cloud-deployed | `references/external-capabilities.md` |

## 2. Ask only what the repo can't answer

Typical real gaps: which package in a monorepo, which of two conflicting
instructions is current, whether a missing tool may be installed, where a
required credential comes from. Batch them into one short message. Zero
questions is a fine outcome. Do not ask about things already in README, CI or
manifests.

## 3. Propose a minimal, reviewable plan

Present it before changing anything:

```
Setup plan for <repo> (<package>, <stack>)
Found: <1–3 lines: toolchain, commands from CI/README, existing guidance>
Conflicts/gaps: <or "none">

Proposed actions (nothing done yet):
1. [run]     <install deps, e.g. `pnpm install --frozen-lockfile`>
2. [edit]    <file> — <one-line why>          (only if needed)
3. [smoke]   <first real check, e.g. `pnpm --filter api test`>
Needs you:   <missing tool/credential/decision, or "nothing">
Not doing:   <things deliberately out of scope>
```

Rules for the plan:
- **Smallest thing that gets a first green check.** Dependency installation that
  follows the lockfile is usually the only mutation needed.
- **Edits are optional, not the deliverable.** Propose writing/updating
  `AGENTS.md` only if the user asked for agent guidance, or if you found a
  non-obvious fact that would cause mistakes next session (a non-standard test
  command, a required service, a CI-only check). Each line must pass: "would
  removing this cause an agent to get it wrong?"
- **When the user explicitly asked for agent guidance, that request is the
  approval for non-conflicting additions**: apply them (append/insert, keep every
  existing line) and show the diff. Only edits that pick a side in a conflict,
  delete existing lines, or touch personal/team boundaries wait for an answer —
  otherwise the user asked for guidance and got none.
- **Existing `AGENTS.md` is someone's work.** Add with a reason per line;
  never regenerate it wholesale or silently delete a line you believe is stale. Nested `AGENTS.md` in
  subpackages stay where they are. Personal preferences (tone, role, local
  paths) do not belong in a team-shared file — ask where the user wants them.
- **Out of scope unless explicitly requested**: new docs/READMEs, git hooks,
  CI changes, credentials or `.env` files with values, project boards, clones
  of other repos, global config (`~/.forge`, shell rc files), commits.
  Forge exposes conversation-level hooks, not per-edit hooks; don't promise
  "format on every edit" automation.

Wait for approval. If the user only asked a question ("how do I run this?"),
answer it and offer the plan — don't execute.

## 4. Apply only what was approved

- Run installs with the lockfile-respecting command (`npm ci`, `pnpm install
  --frozen-lockfile`, `cargo fetch`, `uv sync --frozen`, …). If it wants to
  rewrite the lockfile, stop and say so.
- Installing system tools or runtimes needs its own explicit yes; otherwise
  it's a blocker for the report. When suggesting how to get a tool, don't assume
  the OS: detect it (`uname`, `$OS`) or give the official installer plus the
  common options (a version manager such as mise/asdf/nvm/rustup, Homebrew,
  the Linux distro package, winget/scoop on Windows).
- Keep lockfiles as they are. If no lockfile exists, don't create one as a side
  effect of setup; report it (e.g. "CI runs `npm ci` but no lockfile is committed").
- In a monorepo, run checks only for the target package; skip unrelated packages.
- Every command gets an explicit working directory and a timeout. Long-running
  servers are started in the background, polled for readiness with a bounded
  wait, then stopped — don't leave processes running.
- Before touching a file that is already modified in the working tree, say so.

## 5. First real smoke check

Run the cheapest command that proves the project works in this environment,
preferring what CI runs: one test target, a type check + build, or start the
service and hit one endpoint. A smoke check that only prints `--version` is not
enough. Capture the exit code and the few output lines that prove it.

Verdict:
- **pass** — the check ran and succeeded.
- **fail** — it ran and failed for a project reason (test failure, compile
  error). Show the first real error; diagnose briefly; don't start a refactor.
- **blocked** — it could not run: missing tool, credential, service (DB,
  Docker), network, display/device. Name the exact prerequisite and how to get it.

If there's a project-local run/test skill, use it for this step.

## 6. Report

```
Result: PASS | FAIL | BLOCKED — <one line>
Evidence: `<command>` (cwd <dir>) → exit <n>
  <2–5 relevant output lines, secrets redacted>
Changed: <files edited / deps installed, or "nothing">
Commands for next time:
  install: …   test: …   run: …   lint: …
Blockers / next steps: <concrete, ordered>
```

Say what you did *not* verify (e.g. "e2e tests need a browser, not run").

## Re-running on a set-up repo

Setup must be idempotent. On a second run, rediscover the state, skip steps
that are already satisfied (deps installed, guidance present and accurate), and
go straight to the smoke check. Report "no changes needed" rather than
rewriting files to look productive.
