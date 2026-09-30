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
    routes = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
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


if __name__ == "__main__":
    unittest.main()
