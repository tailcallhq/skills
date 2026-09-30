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


GRAPH = {
    "version": 1,
    "generated_at": "2026-09-30T07:30:00+00:00",
    "repos": [
        {"full_name": "acme/api", "default_branch": "main", "languages": ["Rust"],
         "manifests": [{"file": "Cargo.toml", "kind": "cargo", "deps": ["serde", "tokio"]}],
         "ports": [8080], "errors": []},
        {"full_name": "acme/web", "languages": ["TypeScript"],
         "manifests": [{"file": "package.json", "kind": "npm", "deps": ["react"]}], "ports": [], "errors": []},
    ],
    "edges": [
        {"from": "acme/web", "to": "acme/api", "kind": "url", "protocol": "ws",
         "evidence": "config/app.json:2", "source": "repo_graph"},
        {"from": "acme/web", "to": "acme/api", "kind": "url", "protocol": "ws",
         "evidence": "src/client.ts:9", "source": "repo_graph"},
        {"from": "acme/web", "to": "acme/api", "kind": "manifest",
         "evidence": "package.json:5", "source": "repo_graph"},
    ],
    "url_refs": [],
}


class Ingest(Base):
    def setUp(self):
        super().setUp()
        self.init()
        self.graph = self.tmp / "graph.json"
        self.graph.write_text(json.dumps(GRAPH))

    def ingest(self):
        code, out, err = kb("ingest", str(self.graph))
        self.assertEqual(code, 0, err)
        return out

    def test_candidates_pending(self):
        out = self.ingest()
        self.assertEqual(sorted(out["systems_added"]), ["api", "web"])
        conns = self.read("connections.md")
        self.assertIn("| web -> api | ws | ? | acme/web:config/app.json:2 | pending |", conns)
        self.assertIn("| web -> api | library (manifest) | ? | acme/web:package.json:5 | pending |", conns)
        self.assertEqual(conns.count("web -> api | ws"), 1)  # two ws edges -> one row
        self.assertIn('web -.->|"ws"| api', conns)
        api = K.Doc.parse(self.read("systems/api.md"))
        self.assertEqual(api.meta["status"], "candidate")
        self.assertEqual(api.meta["repos"], ["acme/api"])
        self.assertIn("- **acme/api cargo deps**: serde, tokio <!-- source: acme/api:Cargo.toml; verified: pending -->",
                      self.read("systems/api.md"))
        self.assertIn("- **acme/api exposed ports**: 8080", self.read("systems/api.md"))
        self.assertIn("[web](systems/web.md)", self.read("index.md"))

    def test_idempotent(self):
        self.ingest()
        snap = {p: (self.kb / p).read_text() for p in ("connections.md", "systems/api.md", "systems/web.md", "index.md")}
        head = git(self.kb, "rev-parse", "HEAD")
        out = self.ingest()
        self.assertFalse(out["committed"])
        self.assertEqual(git(self.kb, "rev-parse", "HEAD"), head)
        for p, text in snap.items():
            self.assertEqual((self.kb / p).read_text(), text, p)

    def test_preserves_user_facts_and_existing_systems(self):
        # a human already mapped both repos into one system and stated facts
        kb("add-system", "platform", "--repo", "acme/api", "--purpose", "Everything backend")
        kb("add-fact", "platform", "--section", "interfaces", "--key", "acme/api exposed ports",
           "--value", "8443", "--source", "user")
        kb("add-system", "frontend", "--repo", "acme/web")
        kb("add-connection", "frontend", "platform", "--protocol", "ws", "--auth", "oauth", "--source", "user")
        user_lines = [l for l in self.read("systems/platform.md").splitlines() if "source: user" in l]
        conn_user = [l for l in self.read("connections.md").splitlines() if "| user |" in l]
        out = self.ingest()
        self.assertEqual(out["systems_added"], [])
        text = self.read("systems/platform.md")
        for l in user_lines:
            self.assertIn(l, text)
        # manifest disagrees with user: no new line, a question instead
        self.assertNotIn("**acme/api exposed ports**: 8080", text)
        self.assertIn("precedence keeps `8443` (user)", text)
        conns = self.read("connections.md")
        for l in conn_user:
            self.assertIn(l, conns)
        self.assertNotIn("acme/web:config/app.json:2", conns)  # same edge, user row wins
        self.assertIn("frontend -> platform | library (manifest)", conns)  # other protocol: candidate
        self.assertFalse((self.kb / "systems/api.md").exists())

    def test_never_deletes(self):
        self.ingest()
        smaller = dict(GRAPH, repos=GRAPH["repos"][:1], edges=[])
        self.graph.write_text(json.dumps(smaller))
        self.ingest()
        self.assertTrue((self.kb / "systems/web.md").exists())
        self.assertIn("web -> api | ws", self.read("connections.md"))

    def test_secret_in_graph_refused(self):
        bad = json.loads(json.dumps(GRAPH))
        bad["repos"][0]["manifests"][0]["deps"].append("ghp_" + "a" * 36)
        self.graph.write_text(json.dumps(bad))
        code, _, err = kb("ingest", str(self.graph))
        self.assertEqual(code, 3)
        self.assertFalse((self.kb / "systems/api.md").exists())


