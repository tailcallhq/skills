# Monorepo / workspace

1. Identify the workspace tool (pnpm/yarn/npm workspaces, Nx, Turborepo, Cargo
   workspace, Go work, Gradle multi-project, Bazel).
2. Identify the **target package**: from the user's words, the current
   directory, or ask. Don't set up every package.
3. Read guidance at each level from repo root down to the package: root
   `AGENTS.md`, then nested ones. The nearer file wins for that package; if they
   contradict each other, report both and ask — do not edit either to "fix" it.
4. Install at the level the tool expects (usually root, with the filtered
   command e.g. `pnpm install --filter <pkg>...`, `cargo build -p <crate>`).
5. Smoke-check only the target package (`pnpm --filter <pkg> test`,
   `nx test <pkg>`, `cargo test -p <crate>`).
6. Any guidance edit goes in the narrowest file that owns the fact.
