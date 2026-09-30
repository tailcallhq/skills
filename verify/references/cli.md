# Adapter: CLI / TUI

The handle is the `shell` tool. The evidence is exit code + stdout + stderr.

## Running

- Invoke the tool the way a user would (installed entrypoint,
  `python -m pkg`, `cargo run --`, `node bin/x`), from an explicit cwd,
  inside a temp directory for any files it reads or writes.
- Record all three streams. Example:
  `printf 'a b\nc\n' | python -m wclite.cli; echo "exit=$?"` (on Windows
  PowerShell use `$LASTEXITCODE`).
- Bound long commands with a timeout; never leave an interactive program
  waiting on stdin — pipe input or close stdin (`</dev/null`).

## Cases

- Each changed flag/subcommand with representative input.
- Input sources: file arg, stdin, empty input, missing file, unreadable file,
  unicode, no trailing newline, large input.
- Usage errors: unknown flag, missing arg → non-zero exit, message on
  stderr, nothing half-written.
- `--help`/`--version` still work if touched.
- Output contract: machine-readable output stays parseable; nothing extra on
  stdout that scripts would choke on.

## TUI / interactive programs

Forge's agent `shell` is non-interactive (no PTY). Prefer a non-interactive
mode or scripted input. For true TUI interaction you need an external PTY
driver the project or user already has (`expect`, `pexpect`, `tmux
send-keys` + `capture-pane`). If none is available, mark the interactive
checks BLOCKED and hand the steps to the user; don't install one globally.
A captured screen is evidence of rendering, not of correct behavior — pair it
with state you can assert (files written, exit code).
