#!/usr/bin/env python3
"""Unit tests for routines.py. Run: python3 factory/scripts/test_routines.py"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
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


@unittest.skipUnless(shutil.which("node"), "node not installed")
class RepoHealthWorkflow(unittest.TestCase):
    def run_wf(self, args: dict, answers: dict, *extra: str) -> dict:
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d) / "args.json", Path(d) / "answers.json"
            a.write_text(json.dumps(args)); b.write_text(json.dumps(answers))
            out = subprocess.run(["node", str(HERE / "test_fanout_workflow.mjs"), str(a), str(b),
                                  "--script", "routine-repo-health.js", *extra],
                                 check=True, capture_output=True, text=True)
        return json.loads(out.stdout)

    def test_fanout_fast_readonly_and_merge(self):
        repos = [{"name": f"r{i}", "path": f"/w/r{i}", "url": f"https://github.com/acme/r{i}"} for i in range(5)]
        f = {"check": "ci", "severity": "high", "title": "CI red", "evidence": "https://x/run/1"}
        answers = {"health:r0": {"repo": "r0", "status": "findings", "findings": [f]},
                   "health:r1": {"repo": "r1", "status": "ok", "findings": []},
                   "health:r2": {"repo": "r2", "status": "ok", "findings": []},
                   "health:r3": {"repo": "r3", "status": "ok", "findings": []}}
        res = self.run_wf({"org": "acme", "date": "2026-09-30", "repos": repos}, answers)
        self.assertIsNone(res["error"])
        self.assertEqual(len(res["calls"]), 5)
        for c in res["calls"]:
            self.assertEqual((c["model"], c["effort"]), ("fast", "low"))
            self.assertTrue(c["hasSchema"]); self.assertTrue(c["cwd"].startswith("/w/r"))
        self.assertEqual(res["report"]["counts"], {"repos": 5, "ok": 3, "findings": 1, "failed": 1, "high": 1})

    def test_prompt_is_read_only(self):
        src = (R.SKILL_DIR / "workflows" / "routine-repo-health.js").read_text()
        self.assertIn("Do NOT push", src)
        self.assertIsNone(R.find_credential(src))


class Drift(unittest.TestCase):
    """routines.py drift on the real kubectl fixture vs a KB built with kb.py."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.kb = self.root / "kb"
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1", "KB_TODAY": "2026-09-30",
               "HOME": str(self.root)}
        self.env = env

        def kb(*a):
            subprocess.run([sys.executable, str(HERE / "kb.py"), *a], check=True, capture_output=True,
                           text=True, env=env)

        kb("init", str(self.kb))
        for s in ("api", "web", "payments", "reporter", "ghost"):
            kb("add-system", s, "--kb", str(self.kb), "--repo", f"acme/{s}", "--source", "user")
        # known: web -> api; stale infra edge: reporter -> payments; human intent: api -> payments
        kb("add-connection", "web", "api", "--kb", str(self.kb), "--protocol", "http", "--source", "user")
        kb("add-connection", "reporter", "payments", "--kb", str(self.kb), "--protocol", "http",
           "--source", "infra:kubernetes:deployment/shop/report")
        kb("add-connection", "api", "payments", "--kb", str(self.kb), "--protocol", "grpc", "--source", "user")
        kb("add-connection", "ghost", "api", "--kb", str(self.kb), "--protocol", "http", "--source", "user")
        self.infra = self.root / "infra.json"
        subprocess.run([sys.executable, str(HERE / "infra_graph.py"), "--platform", "kubernetes", "--input",
                        str(HERE / "fixtures" / "infra_graph" / "kubectl-get.json"), "--org", "acme",
                        "--out", str(self.infra)], check=True, capture_output=True, text=True)
        self.head = subprocess.run(["git", "-C", str(self.kb), "rev-parse", "HEAD"], capture_output=True,
                                   text=True).stdout

    def tearDown(self):
        self.tmp.cleanup()

    def test_drift(self):
        d = R.drift(self.infra, self.kb)
        self.assertEqual(d["platforms"], ["kubernetes"])
        live = {(x["from"], x["to"]) for x in d["live_only"]}
        self.assertIn(("reporter", "api"), live)
        self.assertIn(("web", "payments"), live)
        self.assertIn(("external:shop.example.com", "api"), live)
        self.assertNotIn(("web", "api"), live)                      # already in the KB
        self.assertFalse(any(x["to"].startswith("infra:") for x in d["live_only"]))  # unmapped cache skipped
        self.assertEqual([(x["from"], x["to"]) for x in d["kb_only"]], [("reporter", "payments")])
        # user rows are never kb_only (humans own them); api->payments is intended but not live,
        # ghost is not deployed on the platform read, so it is not flagged
        self.assertEqual([(x["from"], x["to"]) for x in d["intended_missing"]], [("api", "payments")])
        self.assertEqual([x["resource"] for x in d["unmapped"]], ["infra:kubernetes:statefulset/shop/cache"])
        self.assertEqual(d["counts"]["kb_only"], 1)
        keys = [x["key"] for k in ("live_only", "kb_only", "unmapped", "intended_missing") for x in d[k]]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(R.drift(self.infra, self.kb), d)  # stable keys
        # read-only: the KB is untouched
        self.assertEqual(subprocess.run(["git", "-C", str(self.kb), "status", "--porcelain"], capture_output=True,
                                        text=True).stdout, "")
        self.assertEqual(subprocess.run(["git", "-C", str(self.kb), "rev-parse", "HEAD"], capture_output=True,
                                        text=True).stdout, self.head)

    def test_cli_and_errors(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(R.main(["drift", "--infra", str(self.infra), "--kb", str(self.kb)]), 0)
        self.assertIn("live_only", json.loads(out.getvalue()))
        with redirect_stderr(io.StringIO()):
            self.assertEqual(R.main(["drift", "--infra", str(self.root / "nope.json"), "--kb", str(self.kb)]), 1)
            self.assertEqual(R.main(["drift", "--infra", str(self.infra), "--kb", str(self.root)]), 1)


if __name__ == "__main__":
    unittest.main()
