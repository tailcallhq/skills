"""Disposable representative fixtures. Offline: no package downloads.

Supported (positive) fixtures, each with a verify recipe the harness executes:
- ts-web:     TypeScript HTTP API + static page, Node >= 22.6 type stripping, node:test
- py-cli:     Python stdlib CLI, unittest
- rust-lib:   Rust library with no dependencies, cargo test --offline

Capability-negative fixtures — they declare a stack whose driver is external
and must be reported as a BLOCKER when the driver is absent, never as a pass:
- gui-desktop (needs a display), android-app (needs adb/emulator),
  container-svc (needs docker/podman).

Git-state variants for change discovery: clean, dirty (staged + unstaged +
untracked), initial (no commits), no-upstream (commits, no remote), monorepo.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

GIT_ENV = {
    "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
}


def _w(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV, "HOME": str(cwd)}  # isolate from the user's global git config
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True,
                          text=True, timeout=60).stdout


TS_WEB = {
    "package.json": '{\n  "name": "fixture-ts-web",\n  "private": true,\n  "type": "module",\n'
                    '  "scripts": {\n    "start": "node --experimental-strip-types src/server.ts",\n'
                    '    "test": "node --experimental-strip-types --test \\"test/*.test.ts\\""\n  }\n}\n',
    "src/app.ts": 'export type Todo = { id: number; title: string; done: boolean };\n'
                  'export function addTodo(list: Todo[], title: string): Todo[] {\n'
                  '  if (!title.trim()) throw new Error("title required");\n'
                  '  return [...list, { id: list.length + 1, title: title.trim(), done: false }];\n}\n',
    "src/server.ts": 'import { createServer } from "node:http";\nimport { addTodo, type Todo } from "./app.ts";\n'
                     'let todos: Todo[] = [];\nconst port = Number(process.env.PORT ?? 0);\n'
                     'const server = createServer((req, res) => {\n'
                     '  if (req.url === "/health") { res.writeHead(200); res.end("ok"); return; }\n'
                     '  if (req.url === "/api/todos" && req.method === "POST") { todos = addTodo(todos, "item");'
                     ' res.writeHead(201, {"content-type": "application/json"}); res.end(JSON.stringify(todos)); return; }\n'
                     '  if (req.url === "/") { res.writeHead(200, {"content-type": "text/html"});'
                     ' res.end("<!doctype html><title>Todos</title><h1>Todos</h1>"); return; }\n'
                     '  res.writeHead(404); res.end();\n});\n'
                     'server.listen(port, "127.0.0.1", () => console.log(`listening ${(server.address() as any).port}`));\n',
    "test/app.test.ts": 'import { test } from "node:test";\nimport assert from "node:assert/strict";\n'
                        'import { addTodo } from "../src/app.ts";\n'
                        'test("adds", () => { assert.equal(addTodo([], " a ")[0].title, "a"); });\n'
                        'test("rejects empty", () => { assert.throws(() => addTodo([], " ")); });\n',
    ".gitignore": "node_modules/\n",
}

PY_CLI = {
    "pyproject.toml": '[project]\nname = "fixture-py-cli"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n'
                      '[project.scripts]\nwc-lite = "wclite.cli:main"\n',
    "wclite/__init__.py": "",
    "wclite/cli.py": 'import argparse, sys\n\ndef count(text: str) -> dict:\n'
                     '    return {"lines": text.count("\\n"), "words": len(text.split())}\n\n'
                     'def main(argv=None) -> int:\n    p = argparse.ArgumentParser(prog="wc-lite")\n'
                     '    p.add_argument("path", nargs="?")\n    a = p.parse_args(argv)\n'
                     '    text = open(a.path).read() if a.path else sys.stdin.read()\n'
                     '    c = count(text)\n    print(f"{c[\'lines\']} {c[\'words\']}")\n    return 0\n\n'
                     'if __name__ == "__main__":\n    raise SystemExit(main())\n',
    "tests/test_cli.py": 'import unittest\nfrom wclite.cli import count\n\nclass T(unittest.TestCase):\n'
                         '    def test_count(self):\n        self.assertEqual(count("a b\\nc\\n"), {"lines": 2, "words": 3})\n\n'
                         'if __name__ == "__main__":\n    unittest.main()\n',
    "tests/__init__.py": "",
    ".gitignore": "__pycache__/\n*.egg-info/\n",
}

RUST_LIB = {
    "Cargo.toml": '[package]\nname = "fixture_rust_lib"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\n',
    "src/lib.rs": '/// Returns the median of a non-empty slice.\npub fn median(xs: &mut [i64]) -> Option<f64> {\n'
                  '    if xs.is_empty() { return None; }\n    xs.sort_unstable();\n    let n = xs.len();\n'
                  '    Some(if n % 2 == 1 { xs[n / 2] as f64 } else { (xs[n / 2 - 1] + xs[n / 2]) as f64 / 2.0 })\n}\n\n'
                  '#[cfg(test)]\nmod tests {\n    use super::*;\n'
                  '    #[test] fn odd() { assert_eq!(median(&mut [3, 1, 2]), Some(2.0)); }\n'
                  '    #[test] fn empty() { assert_eq!(median(&mut []), None); }\n}\n',
    ".gitignore": "target/\n",
}

GUI_DESKTOP = {
    "app.py": 'import tkinter as tk\nroot = tk.Tk()\ntk.Label(root, text="hello").pack()\nroot.mainloop()\n',
    "FIXTURE.md": "Negative fixture: desktop GUI. Requires a display (DISPLAY/WAYLAND_DISPLAY) and a GUI driver.\n",
}
ANDROID_APP = {
    "app/src/main/AndroidManifest.xml": '<manifest package="dev.fixture.app"><application/></manifest>\n',
    "settings.gradle": 'include ":app"\n',
    "FIXTURE.md": "Negative fixture: Android app. Requires adb + emulator/device; not a native Forge capability.\n",
}
CONTAINER_SVC = {
    "Dockerfile": "FROM scratch\nCOPY hello /hello\nCMD [\"/hello\"]\n",
    "compose.yaml": "services:\n  app:\n    build: .\n",
    "FIXTURE.md": "Negative fixture: containerised service. Requires docker/podman.\n",
}

POSITIVE = {"ts-web": TS_WEB, "py-cli": PY_CLI, "rust-lib": RUST_LIB}
NEGATIVE = {"gui-desktop": (GUI_DESKTOP, "gui_display"),
            "android-app": (ANDROID_APP, "mobile_driver"),
            "container-svc": (CONTAINER_SVC, "container_runtime")}


def make(root: Path, name: str) -> Path:
    files = POSITIVE.get(name) or NEGATIVE[name][0]
    d = root / name
    _w(d, files)
    git(d, "init", "-q", "-b", "main")
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", "initial")
    return d


def make_git_variants(root: Path) -> dict[str, Path]:
    """Repositories in the awkward states change discovery must survive."""
    v: dict[str, Path] = {}

    d = root / "git-initial"
    _w(d, {"a.txt": "a\n", "b.txt": "b\n"})
    git(d, "init", "-q", "-b", "main")
    git(d, "add", "a.txt")
    v["initial"] = d  # no commits; a.txt staged, b.txt untracked

    d = root / "git-dirty"
    _w(d, {"tracked.txt": "1\n", "staged.txt": "1\n"})
    git(d, "init", "-q", "-b", "main"); git(d, "add", "-A"); git(d, "commit", "-q", "-m", "c1")
    _w(d, {"tracked.txt": "2\n", "staged.txt": "2\n", "new.txt": "n\n"})
    git(d, "add", "staged.txt")
    v["dirty"] = d

    d = root / "git-no-upstream"
    _w(d, {"x.txt": "x\n"})
    git(d, "init", "-q", "-b", "trunk"); git(d, "add", "-A"); git(d, "commit", "-q", "-m", "c1")
    git(d, "switch", "-q", "-c", "feature")
    _w(d, {"y.txt": "y\n"}); git(d, "add", "-A"); git(d, "commit", "-q", "-m", "c2")
    v["no_upstream"] = d  # default branch is 'trunk' -> no main/master/origin

    d = root / "git-feature-with-base"
    _w(d, {"x.txt": "x\n"})
    git(d, "init", "-q", "-b", "main"); git(d, "add", "-A"); git(d, "commit", "-q", "-m", "c1")
    git(d, "switch", "-q", "-c", "feature")
    _w(d, {"y.txt": "y\n"}); git(d, "add", "-A"); git(d, "commit", "-q", "-m", "c2")
    v["feature"] = d

    d = root / "git-monorepo"
    _w(d, {"packages/api/index.ts": "export {}\n", "packages/web/index.ts": "export {}\n", "README.md": "m\n"})
    git(d, "init", "-q", "-b", "main"); git(d, "add", "-A"); git(d, "commit", "-q", "-m", "c1")
    git(d, "switch", "-q", "-c", "feature")
    _w(d, {"packages/api/index.ts": "export const x = 1\n", "packages/web/index.ts": "export const y = 2\n"})
    git(d, "commit", "-qam", "c2")
    _w(d, {"packages/api/new.ts": "n\n"})
    v["monorepo"] = d
    return v
