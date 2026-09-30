# Adapter: library / SDK

The handle is a consumer: code that imports the **public** API the way a
downstream user would. Evidence is the assertion output of that consumer.

## Approach

- Identify the public surface (exports, `pub` items, `__all__`, package
  entrypoints, documented examples). Test through it, not through private
  helpers — a private-function test can pass while the public contract breaks.
- Run the project's tests first (`cargo test`, `pytest`/`python -m unittest`,
  `npm test`, `go test ./...`). Doc tests/examples count as consumer checks.
- For an ad-hoc probe, write a tiny consumer in a temp dir (or an
  integration-test file if the user wants a regression kept) that depends on
  the package by path. Don't leave scratch files in the repo.
- Offline: prefer `--offline`/cached deps; a failed dependency fetch is
  BLOCKED (network), not FAIL.

## Cases

- Documented examples verbatim.
- Boundaries and invariants: empty/one/many, ordering, mutation of inputs
  (does `median(&mut xs)` reorder the caller's slice? is that documented?),
  numeric overflow/precision, error type and message for invalid input.
- API compatibility: did a signature, default, or error type change in a way
  that breaks existing callers? Compile/import an old-style call.

## Regression tests

When asked to add one: put it where the project keeps tests, name it after
the behavior, and show it failing on the pre-fix code when feasible (e.g.
temporarily in a scratch worktree), then passing on the fix.
