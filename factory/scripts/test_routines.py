#!/usr/bin/env python3
"""Unit tests for routines.py. Run: python3 factory/scripts/test_routines.py"""
from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import routines as R  # noqa: E402

STATE_PY = HERE / "state.py"
KB_PID = "0c8dcfc7-5b1e-4c2a-9f3d-8e6a1b2c4d5f"
BOARD_PID = "7ca331c5-88f1-44de-95f7-4d8dc20bdd19"


def make_state(root: Path, *, org="acme", kb_url="https://github.com/acme/knowledge",
               kb_path="/home/me/workspaces/knowledge", kb_pid=KB_PID, board=BOARD_PID,
               infra: dict | None = None) -> Path:
    """Build a fixture state with the real state.py, so the schema stays honest."""
    f = root / ".agents" / "factory-state.json"
    f.parent.mkdir(parents=True, exist_ok=True)

    def st(*args):
        subprocess.run([sys.executable, str(STATE_PY), "--file", str(f), *args],
                       check=True, capture_output=True, text=True)

    st("init", *(["--org", org] if org else []))
    if kb_url:
        st("set", "kb.url", kb_url)
    if kb_path:
        st("set", "kb.path", kb_path)
    if kb_pid:
        st("set", "kb.project_id", kb_pid)
    if board:
        st("set-phase", "5", "done", "--output", f"project_id={board}")
    if infra:
        st("set-phase", "4c", "done", *[x for k, v in infra.items() for x in ("--output", f"{k}={v}")])
    return f


