#!/usr/bin/env python3
"""Tests for fanout.py + workflows/factory-fanout.js (offline; needs `node` for the workflow harness).

Run: python3 factory/scripts/test_fanout.py
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fanout as F  # noqa: E402
import state as S  # noqa: E402

FIX = json.loads((HERE / "fixtures" / "fanout-6.json").read_text())
HARNESS = HERE / "test_fanout_workflow.mjs"
NODE = shutil.which("node")


def cli(mod, path: Path, *args: str):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(["--file", str(path), *args])
    return code, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.state = self.dir / ".agents" / "factory-state.json"
        S.init(self.state, "acme")
        for r in FIX["repos"]:
            S.set_repo(self.state, r, "clone", "pending", repo_path=f"/ws/{r}", url=f"https://github.com/acme/{r}")

    def tearDown(self):
        self.tmp.cleanup()

    def args(self, stage, mark=True):
        return F.build_args(self.state, stage, "/ws", "/ws/knowledge", "2026-09-30", mark=mark)

    def run_workflow(self, args, answers=None, kill_after=None, budget=None):
        a, b = self.dir / "args.json", self.dir / "answers.json"
        a.write_text(json.dumps(args))
        b.write_text(json.dumps(answers if answers is not None else FIX["answers"]))
        cmd = [NODE, str(HARNESS), str(a), str(b)]
        if kill_after is not None:
            cmd += ["--kill-after", str(kill_after)]
        if budget is not None:
            cmd += ["--budget", str(budget)]
        return json.loads(subprocess.run(cmd, check=True, capture_output=True, text=True).stdout)

    def stages(self):
        st = json.loads(self.state.read_text())
        return {n: r["stages"] for n, r in st["repos"].items()}

    def record_report(self, stage, report):
        return F.record(self.state, stage, F.events_from_report(report), report.get("connections"))


class TestPlanRoute(unittest.TestCase):
    def test_route_threshold(self):
        self.assertEqual(F.route(0), "none")
        self.assertEqual(F.route(3), "task-batch")
        self.assertEqual(F.route(4), "workflow")

    def test_plan_50_repos_fits_full_hour(self):
        p = F.plan(50)
        self.assertEqual(p["per_repo_calls"], 11)
        self.assertTrue(p["fits"])
        self.assertEqual(p["chunks"], [50])

    def test_plan_chunks_when_low(self):
        p = F.plan(50, remaining=720)
        self.assertFalse(p["fits"])
        self.assertEqual(p["chunk_size"], 20)
        self.assertEqual(p["chunks"], [20, 20, 10])

    def test_plan_exhausted(self):
        self.assertEqual(F.plan(5, remaining=100)["chunks"], [])


class TestArgs(Base):
    def test_bootstrap_args_all_repos(self):
        a = self.args("bootstrap")
        self.assertEqual([r["name"] for r in a["repos"]], sorted(FIX["repos"]))
        self.assertEqual(a["route"], "workflow")
        self.assertTrue(all(r["todo"] == ["clone"] for r in a["repos"]))
        self.assertEqual(set(v["clone"] for v in self.stages().values()), {"in_progress"})

    def test_research_needs_clone(self):
        self.assertEqual(self.args("research", mark=False)["repos"], [])
        S.set_repo(self.state, "api", "clone", "done")
        a = self.args("research", mark=False)
        self.assertEqual([r["name"] for r in a["repos"]], ["api"])
        self.assertEqual(a["route"], "task-batch")

    def test_missing_path_refused(self):
        S.set_repo(self.state, "nopath", "clone", "pending")
        with self.assertRaises(F.FanoutError):
            self.args("bootstrap")

    def test_no_state(self):
        code, _, err = cli(F, self.dir / "none.json", "args", "--stage", "bootstrap", "--workspace", "/ws", "--kb", "/k")
        self.assertEqual(code, 1)
        self.assertIn("no state", err)


@unittest.skipUnless(NODE, "node not installed")
class TestWorkflow(Base):
    def test_full_phase_2_and_3(self):
        out = self.run_workflow(self.args("bootstrap"))
        self.assertIsNone(out["error"])
        rep = out["report"]
        self.assertEqual(rep["counts"], {"repos": 6, "ok": 6, "failed": 0})
        self.assertTrue(all(c["model"] == "fast" and c["effort"] == "low" and c["hasSchema"] for c in out["calls"]))
        self.assertTrue(all(c["cwd"] == "/ws" for c in out["calls"]))
        self.record_report("bootstrap", rep)
        self.assertEqual(set(v["clone"] for v in self.stages().values()), {"done"})

        out = self.run_workflow(self.args("research"))
        rep = out["report"]
        self.assertEqual(rep["counts"]["ok"], 6)
        fast = [c for c in out["calls"] if c["model"] == "fast"]
        smart = [c for c in out["calls"] if c["model"] != "fast"]
        self.assertEqual(len(fast), 18)
        self.assertEqual([c["label"] for c in smart], ["connections"])
        self.assertTrue(all(c["effort"] == "low" and c["cwd"].startswith("/ws/") for c in fast))
        r = self.record_report("research", rep)
        self.assertEqual(len(r["done"]), 18)
        self.assertTrue(all(set(v.values()) == {"done"} for v in self.stages().values()))
        self.assertTrue((self.state.parent / "factory-fanout" / "connections.json").exists())
        self.assertEqual(self.args("research")["repos"], [])

    def test_progress_log_per_repo(self):
        out = self.run_workflow(self.args("bootstrap"))
        lines = [p["message"] for p in out["progress"] if p["message"].startswith("FANOUT ")]
        self.assertEqual(sorted(json.loads(l[7:])["repo"] for l in lines), sorted(FIX["repos"]))

    def test_kill_and_resume_refans_only_unfinished(self):
        self.record_report("bootstrap", self.run_workflow(self.args("bootstrap"))["report"])
        killed = self.run_workflow(self.args("research"), kill_after=8)
        self.assertTrue(killed["killed"])
        self.assertIsNone(killed["report"])
        log = json.dumps({"progress": killed["progress"]})
        events = F.events_from_log(log)
        r = F.record(self.state, "research", events)
        # Agents in flight at the kill are lost; only logged stages count.
        n_done = len(r["done"])
        self.assertEqual(n_done, len([e for e in events if e["status"] == "done"]))
        self.assertGreater(n_done, 0)
        self.assertLess(n_done, 18)
        st = self.stages()
        self.assertNotIn("in_progress", {s for v in st.values() for s in v.values()})
        a = self.args("research")
        todo = {x["name"]: x["todo"] for x in a["repos"]}
        finished = {f"{n}:{s}" for n, v in st.items() for s, x in v.items() if x == "done" and s != "clone"}
        self.assertEqual(finished, set(r["done"]))
        for name, stages in todo.items():
            self.assertFalse({f"{name}:{s}" for s in stages} & finished)
        resumed = self.run_workflow(a)
        n_fast = sum(1 for c in resumed["calls"] if c["model"] == "fast")
        self.assertEqual(n_fast, 18 - n_done)
        self.record_report("research", resumed["report"])
        self.assertTrue(all(set(v.values()) == {"done"} for v in self.stages().values()))
        # Draft inputs of resumed repos came from artifacts of the killed run.
        for x in a["repos"]:
            if "draft" in x["todo"] and "graph" not in x["todo"]:
                self.assertIn("graph", x["prior"])

    def test_failed_stage_blocks_repo_and_stops_its_pipeline(self):
        self.record_report("bootstrap", self.run_workflow(self.args("bootstrap"))["report"])
        ans = dict(FIX["answers"])
        del ans["graph:web"]
        out = self.run_workflow(self.args("research"), answers=ans)
        rep = out["report"]
        self.assertEqual(rep["repos"]["web"]["status"], "failed")
        self.assertEqual(rep["repos"]["web"]["failed_stage"], "graph")
        self.assertNotIn("draft:web", [c["label"] for c in out["calls"]])
        self.record_report("research", rep)
        self.assertEqual(self.stages()["web"], {"clone": "done", "survey": "done", "graph": "blocked", "draft": "pending"})

    def test_budget_guard(self):
        out = self.run_workflow(self.args("bootstrap"), budget=3000 + 6000)
        rep = out["report"]
        self.assertEqual(len(out["calls"]), 4)
        self.assertEqual(rep["counts"]["failed"], 2)
        self.record_report("bootstrap", rep)
        self.assertEqual(len(self.args("bootstrap", mark=False)["repos"]), 2)

    def test_task_batch_merge_identical(self):
        """Task-batch path (each sub-agent returns a RepoResult) merges to the same report."""
        self.record_report("bootstrap", self.run_workflow(self.args("bootstrap"))["report"])
        a = self.args("research")
        wf = self.run_workflow(a)["report"]
        ans = FIX["answers"]
        files = []
        for r in reversed(a["repos"]):  # arbitrary completion order
            rr = {"name": r["name"], "status": "ok", "failed_stage": None,
                  "results": {s: ans[f"{s}:{r['name']}"] for s in r["todo"]}}
            f = self.dir / f"task-{r['name']}.json"
            f.write_text(json.dumps(rr))
            files.append(str(f))
        conn = self.dir / "conn.json"
        conn.write_text(json.dumps(ans["connections"]))
        code, out, err = cli(F, self.state, "merge", "--stage", "research", "--connections", str(conn), *files)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), wf)


@unittest.skipUnless(NODE, "node not installed")
class TestRecordCli(Base):
    def test_record_log_from_workflow_status_markdown(self):
        md = "## Progress log\n\n- **[Clone]** FANOUT " + json.dumps(
            {"repo": "api", "stage": "clone", "status": "done", "result": FIX["answers"]["clone:api"]}) + "\n"
        f = self.dir / "status.md"
        f.write_text(md)
        code, out, err = cli(F, self.state, "record", "--stage", "bootstrap", "--log", str(f))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["done"], ["api:clone"])
        self.assertEqual(self.stages()["api"]["clone"], "done")

    def test_record_is_idempotent(self):
        rep = self.run_workflow(self.args("bootstrap"))["report"]
        self.record_report("bootstrap", rep)
        r = self.record_report("bootstrap", rep)
        self.assertEqual(r["done"], [])
        self.assertEqual(len(r["ignored"]), 6)

    def test_stage_mismatch_refused(self):
        f = self.dir / "r.json"
        f.write_text(json.dumps({"stage": "research", "repos": {}}))
        code, _, err = cli(F, self.state, "record", "--stage", "bootstrap", "--result", str(f))
        self.assertEqual(code, 1)
        self.assertIn("!=", err)


if __name__ == "__main__":
    unittest.main(verbosity=1)
