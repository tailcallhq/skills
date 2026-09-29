#!/usr/bin/env python3
"""Value-free inspection of Forge agent config files.

Usage: inspect_config.py FILE [FILE ...] [--kind hooks|mcp|auto]

Prints parse errors (line/column), structure, key names and schema problems
for hooks.json and mcp.json-style files. It never prints configuration
values: env/header values, tokens, URLs and hook command strings are reduced
to "<set>" or a length, so the output is safe to paste into a conversation.
Read-only. Exit code 0 = no problems, 1 = problems found, 2 = usage error.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HOOK_EVENTS = {"conversation_start", "conversation_end"}
HOOK_ENTRY_KEYS = {"id", "name", "matcher", "enabled", "type", "command", "args", "timeout"}
MCP_STDIO_KEYS = {"command", "args", "env", "timeout", "disable", "type", "cwd"}
MCP_HTTP_KEYS = {"url", "serverUrl", "auth", "oauth", "headers", "timeout", "disable", "type"}


def safe_name(name: object) -> str:
    """Render an untrusted key for display; flag suspicious characters."""
    s = str(name)
    if any(c in s for c in "\"'\\{}[]$`;|&<>\n\r\t") or len(s) > 80:
        return f"<suspicious name, {len(s)} chars>"
    return s


def load(path: Path) -> tuple[object | None, list[str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, ["file does not exist (treated as empty by Forge)"]
    except OSError as e:
        return None, [f"unreadable: {e.__class__.__name__}"]
    if not text.strip():
        return None, ["file is empty (invalid JSON)"]
    try:
        return json.loads(text), []
    except json.JSONDecodeError as e:
        return None, [f"JSON parse error at line {e.lineno}, column {e.colno}: {e.msg}"]


def check_hooks(doc: object) -> tuple[list[str], list[str]]:
    info, problems = [], []
    if not isinstance(doc, dict):
        return info, ["root must be an object keyed by event name"]
    for event, entries in doc.items():
        ev = safe_name(event)
        if event not in HOOK_EVENTS:
            problems.append(
                f"unknown event '{ev}' — the whole file fails to parse, so NO hooks run "
                f"(supported: {', '.join(sorted(HOOK_EVENTS))})"
            )
            continue
        if not isinstance(entries, list):
            problems.append(f"{ev}: must be a list of hook entries")
            continue
        for i, e in enumerate(entries):
            where = f"{ev}[{i}]"
            if not isinstance(e, dict):
                problems.append(f"{where}: entry must be an object")
                continue
            name = safe_name(e.get("name", "<missing>"))
            for k in e:
                if k not in HOOK_ENTRY_KEYS:
                    problems.append(f"{where} ({name}): unknown key '{safe_name(k)}' — whole file fails to parse")
            if "name" not in e:
                problems.append(f"{where}: missing required 'name' — whole file fails to parse")
            t = e.get("type")
            if t is None:
                problems.append(f"{where} ({name}): missing 'type' (must be \"command\") — whole file fails to parse")
            elif t != "command":
                problems.append(f"{where} ({name}): unknown type '{safe_name(t)}' — whole file fails to parse")
            cmd = e.get("command")
            if not isinstance(cmd, str) or not cmd.strip():
                problems.append(f"{where} ({name}): missing/empty 'command'")
            if "timeout" in e and not (isinstance(e["timeout"], int) and e["timeout"] > 0):
                problems.append(f"{where} ({name}): 'timeout' must be a positive integer (seconds)")
            if "enabled" in e and not isinstance(e["enabled"], bool):
                problems.append(f"{where} ({name}): 'enabled' must be true/false")
            if "args" in e and not (isinstance(e["args"], list) and all(isinstance(a, str) for a in e["args"])):
                problems.append(f"{where} ({name}): 'args' must be a list of strings")
            info.append(
                f"{where}: name={name} matcher={safe_name(e.get('matcher', '*'))} "
                f"enabled={e.get('enabled', True)} timeout={e.get('timeout', 60)}s "
                f"command=<{len(cmd) if isinstance(cmd, str) else 0} chars>"
            )
    return info, problems


def check_mcp(doc: object) -> tuple[list[str], list[str]]:
    info, problems = [], []
    if not isinstance(doc, dict):
        return info, ["root must be an object"]
    servers = doc.get("mcpServers", doc.get("servers"))
    if servers is None:
        return info, ["no 'mcpServers' key — no servers declared"]
    if not isinstance(servers, dict):
        return info, ["'mcpServers' must be an object keyed by alias"]
    for alias, cfg in servers.items():
        a = safe_name(alias)
        if not isinstance(cfg, dict):
            problems.append(f"{a}: server config must be an object")
            continue
        has_cmd, has_url = "command" in cfg, ("url" in cfg or "serverUrl" in cfg)
        if has_cmd == has_url:
            problems.append(f"{a}: needs exactly one of 'command' (stdio) or 'url' (http)")
        allowed = MCP_STDIO_KEYS if has_cmd else MCP_HTTP_KEYS
        for k in cfg:
            if k not in allowed:
                info.append(f"{a}: note: key '{safe_name(k)}' is not a Forge field for this transport and is ignored")
        parts = [f"transport={'stdio' if has_cmd else 'http' if has_url else '?'}"]
        if has_cmd:
            c = cfg.get("command")
            parts.append(f"command={safe_name(c) if isinstance(c, str) and '/' not in c and ' ' not in c else '<path or complex>'}")
            parts.append(f"args={len(cfg.get('args') or [])}")
        if isinstance(cfg.get("env"), dict):
            parts.append("env_keys=[" + ", ".join(safe_name(k) + "=<set>" for k in cfg["env"]) + "]")
        if isinstance(cfg.get("headers"), dict):
            parts.append("header_keys=[" + ", ".join(safe_name(k) + "=<set>" for k in cfg["headers"]) + "]")
        if "auth" in cfg or "oauth" in cfg:
            parts.append("auth=<configured>")
        parts.append(f"disable={cfg.get('disable', False)}")
        info.append(f"{a}: " + " ".join(parts))
    return info, problems


def main(argv: list[str]) -> int:
    kind = "auto"
    files: list[str] = []
    it = iter(argv)
    for a in it:
        if a == "--kind":
            kind = next(it, "auto")
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            files.append(a)
    if not files:
        print(__doc__, file=sys.stderr)
        return 2
    bad = False
    for f in files:
        p = Path(f).expanduser()
        print(f"== {p}")
        doc, errs = load(p)
        for e in errs:
            print(f"  PROBLEM: {e}")
        if errs:
            bad = bad or "does not exist" not in errs[0]
            continue
        k = kind
        if k == "auto":
            k = "hooks" if p.name.startswith("hooks") or (isinstance(doc, dict) and set(doc) & HOOK_EVENTS) else "mcp"
        info, problems = (check_hooks if k == "hooks" else check_mcp)(doc)
        print(f"  kind: {k}")
        for line in info:
            print(f"  {line}")
        for line in problems:
            print(f"  PROBLEM: {line}")
        if not problems:
            print("  OK: parses and matches the expected shape")
        bad = bad or bool(problems)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