class Cron(unittest.TestCase):
    def test_valid(self):
        for expr in ["0 5 * * 1", "30 2 * * *", "*/15 0-6 1,15 jan-mar mon-fri", "0 0 * * 7", "5 4 * * sun"]:
            self.assertEqual(len(R.check_cron(expr)), 5, expr)

    def test_invalid(self):
        for expr in ["", "0 5 * *", "0 5 * * 1 2026", "60 * * * *", "0 24 * * *", "0 0 0 * *",
                     "0 0 * 13 *", "0 0 * * 8", "@weekly", "0 0 * * mon-", "*/0 * * * *", "5-1 * * * *",
                     "0 0 ,1 * *", "x * * * *"]:
            with self.assertRaises(R.RoutineError, msg=expr):
                R.check_cron(expr)

    def test_every_default_cron_valid(self):
        for name, meta in R.CATALOG.items():
            R.check_cron(meta["cron"])

    def test_cli(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(R.main(["cron-check", "kb-refresh"]), 0)
        self.assertTrue(json.loads(out.getvalue())["ok"])
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(R.main(["cron-check", "kb-refresh", "--cron", "0 5 * *"]), 1)
            self.assertEqual(R.main(["cron-check", "nope"]), 1)
        self.assertIn("5 fields", err.getvalue())


class Catalog(unittest.TestCase):
    def test_doc_blocks_and_table_match_catalog(self):
        doc = R.DOC.read_text()
        blocks = R.load_blocks()
        for name, meta in R.CATALOG.items():
            self.assertIn(name, blocks, f"no ```text {name} block")
            self.assertIn(f"## {name}", doc)
            row = re.search(rf"^\| \[{re.escape(name)}\]\(#[^)]*\) \| `([^`]+)`", doc, re.M)
            self.assertIsNotNone(row, f"catalog table row for {name}")
            self.assertEqual(row.group(1), meta["cron"], f"{name}: table cron != CATALOG cron")
            self.assertIn(meta["tier"], ("fast", "intelligent"))

    def test_list(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(R.main(["list", "--json"]), 0)
        self.assertEqual({r["name"] for r in json.loads(out.getvalue())}, set(R.CATALOG))


class Prompts(unittest.TestCase):
    """Invariants every routine prompt must satisfy (raw and rendered)."""

    def test_every_prompt_invariants(self):
        for name in R.CATALOG:
            with self.subTest(name=name):
                p = R.raw_prompt(name)
                # access check first, fail loudly on the board
                step0 = p.index("STEP 0")
                self.assertLess(step0, p.index("TASK:"))
                self.assertIn("`gh auth status`", p[:p.index("TASK:")])
                self.assertIn("PRIVATE", p[:p.index("TASK:")])
                self.assertIn("FAIL LOUDLY", p)
                self.assertIn("add_issue", p[:p.index("TASK:")])
                self.assertIn("routine-failure", p)
                # ids from state, not hard-coded
                self.assertIn("state.py --file {{state_path}} get", p)
                self.assertIn("kb.project_id", p)
                # never merges / runs / touches infra
                self.assertIn("Never merge", p)
                self.assertIn("Never call `project_run`", p)
                self.assertIn("Never write to infrastructure", p)
                for bad in ("gh pr merge", "--auto-approve", "--allow-multiple\"", "terraform apply",
                            "kubectl apply", "--admin", "git push --force"):
                    self.assertNotIn(bad, p)
                self.assertIsNone(R.find_credential(p), "credential-like string in prompt")

    def test_no_hardcoded_ids(self):
        uuid = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
        for name in R.CATALOG:
            self.assertIsNone(uuid.search(R.raw_prompt(name)), name)

    def test_find_credential_catches_samples(self):
        for s in ["AKIA" + "ABCDEFGHIJKLMNOP", "ghp_" + "a" * 36, "token=" + "abcdef123456",
                  "https://user:" + "pw@host/x", "xoxb-" + "1234567890abc"]:
            self.assertIsNotNone(R.find_credential(s), s)
        for s in ["SENTRY_AUTH_TOKEN", "`gh auth status`", "{{env.AWS_PROFILE}}", "https://github.com/acme/knowledge"]:
            self.assertIsNone(R.find_credential(s), s)


class Render(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_render_fills_ids_from_state(self):
        f = make_state(self.root, infra={"infra_kubernetes": "passed"})
        for name, meta in R.CATALOG.items():
            with self.subTest(name=name):
                out = R.render(name, f)
                p = out["prompt"]
                self.assertNotRegex(p, r"\{\{[a-z_]+\}\}")
                self.assertIn("acme/knowledge", p)
                self.assertIn(BOARD_PID, p)
                self.assertIn(str(f.resolve()), p)
                self.assertIn(f"Workspace root: {self.root.resolve()}", p)
                self.assertIn(str(R.SKILL_DIR), p)
                self.assertEqual(out["trigger"], {"cron": meta["cron"]})
                self.assertEqual(out["overlap_policy"], "skip")
                self.assertTrue(out["name"])
                self.assertTrue(p.lstrip().startswith("You are the factory routine"))
                self.assertIsNone(R.find_credential(p))

    def test_render_cli_with_cron_and_timezone(self):
        f = make_state(self.root)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = R.main(["render", "kb-refresh", "--state", str(f), "--cron", "15 3 * * 2",
                         "--timezone", "Europe/Berlin"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["trigger"], {"cron": "15 3 * * 2", "timezone": "Europe/Berlin"})

    def test_render_rejects_bad_cron(self):
        f = make_state(self.root)
        with self.assertRaises(R.RoutineError):
            R.render("kb-refresh", f, cron="0 5 * *")

    def test_refuses_missing_ids(self):
        cases = {
            "kb.url": dict(kb_url=None),
            "kb.path": dict(kb_path=None),
            "kb.project_id": dict(kb_pid=None),
            "phases[5].outputs.project_id": dict(board=None),
            "org": dict(org=None),
        }
        for i, (field, kw) in enumerate(cases.items()):
            with self.subTest(field=field):
                d = self.root / str(i)
                f = make_state(d, **kw)
                with self.assertRaises(R.RoutineError) as cm:
                    R.render("kb-refresh", f)
                self.assertIn(field, str(cm.exception))
                err = io.StringIO()
                with redirect_stderr(err), redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(R.main(["render", "kb-refresh", "--state", str(f)]), 1)
                self.assertEqual(out.getvalue(), "")
                self.assertIn(field, err.getvalue())

    def test_refuses_missing_state_file_and_unknown_routine(self):
        with self.assertRaises(R.RoutineError):
            R.render("kb-refresh", self.root / "nope.json")
        f = make_state(self.root)
        with self.assertRaises(R.RoutineError):
            R.render("alert-intake", f)

    def test_refuses_credential_in_state_value(self):
        f = make_state(self.root, kb_path="/home/me/token=" + "abcdefgh12345678")
        with self.assertRaises(R.RoutineError) as cm:
            R.render("kb-refresh", f)
        self.assertIn("credential", str(cm.exception))

    def test_ssh_kb_url(self):
        f = make_state(self.root, kb_url="git@github.com:acme/knowledge.git")
        self.assertIn("gh repo view acme/knowledge ", R.render("kb-refresh", f)["prompt"])


if __name__ == "__main__":
    unittest.main()
