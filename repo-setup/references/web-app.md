# Web frontend / full-stack app

**Package manager** comes from the lockfile, not habit:
`pnpm-lock.yaml` → pnpm, `yarn.lock` → yarn, `bun.lockb`/`bun.lock` → bun,
`package-lock.json` → npm. Check `packageManager` field and `.nvmrc`/`engines`
for versions. A mismatch with installed Node is a finding.

**Smoke check**: `typecheck`/`lint` + unit tests (`vitest`, `jest`) are the
usual cheap first check. Starting the dev server and fetching `/` over HTTP
proves it serves; it does not prove the UI renders.

**Browser/e2e** (Playwright, Cypress): need browsers installed and often a
display. Don't download browsers without approval; if unavailable report e2e as
*not verified* (blocker), and hand visual checks to the user.

Don't add formatters, lint configs or husky hooks as part of setup.
