#!/usr/bin/env python3
"""Unit tests for kb.py (stdlib unittest). Run: python3 factory/scripts/test_kb.py

`gh` is stubbed by a fake executable on PATH; `git` is real but pushes go to a
local bare repository.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import kb as K  # noqa: E402

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1", "KB_TODAY": "2026-09-30",
}


def kb(*args: str) -> tuple[int, dict | None, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = K.main(list(args))
    text = out.getvalue()
    return code, (json.loads(text) if text.strip() else None), err.getvalue()


def git(path, *args) -> str:
    return subprocess.run(["git", *args], cwd=path, text=True, capture_output=True, check=True).stdout.strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._env = dict(os.environ)
        os.environ.update(GIT_ENV)
        os.environ["HOME"] = str(self.tmp / "home")
        (self.tmp / "home").mkdir()
        self.kb = self.tmp / "kb"
        os.environ["KB_DIR"] = str(self.kb)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def init(self):
        code, out, err = kb("init", str(self.kb))
        self.assertEqual(code, 0, err)
        return out

    def read(self, rel):
        return (self.kb / rel).read_text()


class Init(Base):
    def test_skeleton_and_commit(self):
        out = self.init()
        for rel in ("README.md", "index.md", "connections.md", "decisions.md", "systems/.gitkeep"):
            self.assertTrue((self.kb / rel).exists(), rel)
        self.assertTrue(out["committed"])
        self.assertEqual(git(self.kb, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(git(self.kb, "status", "--porcelain"), "")
        self.assertIn("Precedence", self.read("README.md"))

    def test_refuses_non_empty(self):
        self.kb.mkdir()
        (self.kb / "x").write_text("x")
        code, _, err = kb("init", str(self.kb))
        self.assertEqual(code, 4)
        self.assertIn("not empty", err)

    def test_idempotent(self):
        self.init()
        code, out, _ = kb("init", str(self.kb), "--force")
        self.assertEqual(code, 0)
        self.assertFalse(out["committed"])


class AddSystem(Base):
    def test_create_and_extend(self):
        self.init()
        code, out, err = kb("add-system", "api", "--repo", "acme/api", "--owner", "@core",
                            "--purpose", "Public REST API", "--runtime", "fly")
        self.assertEqual(code, 0, err)
        self.assertTrue(out["created"])
        doc = K.Doc.parse(self.read("systems/api.md"))
        self.assertEqual(doc.meta["repos"], ["acme/api"])
        self.assertEqual(doc.meta["runtime"]["platform"], "fly")
        self.assertEqual(doc.meta["status"], "active")
        self.assertEqual(doc.meta["verified"], "2026-09-30")
        self.assertIn("- **purpose**: Public REST API <!-- source: user; verified: 2026-09-30 -->",
                      self.read("systems/api.md"))
        kb("add-system", "api", "--repo", "acme/api-worker", "--repo", "acme/api")
        doc = K.Doc.parse(self.read("systems/api.md"))
        self.assertEqual(doc.meta["repos"], ["acme/api", "acme/api-worker"])
        self.assertIn("[api](systems/api.md)", self.read("index.md"))
        self.assertEqual(git(self.kb, "status", "--porcelain"), "")

    def test_roundtrip(self):
        self.init()
        kb("add-system", "api", "--purpose", "Serves: the \"API\"")
        text = self.read("systems/api.md")
        self.assertEqual(K.Doc.parse(text).render(), text)

    def test_dirty_refused(self):
        self.init()
        (self.kb / "scratch.md").write_text("x")
        code, _, err = kb("add-system", "api")
        self.assertEqual(code, 4)
        self.assertIn("uncommitted", err)
        code, _, _ = kb("add-system", "api", "--force")
        self.assertEqual(code, 0)

    def test_bad_slug(self):
        self.init()
        code, _, _ = kb("add-system", "Bad Name")
        self.assertEqual(code, 1)


class AddEnv(Base):
    def setUp(self):
        super().setUp()
        self.init()
        kb("add-system", "api")

    def envs(self):
        return K.Doc.parse(self.read("systems/api.md")).meta["runtime"]["environments"]

    def test_add_and_conflict(self):
        code, out, err = kb("add-env", "api", "staging", "--id", "api-staging", "--platform", "fly",
                            "--url", "https://api.staging.acme.dev", "--source", "infra:fly:app/api-staging")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.envs()[0], {
            "name": "staging", "id": "api-staging", "platform": "fly", "url": "https://api.staging.acme.dev",
            "source": "infra:fly:app/api-staging", "verified": "2026-09-30"})
        # manifest-sourced disagreement -> question, no overwrite
        code, out, _ = kb("add-env", "api", "staging", "--id", "api-stg", "--source", "acme/api:fly.staging.toml")
        self.assertEqual(out["result"], "conflict")
        self.assertEqual(self.envs()[0]["id"], "api-staging")
        doc = K.Doc.parse(self.read("systems/api.md"))
        self.assertEqual(len(list(doc.questions())), 1)
        # user wins
        code, out, _ = kb("add-env", "api", "staging", "--id", "api-stg", "--source", "user")
        self.assertEqual(self.envs()[0]["id"], "api-stg")
        self.assertEqual(self.envs()[0]["source"], "user")
        # infra can no longer touch it
        kb("add-env", "api", "staging", "--id", "zzz", "--source", "infra:fly:app/zzz")
        self.assertEqual(self.envs()[0]["id"], "api-stg")

    def test_unknown_system(self):
        code, _, err = kb("add-env", "nope", "prod", "--id", "x", "--source", "user")
        self.assertEqual(code, 1)
        self.assertIn("no such system", err)


def monitoring_md_example() -> dict:
    text = (HERE.parent / "references" / "monitoring.md").read_text()
    start = text.index("```yaml\nmonitoring:")
    block = text[start + len("```yaml\n"): text.index("```", start + 7)]
    return K.yaml_load(block)


class AddMonitor(Base):
    def setUp(self):
        super().setUp()
        self.init()
        kb("add-system", "shop", "--repo", "acme/shop")

    def test_matches_monitoring_md(self):
        code, out, err = kb("add-monitor", "shop", "--kind", "sentry", "--project", "shop-web",
                            "--source", "acme/shop:sentry.properties", "--org", "acme")
        self.assertEqual(code, 0, err)
        # later: connected + second source; dedupes on (system, kind, project)
        kb("add-monitor", "shop", "--kind", "sentry", "--project", "shop-web", "--mcp", "sentry",
           "--source", "acme/shop:web/package.json", "--source", "acme/shop:sentry.properties")
        doc = K.Doc.parse(self.read("systems/shop.md"))
        self.assertEqual({"monitoring": doc.meta["monitoring"]}, monitoring_md_example())
        # rendered block keeps the documented key order
        text = self.read("systems/shop.md")
        self.assertIn("monitoring:\n  - kind: sentry\n    org: acme\n    project: shop-web\n    mcp: sentry\n"
                      "    source: [acme/shop:sentry.properties, acme/shop:web/package.json]\n"
                      "    verified: 2026-09-30\n", text)

    def test_distinct_projects_and_kinds(self):
        kb("add-monitor", "shop", "--kind", "sentry", "--project", "a", "--source", "user")
        kb("add-monitor", "shop", "--kind", "sentry", "--project", "b", "--source", "user")
        kb("add-monitor", "shop", "--kind", "datadog", "--site", "datadoghq.eu", "--source", "acme/shop:datadog.yaml")
        mons = K.Doc.parse(self.read("systems/shop.md")).meta["monitoring"]
        self.assertEqual([(m["kind"], m.get("project")) for m in mons], [("sentry", "a"), ("sentry", "b"), ("datadog", None)])

    def test_refusals(self):
        self.assertEqual(kb("add-monitor", "shop", "--kind", "sentry")[0], 1)  # no source
        self.assertEqual(kb("add-monitor", "shop", "--kind", "github-actions", "--source", "user")[0], 1)
        self.assertEqual(kb("add-monitor", "shop", "--kind", "newrelic", "--source", "user")[0], 1)
        self.assertEqual(kb("add-monitor", "shop", "--kind", "sentry", "--source", "sentry.properties")[0], 1)


if __name__ == "__main__":
    unittest.main()
