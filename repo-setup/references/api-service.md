# API / backend service

**Find the real commands**: CI workflow steps first, then `scripts` in
`package.json`, `Makefile` targets, `[tool.*]` sections, `cargo` aliases.

**Services it depends on**: look for `docker-compose*.yml`/`compose.yaml`,
`DATABASE_URL`/`REDIS_URL` in `.env.example`, migration dirs (`migrations/`,
`prisma/`, `alembic/`, `diesel.toml`). Missing DB/Docker is a *blocker*,
not a reason to rewrite config to SQLite.

**Env**: copy `.env.example` → `.env` only with approval and only with the
example's placeholder values; never invent or fetch real secrets. Report which
required vars are unset by name only.

**Smoke check, in order of preference**:
1. The unit-test command CI uses, scoped to one package if large.
2. Start the server in the background on a free port with explicit cwd, poll a
   health/root endpoint with a bounded wait (≈30s), make one request, stop the
   process (kill the process group).
3. Build/type-check only — report as partial: "builds, not exercised".

Migrations against a real (non-local) database are never part of setup.