class Precedence(Base):
    def setUp(self):
        super().setUp()
        self.init()
        kb("add-system", "api")

    def fact(self, value, source):
        code, out, err = kb("add-fact", "api", "--section", "interfaces", "--key", "port",
                            "--value", value, "--source", source)
        self.assertEqual(code, 0, err)
        return out["result"]

    def effective(self):
        doc = K.Doc.parse(self.read("systems/api.md"))
        return K.effective([f for _, _, f in doc.facts() if f["key"] == "port"])

    def test_order(self):
        self.assertEqual(self.fact("8080", "acme/api:Dockerfile"), "added")
        self.assertEqual(self.fact("9090", "infra:fly:app/api"), "conflict")
        self.assertEqual(self.effective()["value"], "9090")
        self.assertEqual(self.fact("443", "user"), "conflict")
        self.assertEqual(self.effective()["source"], "user")
        # lower precedence after user: only a question, never a new effective value
        self.assertEqual(self.fact("7070", "infra:k8s:svc/api"), "conflict")
        self.assertEqual(self.effective()["value"], "443")
        text = self.read("systems/api.md")
        self.assertIn("8080", text)  # manifest line kept
        doc = K.Doc.parse(text)
        self.assertEqual(len(list(doc.questions())), 3)

    def test_same_source_update(self):
        self.fact("8080", "acme/api:Dockerfile")
        self.assertEqual(self.fact("8081", "acme/api:Dockerfile"), "updated")
        self.assertEqual(self.effective()["value"], "8081")


class Secrets(Base):
    def test_patterns(self):
        for s in ["AKIA" + "ABCDEFGHIJKLMNOP", "AIza" + "x" * 35, "ghp_" + "a" * 36, "sk-" + "a" * 24,
                  "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
                  "password=hunter2", '{"type": "service_account", "project_id": "x"}',
                  "-----BEGIN RSA PRIVATE KEY-----"]:
            self.assertIsNotNone(K.find_secret(s), s)
        for s in ["https://api.staging.acme.dev:8443", "acme/api:.env.example", "SENTRY_AUTH_TOKEN",
                  "oauth", "api-key header"]:
            self.assertIsNone(K.find_secret(s), s)

    def test_refused_everywhere(self):
        self.init()
        kb("add-system", "api")
        head = git(self.kb, "rev-parse", "HEAD")
        tok = "ghp_" + "b" * 36
        cases = [
            ("add-system", "api", "--purpose", f"uses {tok}"),
            ("add-env", "api", "prod", "--id", "x", "--url", "https://u:password=x@h", "--source", "user"),
            ("add-monitor", "api", "--kind", "sentry", "--project", tok, "--source", "user"),
            ("add-fact", "api", "--section", "purpose", "--key", "k", "--value", "AKIA" + "Z" * 16, "--source", "user"),
            ("add-connection", "api", "external:x", "--protocol", "http", "--auth", "sk-" + "c" * 30, "--source", "user"),
        ]
        for c in cases:
            code, _, err = kb(*c)
            self.assertEqual(code, 3, c)
            self.assertIn("secret", err)
        # --force bypasses the dirty-checkout check, never the secret check
        code, _, _ = kb("add-fact", "api", "--section", "purpose", "--key", "k", "--value", tok,
                        "--source", "user", "--force")
        self.assertEqual(code, 3)
        self.assertEqual(git(self.kb, "rev-parse", "HEAD"), head)


class IndexAndStale(Base):
    def test_index_regenerates(self):
        self.init()
        kb("add-system", "api", "--purpose", "REST API", "--owner", "@core")
        kb("add-system", "web")
        kb("add-connection", "web", "api", "--protocol", "https", "--source", "user")
        # a human edits the table by hand and breaks the graph / index
        conns = self.read("connections.md").replace('web -->|"https"| api', "")
        (self.kb / "connections.md").write_text(conns)
        (self.kb / "index.md").write_text("junk\n")
        git(self.kb, "commit", "-qam", "hand edit")
        code, out, err = kb("index")
        self.assertEqual(code, 0, err)
        self.assertEqual(out["systems"], 2)
        self.assertIn('web -->|"https"| api', self.read("connections.md"))
        idx = self.read("index.md")
        self.assertIn("| [api](systems/api.md) | active |  | @core | REST API | 2026-09-30 | 0 |", idx)
        self.assertIn("[web](systems/web.md)", idx)
        self.assertTrue(out["committed"])
        self.assertFalse(kb("index")[1]["committed"])

    def test_stale(self):
        os.environ["KB_TODAY"] = "2026-07-01"
        self.init()
        kb("add-system", "api", "--purpose", "old fact")
        kb("add-env", "api", "prod", "--id", "api-prod", "--source", "infra:fly:app/api-prod")
        os.environ["KB_TODAY"] = "2026-09-25"
        kb("add-fact", "api", "--section", "interfaces", "--key", "port", "--value", "8080", "--source", "user")
        kb("add-fact", "api", "--section", "interfaces", "--key", "grpc", "--value", "9000",
           "--source", "acme/api:build.rs", "--pending")
        os.environ["KB_TODAY"] = "2026-09-30"
        code, out, _ = kb("stale", "--days", "30")
        self.assertEqual(code, 0)
        keys = sorted((s["kind"], s["key"]) for s in out["stale"])
        self.assertEqual(keys, [("environment", "prod"), ("fact", "purpose"), ("system", "api")])
        self.assertEqual([p["key"] for p in out["pending"]], ["grpc"])
        self.assertEqual({s["age_days"] for s in out["stale"]}, {91})
        code, out, _ = kb("stale", "--days", "100")
        self.assertEqual(out["stale"], [])


if __name__ == "__main__":
    unittest.main()
