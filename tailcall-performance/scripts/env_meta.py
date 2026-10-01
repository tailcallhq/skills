#!/usr/bin/env python3
"""Print environment metadata for a benchmark as JSON (read-only, stdlib only).
Usage: env_meta.py [--cwd DIR] [tool ...]   e.g. env_meta.py node cargo python3
"""
import json, os, platform, shutil, subprocess, sys


def run(cmd, cwd=None):
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return None


def main():
    args = sys.argv[1:]
    cwd = None
    if args[:1] == ["--cwd"]:
        cwd, args = args[1], args[2:]
    meta = {"platform": platform.platform(), "machine": platform.machine(),
            "python": platform.python_version(), "cpu_count": os.cpu_count()}
    if hasattr(os, "getloadavg"):
        meta["loadavg"] = os.getloadavg()
    if shutil.which("git"):
        meta["git_commit"] = run(["git", "rev-parse", "HEAD"], cwd)
        st = run(["git", "status", "--porcelain"], cwd)
        meta["git_dirty"] = bool(st) if st is not None else None
    tools = {}
    for t in args or ["node", "cargo", "rustc", "go", "python3", "java"]:
        if shutil.which(t):
            out = run([t, "--version"]) or run([t, "version"]) or ""
            tools[t] = out.splitlines()[0] if out else "present"
    meta["tools"] = tools
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
