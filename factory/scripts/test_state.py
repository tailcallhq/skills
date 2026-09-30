#!/usr/bin/env python3
"""Unit tests for state.py (stdlib unittest). Run: python3 factory/scripts/test_state.py"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import state as S  # noqa: E402

SCRIPT = Path(__file__).resolve().parent / "state.py"


def cli(path: Path, *args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = S.main(["--file", str(path), *args])
    return code, out.getvalue().strip(), err.getvalue().strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / ".agents" / "factory-state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def read(self) -> dict:
        return json.loads(self.path.read_text())


class TestInit(Base):
    def test_fresh_init(self):
        code, out, _ = cli(self.path, "init", "--org", "acme")
        self.assertEqual(code, 0)
        st = self.read()
        self.assertEqual(S.validate(st), [])
        self.assertEqual(st["version"], S.VERSION)
        self.assertEqual(st["org"], "acme")
        self.assertEqual([p["id"] for p in st["phases"]], list(S.PHASE_IDS))
        self.assertTrue(all(p["status"] == "pending" for p in st["phases"]))
        self.assertEqual(st["kb"], {"url": None, "path": None, "project_id": None})
        self.assertEqual(st["machine"], {"cloud": None, "push_triggers": None})
        self.assertEqual((st["repos"], st["routines"]), ({}, {}))
        self.assertIn("resuming at phase 0", out)

    def test_next_without_state(self):
        code, out, _ = cli(self.path, "next")
        self.assertEqual(code, 0)
        self.assertIn("no factory state", out)
        self.assertFalse(self.path.exists())

    def test_init_is_idempotent_and_keeps_progress(self):
        cli(self.path, "init", "--org", "acme")
        cli(self.path, "set-phase", "0", "done")
        code, out, err = cli(self.path, "init", "--org", "acme")
        self.assertEqual(code, 0)
        self.assertIn("exists", err)
        self.assertEqual(S.get_path(self.read(), "phases.0")["status"], "done")
        self.assertIn("phase 1", out)

    def test_init_other_org_refused_without_force(self):
        cli(self.path, "init", "--org", "acme")
        self.assertEqual(cli(self.path, "init", "--org", "other")[0], 1)
        self.assertEqual(cli(self.path, "init", "--org", "other", "--force")[0], 0)
        self.assertEqual(self.read()["org"], "other")

    def test_set_requires_init(self):
        code, _, err = cli(self.path, "set-phase", "0", "done")
        self.assertEqual(code, 1)
        self.assertIn("init", err)


class TestResume(Base):
    def setUp(self):
        super().setUp()
        cli(self.path, "init", "--org", "acme")

    def test_resume_mid_phase_reports_pending_repos(self):
        cli(self.path, "set", "kb.url", "https://github.com/acme/knowledge")
        cli(self.path, "set-phase", "0", "done", "--output", "gh_user=octo")
        cli(self.path, "set-phase", "1", "done", "--output", "kb_repo=acme/knowledge")
        cli(self.path, "set-phase", "2", "done")
        cli(self.path, "set-phase", "3", "in_progress")
        for name in ("api", "web", "worker"):
            cli(self.path, "set-repo", name, "clone", "done", "--path", f"/w/{name}")
        for stage in ("survey", "graph", "draft"):
            cli(self.path, "set-repo", "api", stage, "done")
        cli(self.path, "set-repo", "web", "survey", "done")

        # A "second session": fresh process reading the same file.
        r = subprocess.run([sys.executable, str(SCRIPT), "--file", str(self.path), "next"],
                           capture_output=True, text=True, check=True)
        self.assertEqual(r.stdout.strip(), "resuming at phase 3 (research), repos web,worker pending")
        info = S.next_phase(self.read())
        self.assertEqual(info["done"], ["0", "1", "2"])
        self.assertEqual(info["pending_repos"], ["web", "worker"])
        # Outputs from completed phases survive for the resumed session.
        self.assertEqual(json.loads(cli(self.path, "get", "phases.1.outputs")[1]), {"kb_repo": "acme/knowledge"})
        self.assertEqual(json.loads(cli(self.path, "get", "kb.url")[1]), "https://github.com/acme/knowledge")

    def test_skipped_phase_is_not_resumed(self):
        for pid in ("0", "1", "2", "3", "4"):
            cli(self.path, "set-phase", pid, "done")
        cli(self.path, "set-phase", "4b", "skipped")
        self.assertEqual(cli(self.path, "next")[1], "resuming at phase 5 (board)")

    def test_all_done(self):
        for pid in S.PHASE_IDS:
            cli(self.path, "set-phase", pid, "done")
        info = json.loads(cli(self.path, "next", "--json")[1])
        self.assertEqual(info["state"], "complete")

    def test_blocked_then_unblocked(self):
        cli(self.path, "set-phase", "0", "blocked", "--blocker", "gh auth status: not logged in")
        info = json.loads(cli(self.path, "next", "--json")[1])
        self.assertEqual((info["phase"], info["status"]), ("0", "blocked"))
        self.assertEqual(info["blocker"], "gh auth status: not logged in")
        self.assertIn("blocked: gh auth status", info["message"])
        # blocked requires a message
        self.assertEqual(cli(self.path, "set-phase", "1", "blocked")[0], 1)
        # unblock: blocker is cleared
        cli(self.path, "set-phase", "0", "in_progress")
        p = S.get_path(self.read(), "phases.0")
        self.assertEqual((p["status"], p["blocker"]), ("in_progress", None))
        cli(self.path, "set-phase", "0", "done")
        self.assertEqual(S.next_phase(self.read())["phase"], "1")

    def test_summary_lists_phases_and_repos(self):
        cli(self.path, "set-phase", "0", "done")
        cli(self.path, "set-repo", "api", "clone", "done")
        _, out, _ = cli(self.path, "summary")
        self.assertIn("phase 0", out)
        self.assertIn("api", out)
        self.assertIn("resuming at phase 1", out)


class TestRefuseDowngrade(Base):
    def setUp(self):
        super().setUp()
        cli(self.path, "init")

    def test_phase_done_downgrade_refused(self):
        cli(self.path, "set-phase", "0", "done")
        code, _, err = cli(self.path, "set-phase", "0", "in_progress")
        self.assertEqual(code, 1)
        self.assertIn("--force", err)
        self.assertEqual(S.get_path(self.read(), "phases.0")["status"], "done")
        self.assertEqual(cli(self.path, "set-phase", "0", "pending", "--force")[0], 0)
        self.assertEqual(S.get_path(self.read(), "phases.0")["status"], "pending")

    def test_done_to_done_allowed_and_merges_outputs(self):
        cli(self.path, "set-phase", "1", "done", "--output", "a=1")
        self.assertEqual(cli(self.path, "set-phase", "1", "done", "--output", "b=2")[0], 0)
        self.assertEqual(S.get_path(self.read(), "phases.1.outputs"), {"a": "1", "b": "2"})

    def test_repo_done_downgrade_refused(self):
        cli(self.path, "set-repo", "api", "clone", "done")
        self.assertEqual(cli(self.path, "set-repo", "api", "clone", "pending")[0], 1)
        self.assertEqual(self.read()["repos"]["api"]["stages"]["clone"], "done")
        self.assertEqual(cli(self.path, "set-repo", "api", "clone", "pending", "--force")[0], 0)

    def test_invalid_values_rejected(self):
        self.assertEqual(cli(self.path, "set-phase", "9", "done")[0], 1)
        self.assertEqual(cli(self.path, "set-phase", "0", "finished")[0], 1)
        self.assertEqual(cli(self.path, "set-repo", "api", "deploy", "done")[0], 1)
        self.assertEqual(cli(self.path, "set", "machine.cloud", "maybe")[0], 1)
        self.assertEqual(cli(self.path, "set", "version", "9")[0], 1)


class TestCorruptRecovery(Base):
    def test_restores_from_backup(self):
        cli(self.path, "init", "--org", "acme")
        cli(self.path, "set-phase", "0", "done")
        cli(self.path, "set-phase", "1", "done")  # .bak now holds phase 0 done
        self.path.write_text('{"version": 1, "phases": [trunc')
        code, out, err = cli(self.path, "next")
        self.assertEqual(code, 0)
        self.assertIn("restored from .bak", err)
        self.assertIn("phase 1", out)  # .bak had 0 done, 1 not yet
        self.assertEqual(S.validate(self.read()), [])
        self.assertEqual(len(list(self.path.parent.glob("*.corrupt-*"))), 1)

    def test_fresh_when_no_backup(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("not json at all")
        code, out, err = cli(self.path, "set-phase", "0", "in_progress")
        self.assertEqual(code, 0)
        self.assertIn("starting fresh", err)
        st = self.read()
        self.assertEqual(S.validate(st), [])
        self.assertTrue(st["recovered_from"].startswith("factory-state.json.corrupt-"))
        self.assertEqual(S.get_path(st, "phases.0")["status"], "in_progress")
        self.assertIn("WARNING: recovered", cli(self.path, "summary")[1])

    def test_invalid_schema_salvages_org(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"version": 1, "org": "acme", "phases": "oops"}))
        cli(self.path, "next")
        self.assertEqual(self.read()["org"], "acme")

    def test_newer_version_refused_not_clobbered(self):
        self.path.parent.mkdir(parents=True)
        newer = json.dumps({"version": S.VERSION + 1})
        self.path.write_text(newer)
        self.assertEqual(cli(self.path, "next")[0], 1)
        self.assertEqual(self.path.read_text(), newer)

    def test_no_tmp_files_left(self):
        cli(self.path, "init")
        cli(self.path, "set-repo", "api", "clone", "done")
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])


class TestConcurrency(Base):
    def setUp(self):
        super().setUp()
        cli(self.path, "init")

    def test_eight_threads_lose_no_writes(self):
        repos = [f"repo{i:02d}" for i in range(8)]
        errors: list[BaseException] = []
        barrier = threading.Barrier(len(repos))

        def worker(name):
            try:
                barrier.wait()
                for stage in S.STAGES:
                    S.set_repo(self.path, name, stage, "in_progress")
                    S.set_repo(self.path, name, stage, "done", repo_path=f"/w/{name}")
            except BaseException as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in repos]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        st = self.read()
        self.assertEqual(sorted(st["repos"]), repos)
        for name in repos:
            self.assertEqual(st["repos"][name]["stages"], {s: "done" for s in S.STAGES})
            self.assertEqual(st["repos"][name]["path"], f"/w/{name}")

    def test_parallel_processes_lose_no_writes(self):
        repos = [f"p{i}" for i in range(8)]
        procs = [subprocess.Popen([sys.executable, str(SCRIPT), "--file", str(self.path),
                                   "set-repo", n, "clone", "done"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for n in repos]
        for p in procs:
            self.assertEqual(p.wait(timeout=60), 0, p.stderr.read())
            p.stderr.close()
        st = self.read()
        self.assertEqual(sorted(st["repos"]), repos)


class TestPendingRepos(Base):
    def setUp(self):
        super().setUp()
        cli(self.path, "init")

    def test_after_partial_completion(self):
        for n in ("a", "b", "c", "d"):
            cli(self.path, "set-repo", n, "clone", "pending", "--url", f"https://github.com/acme/{n}")
        cli(self.path, "set-repo", "a", "clone", "done")
        cli(self.path, "set-repo", "b", "clone", "skipped")
        cli(self.path, "set-repo", "c", "clone", "in_progress")
        self.assertEqual(json.loads(cli(self.path, "pending-repos", "clone")[1]), ["c", "d"])
        # survey is pending everywhere; --ready keeps only repos whose clone finished
        self.assertEqual(json.loads(cli(self.path, "pending-repos", "survey")[1]), ["a", "b", "c", "d"])
        self.assertEqual(json.loads(cli(self.path, "pending-repos", "survey", "--ready")[1]), ["a", "b"])
        cli(self.path, "set-repo", "a", "survey", "done")
        self.assertEqual(json.loads(cli(self.path, "pending-repos", "survey", "--ready")[1]), ["b"])

    def test_empty_and_blocked(self):
        self.assertEqual(json.loads(cli(self.path, "pending-repos", "clone")[1]), [])
        cli(self.path, "set-repo", "a", "clone", "blocked")
        self.assertEqual(json.loads(cli(self.path, "pending-repos", "clone")[1]), ["a"])


class TestRoutinesAndKeys(Base):
    def test_set_keys_and_routine(self):
        cli(self.path, "init")
        cli(self.path, "set", "machine.cloud", "true")
        cli(self.path, "set", "kb.project_id", "abc")
        cli(self.path, "set-routine", "alert_intake", "--automation-id", "auto-1")
        cli(self.path, "set-routine", "alert_intake", "--cursor", "2026-09-30T00:00:00Z")
        st = self.read()
        self.assertIs(st["machine"]["cloud"], True)
        self.assertEqual(st["kb"]["project_id"], "abc")
        self.assertEqual(st["routines"]["alert_intake"]["automation_id"], "auto-1")
        self.assertEqual(st["routines"]["alert_intake"]["cursor"], "2026-09-30T00:00:00Z")

    def test_env_var_selects_file(self):
        env_path = Path(self.tmp.name) / "alt.json"
        os.environ["FACTORY_STATE"] = str(env_path)
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(S.main(["init"]), 0)
        finally:
            del os.environ["FACTORY_STATE"]
        self.assertTrue(env_path.exists())


if __name__ == "__main__":
    unittest.main(verbosity=1)
