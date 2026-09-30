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
import repo_graph_gh as GH  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
