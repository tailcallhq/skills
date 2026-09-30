# Review checklist (prompts, not a script)

Use this as a generic prompt for review coverage. Repository-local instructions, architecture docs, README files, contributing guides, CI configuration, and existing nearby code patterns take precedence over this checklist.

## 1. Compatibility & Integration

- [ ] **Public contracts**: Are public APIs, schemas, protocols, CLI flags, config keys, events, or persisted data formats changed? Are consumers updated?
- [ ] **Backward compatibility**: Can existing users, data, configs, saved state, and integrations continue to work?
- [ ] **Migrations and rollout**: Are schema/data/config migrations safe, reversible where needed, and compatible with deployment order?
- [ ] **Dependency changes**: Do new or changed dependencies affect licensing, supply chain risk, binary size, startup time, platform support, or transitive behavior?
- [ ] **Cross-module integration**: Are all call sites, entry points, tests, docs, generated clients, and build/config references updated?
- [ ] **Serialization/deserialization**: Do added fields, variants, or protocol changes handle older inputs and unknown values as the project expects?

## 2. Repository Convention Adherence

- [ ] **Local guidance**: Did you read and apply available `AGENTS.md`, `ARCHITECTURE.md`, `README.md`, contributing/style/testing docs, and package/module docs?
- [ ] **Existing patterns**: Does the change match nearby code for naming, structure, error handling, logging, dependency injection, tests, and public API shape?
- [ ] **Error handling**: Are errors represented, propagated, logged, and surfaced consistently with the repository's conventions?
- [ ] **Testing style**: Do tests follow existing organization, naming, fixture style, assertion style, and determinism requirements?
- [ ] **Build and lint workflow**: Were the repository's documented or CI-equivalent format, lint, typecheck, and test commands used where appropriate?
- [ ] **Documentation expectations**: Are user-facing behavior changes reflected where the repository normally documents them, without adding unnecessary docs?

## 3. Security & Safety

- [ ] **Secrets exposure**: Are tokens, credentials, private paths, PII, or sensitive payloads leaked in logs, errors, telemetry, UI, generated files, or test snapshots?
- [ ] **Injection risks**: Are user-controlled values passed to shells, interpreters, templates, SQL/queries, paths, URLs, headers, or commands safely?
- [ ] **Auth and authorization**: Are permission checks, identity boundaries, token scopes, session handling, and access controls preserved?
- [ ] **Input validation**: Are untrusted inputs validated for type, size, range, encoding, path traversal, and malformed/hostile values?
- [ ] **Sensitive persistence**: Are secrets and sensitive data stored, cached, serialized, and deleted according to project expectations?
- [ ] **Resource limits**: Are timeouts, retries, concurrency, memory, file descriptors, child processes, and network calls bounded and cleaned up?
- [ ] **Unsafe or privileged operations**: Are elevated privileges, unsafe code, filesystem writes/deletes, network access, and external processes justified and constrained?

## 4. Functionality & Correctness

- [ ] **Requirement trace**: Does the changed code satisfy the PR description, linked issue, and user-visible behavior promised?
- [ ] **Actual side effect**: Does the new logic modify the state or output used by the real feature, not just display, logging, tracking, metadata, or a stale copy?
- [ ] **Source precedence**: When multiple sources exist (arguments, config, environment, defaults, persisted state, remote values), is the winning value correct in every changed path?
- [ ] **Call-site coverage**: Do all relevant entry points and callers use the new behavior?
- [ ] **Stale state**: Can cached, cloned, memoized, or previously captured values survive after updates that should invalidate them?
- [ ] **Branch behavior**: Are success/error, present/absent, empty/non-empty, fallback, retry, cancellation, and cleanup paths correct and observable?
- [ ] **Async/concurrency**: Are ordering assumptions, races, locks, cancellation, retries, and shared state handled correctly?
- [ ] **Edge cases**: Empty values, zero values, boundaries, large inputs, duplicates, collisions, encoding, timezone/clock behavior, and platform differences.
- [ ] **Error propagation**: Are failures surfaced to the right caller/user without being swallowed, duplicated, or transformed misleadingly?
- [ ] **Idempotency**: Can operations be retried or repeated safely when the feature requires it?
- [ ] **Performance**: Are expensive operations, allocations, I/O, network calls, and repeated computations justified or cached appropriately?
- [ ] **Type/data safety**: Are conversions, parsing, numeric operations, nullability/optionality, and invariants handled safely?

## 5. Not in scope

Formatting, naming preferences, and style opinions not required by the repo's
own linters or docs. Mention them only if the user asked for a style pass.
