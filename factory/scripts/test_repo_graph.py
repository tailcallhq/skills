#!/usr/bin/env python3
"""Unit tests for repo_graph.py (stdlib unittest). Run: python3 factory/scripts/test_repo_graph.py"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import repo_graph as G  # noqa: E402

FIX = HERE / "fixtures" / "repo_graph"
PATHS = [str(FIX / n) for n in ("api", "web", "infra")]


def run(*args: str) -> dict:
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "graph.json"
        with redirect_stderr(io.StringIO()):
            assert G.main([*args, "--org", "acme", "--out", str(out)]) == 0
        return json.loads(out.read_text())


def strip(g: dict) -> dict:
    g = dict(g)
    g.pop("generated_at")
    return g


class Edges(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = run(*PATHS)

    def test_schema(self):
        self.assertEqual(self.g["version"], 1)
        self.assertIn("generated_at", self.g)
        for r in self.g["repos"]:
            for k in ("full_name", "languages", "manifests", "errors"):
                self.assertIn(k, r)
        for e in self.g["edges"]:
            self.assertEqual(e["source"], "repo_graph")

    def test_four_edges_exact_evidence(self):
        got = {(e["from"], e["to"], e["kind"], e["evidence"], e.get("protocol"))
               for e in self.g["edges"]}
        self.assertEqual(got, {
            ("acme/web", "acme/api", "url", "config/app.json:2", "ws"),
            ("acme/infra", "acme/api", "workflow_uses", ".github/workflows/deploy.yml:5", None),
            ("acme/infra", "acme/web", "submodule", ".gitmodules:3", None),
            ("acme/web", "acme/api", "manifest", "package.json:5", None),
        })

    def test_manifests(self):
        repos = {r["full_name"]: r for r in self.g["repos"]}
        self.assertEqual(repos["acme/api"]["manifests"],
                         [{"file": "Cargo.toml", "kind": "cargo", "deps": ["insta", "serde", "tokio"]}])
        self.assertEqual(repos["acme/web"]["manifests"][0]["deps"], ["api-client", "react", "typescript"])
        self.assertEqual(repos["acme/api"]["languages"], ["Rust"])
        self.assertEqual(repos["acme/api"]["ports"], [8080])

    def test_jobs_deterministic(self):
        self.assertEqual(strip(run(*PATHS, "--jobs", "8")), strip(run(*PATHS, "--jobs", "1")))


class Parsers(unittest.TestCase):
    def test_pyproject(self):
        lines = ['[project]', 'dependencies = [', '  "requests>=2",', '  "click",', ']',
                 '[tool.poetry.dependencies]', 'python = "^3.11"', 'httpx = "*"']
        self.assertEqual(G.parse_pyproject(lines), ["requests", "click", "httpx"])

    def test_go(self):
        lines = ["module x", "require (", "  github.com/a/b v1.0.0 // indirect", ")",
                 "require golang.org/x/y v0.1.0"]
        self.assertEqual(G.parse_go(lines), ["github.com/a/b", "golang.org/x/y"])

    def test_image_edge(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "Dockerfile").write_text("FROM ghcr.io/acme/base:1.2\n")
            _, edges, _ = G.scan_repo(d, "acme")
            self.assertEqual([(e["to"], e["kind"], e["evidence"]) for e in edges],
                             [("acme/base", "image", "Dockerfile:1")])


class Merge(unittest.TestCase):
    def test_existing_kept_unless_refresh(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "graph.json"
            api = Path(d) / "api"
            shutil.copytree(FIX / "api", api)
            with redirect_stderr(io.StringIO()):
                G.main([str(api), "--org", "acme", "--out", str(out)])
                (api / "package.json").write_text('{"dependencies": {"left-pad": "1"}}')
                G.main([str(api), "--org", "acme", "--out", str(out)])
                kinds = [m["kind"] for m in json.loads(out.read_text())["repos"][0]["manifests"]]
                self.assertEqual(kinds, ["cargo"])
                G.main([str(api), "--org", "acme", "--out", str(out), "--refresh"])
                kinds = [m["kind"] for m in json.loads(out.read_text())["repos"][0]["manifests"]]
                self.assertEqual(kinds, ["cargo", "npm"])

    def test_url_edge_across_runs(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "graph.json"
            with redirect_stderr(io.StringIO()):
                G.main([PATHS[1], "--org", "acme", "--out", str(out)])
                G.main([PATHS[0], "--org", "acme", "--out", str(out)])
            kinds = [e["kind"] for e in json.loads(out.read_text())["edges"]]
            self.assertIn("url", kinds)


if __name__ == "__main__":
    unittest.main()
