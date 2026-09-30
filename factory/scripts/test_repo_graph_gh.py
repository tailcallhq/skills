#!/usr/bin/env python3
"""Tests for the gh layer of repo_graph (fake gh on PATH). Run: python3 factory/scripts/test_repo_graph_gh.py"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import repo_graph as G  # noqa: E402
import repo_graph_gh as GH  # noqa: E402
import io, time  # noqa: E402
from contextlib import redirect_stderr  # noqa: E402

FAKE = HERE / "fixtures" / "fake_gh"


class FakeGhCase(unittest.TestCase):
    routes = None  # None: use fixtures/fake_gh/routes.json

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.routes_file = FAKE / "routes.json"
        if self.routes is not None:
            self.routes_file = Path(self.tmp.name) / "routes.json"
            self.routes_file.write_text(json.dumps(self.routes))
        self.log_file = Path(self.tmp.name) / "calls.log"
        self.env = dict(os.environ)
        os.environ["PATH"] = f"{FAKE}{os.pathsep}{os.environ['PATH']}"
        os.environ["FAKE_GH_ROUTES"] = str(self.routes_file)
        os.environ["FAKE_GH_LOG"] = str(self.log_file)
        for k in ("FAKE_GH_DELAY", "FAKE_GH_REMAINING"):
            os.environ.pop(k, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        self.tmp.cleanup()

    def calls(self):
        return self.log_file.read_text().split() if self.log_file.exists() else []


class Wrapper(FakeGhCase):
    routes = {"repos/acme/api": {"name": "api"},
              "repos/acme/secret": {"__status": 403, "__body": {"message": "Resource not accessible"}}}

    def test_ok(self):
        data, err = GH.Gh().api("repos/acme/api")
        self.assertEqual((data, err), ({"name": "api"}, None))

    def test_403_is_error_not_raise(self):
        data, err = GH.Gh().api("repos/acme/secret")
        self.assertIsNone(data)
        self.assertIn("403", err)
        self.assertIn("Resource not accessible", err)

    def test_backoff_when_low(self):
        os.environ["FAKE_GH_REMAINING"] = "10"
        slept = []
        gh = GH.Gh(sleep=slept.append)
        gh.api("repos/acme/api")
        self.assertEqual(slept, [])  # first call: remaining unknown
        gh.api("repos/acme/api")
        self.assertEqual(len(slept), 1)
        self.assertEqual(gh.backoffs, 1)
        self.assertLessEqual(slept[0], GH.MAX_SLEEP)

    def test_no_backoff_at_threshold(self):
        os.environ["FAKE_GH_REMAINING"] = "50"
        slept = []
        gh = GH.Gh(sleep=slept.append)
        gh.api("repos/acme/api")
        gh.api("repos/acme/api")
        self.assertEqual(slept, [])
        self.assertEqual(gh.calls, 2)


class FetchRepo(FakeGhCase):
    def test_metadata_activity_files(self):
        gh = GH.Gh()
        info, files, errors = GH.fetch_repo(gh, "acme/svc1")
        self.assertEqual(errors, [])
        self.assertEqual(gh.calls, GH.CALLS_PER_REPO)
        self.assertEqual(info["default_branch"], "main")
        self.assertEqual(info["languages"], ["Rust", "Shell"])
        self.assertEqual(info["topics"], ["infra"])
        a = info["activity"]
        self.assertEqual((a["open_prs"], a["merged_prs_30d"]), (3, 7))
        self.assertEqual([b["name"] for b in a["active_branches_14d"]], ["main"])
        self.assertEqual([c["login"] for c in a["top_contributors"]], ["u0", "u1", "u2", "u3", "u4"])
        self.assertEqual(sorted(files), [".github/workflows/ci.yml", ".gitmodules", "Cargo.toml"])

    def test_403_recorded(self):
        info, files, errors = GH.fetch_repo(GH.Gh(), "acme/locked")
        self.assertEqual((info, files), ({}, {}))
        self.assertEqual(len(errors), 1)
        self.assertIn("403", errors[0])


class ListOrg(FakeGhCase):
    def test_paginates_and_skips_archived(self):
        repos, errors = GH.list_org(GH.Gh(), "acme", 50)
        self.assertEqual(errors, [])
        self.assertEqual(repos, [f"acme/svc{i}" for i in range(1, 7)] + ["acme/locked"])

    def test_cap(self):
        repos, _ = GH.list_org(GH.Gh(), "acme", 3)
        self.assertEqual(repos, ["acme/svc1", "acme/svc2", "acme/svc3"])
        self.assertEqual(self.calls(), ["graphql:org:acme"])


SIX = [f"acme/svc{i}" for i in range(1, 7)]


def cli(*args):
    with tempfile.TemporaryDirectory() as d, redirect_stderr(io.StringIO()):
        out = Path(d) / "graph.json"
        assert G.main([*args, "--org", "acme", "--out", str(out), "--workspaces", d]) == 0
        g = json.loads(out.read_text())
    g.pop("generated_at")
    return g


class Cli(FakeGhCase):
    def test_remote_repo_edges_and_activity(self):
        g = cli("acme/svc1")
        (r,) = g["repos"]
        self.assertEqual((r["full_name"], r["path"], r["source"]), ("acme/svc1", None, "gh api"))
        self.assertEqual(r["activity"]["open_prs"], 3)
        self.assertEqual(r["manifests"], [{"file": "Cargo.toml", "kind": "cargo", "deps": ["lib"]}])
        kinds = {(e["to"], e["kind"]) for e in g["edges"]}
        self.assertEqual(kinds, {("acme/svc2", "manifest"), ("acme/proto", "submodule"),
                                 ("acme/actions", "workflow_uses")})
        self.assertEqual(g["api_calls"], GH.CALLS_PER_REPO)

    def test_403_recorded_and_batch_continues(self):
        g = cli("acme/locked", "acme/svc1", "--jobs", "2")
        by = {r["full_name"]: r for r in g["repos"]}
        self.assertIn("403", by["acme/locked"]["errors"][0])
        self.assertEqual(by["acme/svc1"]["errors"], [])
        self.assertTrue(any(e["from"] == "acme/svc1" for e in g["edges"]))

    def test_org_enumeration_cap(self):
        g = cli("--max-repos", "3")
        self.assertEqual([r["full_name"] for r in g["repos"]], SIX[:3])
        self.assertEqual(self.calls().count("graphql:org:acme"), 1)
        self.assertNotIn("graphql:org:acme@c1", self.calls())

    def test_org_default_enumerates_all(self):
        g = cli()
        self.assertEqual(len(g["repos"]), 7)  # 6 svc + locked (archived skipped)
        self.assertEqual(g["api_calls"], 2 + 6 * GH.CALLS_PER_REPO + 1)

    def test_org_prefers_local_checkout(self):
        with tempfile.TemporaryDirectory() as ws:
            (Path(ws) / "svc1").mkdir()
            (Path(ws) / "svc1" / "Cargo.toml").write_text('[dependencies]\nlocal_only = "1"\n')
            with redirect_stderr(io.StringIO()):
                out = Path(ws) / "g.json"
                G.main(["--org", "acme", "--max-repos", "2", "--workspaces", ws, "--out", str(out)])
            g = json.loads(out.read_text())
        by = {r["full_name"]: r for r in g["repos"]}
        self.assertEqual(by["acme/svc1"]["manifests"][0]["deps"], ["local_only"])
        self.assertNotIn("graphql:acme/svc1", self.calls())
        self.assertIn("graphql:acme/svc2", self.calls())

    def test_local_path_without_activity_makes_no_calls(self):
        fix = str(HERE / "fixtures" / "repo_graph" / "api")
        g = cli(fix)
        self.assertNotIn("api_calls", g)
        self.assertEqual(self.calls(), [])

    def test_activity_on_local_path(self):
        with tempfile.TemporaryDirectory() as ws:
            d = Path(ws) / "svc3"
            d.mkdir()
            g = cli(str(d), "--activity")
        (r,) = g["repos"]
        self.assertEqual(r["full_name"], "acme/svc3")
        self.assertEqual(r["activity"]["merged_prs_30d"], 7)
        self.assertEqual(r["languages"], ["Rust", "Shell"])

    def test_jobs_parallel_faster_same_output(self):
        os.environ["FAKE_GH_DELAY"] = "0.2"
        t0 = time.monotonic()
        g1 = cli(*SIX, "--jobs", "1")
        t1 = time.monotonic() - t0
        t0 = time.monotonic()
        g8 = cli(*SIX, "--jobs", "8")
        t8 = time.monotonic() - t0
        self.assertEqual(g1, g8)
        self.assertGreater(t1, 12 * 0.2)       # 6 repos x 2 sequential calls
        self.assertLess(t8, t1 / 2, (t1, t8))


if __name__ == "__main__":
    unittest.main()
