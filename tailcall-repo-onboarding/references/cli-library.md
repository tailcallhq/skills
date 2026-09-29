# CLI tool or library

**Smoke check**: the project's test command (`cargo test`, `pytest`, `go test ./...`,
`npm test`), scoped if the suite is slow. For a CLI, also run one real
invocation on harmless input (e.g. `--help` plus one command reading stdin or a
temp file) — `--version` alone isn't evidence.

**Python**: prefer the declared tool (`uv`, `poetry`, `pdm`, `hatch`). Create a
virtualenv inside the project (`.venv`) only with approval; never
`pip install` into the system/global interpreter.

**Rust**: respect `rust-toolchain.toml`; `cargo` will fetch that toolchain —
say so before it downloads. Honour `default-members` rather than forcing
`--workspace`.

**Go**: `go.work` means multi-module; run from the right module.

Offline? Try the offline flag (`cargo test --offline`, `npm ci --offline`) and
report *blocked: network* if the cache is empty.
