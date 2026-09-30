#!/usr/bin/env python3
"""Unit tests for intake.py (stdlib unittest). Run: python3 factory/scripts/test_intake.py

`gh` is a fake executable (fixtures/fake_gh_runs/gh) put first on PATH; the KB
is written with kb.py's own writers so the fixture format cannot drift.
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
import intake as I  # noqa: E402
import kb as K  # noqa: E402

FAKE_GH = HERE / "fixtures" / "fake_gh_runs"
T0 = "2026-09-30T06:00:00Z"


def run(runid, wf, branch, sha, created, repo="acme/api"):
    return {"databaseId": runid, "workflowName": wf, "headBranch": branch, "headSha": sha,
            "url": f"https://github.com/{repo}/actions/runs/{runid}", "createdAt": created,
            "displayTitle": "fix things", "event": "push", "conclusion": "failure"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._env = dict(os.environ)
        os.environ["PATH"] = f"{FAKE_GH}{os.pathsep}{os.environ['PATH']}"
        os.environ["FAKE_GH_RUNS"] = str(self.tmp / "runs.json")
        os.environ["FAKE_GH_LOG"] = str(self.tmp / "gh.log")
        os.environ["INTAKE_NOW"] = "2026-09-30T08:00:00Z"
        os.environ.pop("KB_DIR", None)
        self.kb = self.tmp / "kb"
        self.make_kb()
        self.graph = self.tmp / "graph.json"
        self.graph.write_text(json.dumps({"repos": [
            {"full_name": "acme/api", "default_branch": "main"},
            {"full_name": "acme/web", "default_branch": "main"},
            {"full_name": "acme/stray", "default_branch": "main"},   # in no KB system
            {"full_name": "other/api"},                               # other org: ignored
        ]}))
        self.state = self.tmp / "state.json"  # absent by default
        self.set_runs({"acme/api": [], "acme/web": [], "acme/stray": []})

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_kb(self):
        (self.kb / "systems").mkdir(parents=True)
        api = K.new_system("api", "active")
        api.meta["repos"] = ["acme/api", "acme/api-worker"]
        api.meta["runtime"] = {"environments": [
            {"name": "prod", "id": "api-prod", "url": "https://api.acme.dev", "source": "user"}]}
        api.meta["monitoring"] = [{"kind": "sentry", "org": "acme", "project": "api-backend", "source": ["user"]}]
        K.save_system(self.kb, api)
        web = K.new_system("web", "active")
        web.meta["repos"] = ["acme/web"]
        K.save_system(self.kb, web)
        conns = K.Connections([{"from": "web", "to": "external:payments.stripe-proxy.internal", "protocol": "https",
                                "auth": "?", "source": "user", "verified": "2026-09-30"}])
        (self.kb / "connections.md").write_text(conns.render())

    def set_runs(self, runs):
        Path(os.environ["FAKE_GH_RUNS"]).write_text(json.dumps(runs))

    def cli(self, *args, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = I.main(list(args), stdin=io.StringIO(stdin))
        return code, (json.loads(out.getvalue()) if out.getvalue().strip() else None), err.getvalue()

    def ci(self, since, *extra):
        code, out, err = self.cli("ci", "--org", "acme", "--since", since, "--graph", str(self.graph),
                                  "--state", str(self.state), "--kb", str(self.kb), *extra)
        self.assertEqual(code, 0, err)
        return out

    def alerts(self, source, data, since=T0, *extra):
        code, out, err = self.cli("alerts", "--source", source, "--since", since, "--kb", str(self.kb), *extra,
                                  stdin=json.dumps(data))
        self.assertEqual(code, 0, err)
        return out


class CiTests(Base):
    def test_dedupe_across_two_polls_and_cursor_advance(self):
        r1 = run(1, "ci", "main", "a" * 40, "2026-09-30T07:00:00Z")
        r1_rerun = dict(run(2, "ci", "main", "a" * 40, "2026-09-30T07:10:00Z"))  # same wf+branch+sha
        self.set_runs({"acme/api": [r1, r1_rerun], "acme/web": [], "acme/stray": []})
        first = self.ci(T0)
        self.assertEqual(len(first["issues"]), 1, "re-run of the same wf+branch+sha is one issue")
        self.assertEqual(first["duplicates"], 1)
        c1 = first["cursor"]
        self.assertTrue(c1.startswith("2026-09-30T08:00:00Z;seen="), c1)
        issue = first["issues"][0]
        self.assertEqual(issue["labels"], ["ci-failure"])
        self.assertEqual(issue["system"], "api")
        self.assertIn("https://github.com/acme/api/actions/runs/1", issue["body"])
        self.assertIn("KB system: `api`", issue["body"])
        self.assertIs(issue["auto_run"], False)

        # second poll an hour later: old failure still inside the lookback window, plus a new one
        os.environ["INTAKE_NOW"] = "2026-09-30T09:00:00Z"
        r3 = run(3, "ci", "main", "b" * 40, "2026-09-30T08:30:00Z")
        self.set_runs({"acme/api": [r1, r1_rerun, r3], "acme/web": [], "acme/stray": []})
        second = self.ci(c1)
        self.assertEqual([i["head_sha"] for i in second["issues"]], ["b" * 40])
        self.assertEqual(second["duplicates"], 2)
        self.assertTrue(second["cursor"].startswith("2026-09-30T09:00:00Z"))
        self.assertGreater(second["cursor"], c1)
        # the cursor keeps the old key so a third poll still dedupes both
        os.environ["INTAKE_NOW"] = "2026-09-30T09:05:00Z"
        third = self.ci(second["cursor"])
        self.assertEqual(third["issues"], [])
        # gh was asked for runs since cursor - lookback, per repo, in the org only
        log = Path(os.environ["FAKE_GH_LOG"]).read_text().splitlines()
        self.assertIn("acme/api >=2026-09-30T02:00:00Z", log)
        self.assertFalse(any(line.startswith("other/") for line in log))

    def test_unknown_repo_maps_to_unknown_not_crash(self):
        self.set_runs({"acme/api": [], "acme/web": [], "acme/stray": [run(9, "build", "main", "c" * 40,
                                                                           "2026-09-30T07:00:00Z", "acme/stray")]})
        out = self.ci(T0, "--repos", "acme/ghost")  # ghost: gh 404
        self.assertEqual(len(out["issues"]), 1)
        self.assertEqual(out["issues"][0]["system"], "unknown")
        self.assertIn("KB system: `unknown`", out["issues"][0]["body"])
        self.assertTrue(any(w.startswith("acme/ghost:") for w in out["warnings"]), out["warnings"])

    def test_missing_kb_and_graph_degrade(self):
        shutil.rmtree(self.kb)
        self.graph.unlink()
        self.state.write_text(json.dumps({"repos": {"api": {"url": "https://github.com/acme/api"}}}))
        self.set_runs({"acme/api": [run(1, "ci", "main", "a" * 40, "2026-09-30T07:00:00Z")]})
        out = self.ci(T0)
        self.assertEqual(out["polled"], 1, "repos fall back to factory state")
        self.assertEqual(out["issues"][0]["system"], "unknown")
        self.assertTrue(any("no KB systems/" in w for w in out["warnings"]))

    def test_default_branch_only(self):
        self.set_runs({"acme/api": [run(1, "ci", "feat/x", "a" * 40, "2026-09-30T07:00:00Z"),
                                    run(2, "ci", "main", "d" * 40, "2026-09-30T07:00:00Z")],
                       "acme/web": [], "acme/stray": []})
        self.assertEqual(len(self.ci(T0)["issues"]), 2)
        only = self.ci(T0, "--default-branch-only")
        self.assertEqual([i["branch"] for i in only["issues"]], ["main"])

    def test_empty_cursor_is_last_24h(self):
        self.ci("")
        log = Path(os.environ["FAKE_GH_LOG"]).read_text()
        self.assertIn(">=2026-09-29T02:00:00Z", log)  # now - 24h - 6h lookback

    def test_bad_cursor_is_usage_error(self):
        code, out, err = self.cli("ci", "--org", "acme", "--since", "yesterday-ish", "--graph", str(self.graph))
        self.assertEqual(code, 2)
        self.assertIn("--since", err)


class AllowlistTests(Base):
    def write(self, rules):
        p = self.tmp / "allow.json"
        p.write_text(json.dumps({"version": 1, "auto_run": rules}))
        return str(p)

    def test_only_exact_system_and_label_match(self):
        allow = self.write([{"system": "api", "label": "ci-failure"}])
        self.set_runs({"acme/api": [run(1, "ci", "main", "a" * 40, "2026-09-30T07:00:00Z")],
                       "acme/web": [run(2, "ci", "main", "e" * 40, "2026-09-30T07:00:00Z", "acme/web")],
                       "acme/stray": []})
        out = self.ci(T0, "--allowlist", allow)
        by = {i["system"]: i["auto_run"] for i in out["issues"]}
        self.assertEqual(by, {"api": True, "web": False})
        # same system, other label -> false
        al = self.alerts("sentry", [{"id": "1", "title": "boom", "project": {"slug": "api-backend"},
                                     "lastSeen": "2026-09-30T07:00:00Z", "status": "unresolved"}],
                         T0, "--allowlist", allow)
        self.assertEqual(al["issues"][0]["system"], "api")
        self.assertIs(al["issues"][0]["auto_run"], False)

    def test_no_allowlist_means_never_auto_run(self):
        self.set_runs({"acme/api": [run(1, "ci", "main", "a" * 40, "2026-09-30T07:00:00Z")],
                       "acme/web": [], "acme/stray": []})
        self.assertIs(self.ci(T0)["issues"][0]["auto_run"], False)

    def test_wildcards_unknown_and_bad_labels_refused(self):
        for rules in ([{"system": "*", "label": "alert"}], [{"system": "unknown", "label": "alert"}],
                      [{"system": "api", "label": "deploy"}], [{"system": "api"}]):
            code, _, err = self.cli("ci", "--org", "acme", "--since", T0, "--graph", str(self.graph),
                                    "--allowlist", self.write(rules))
            self.assertEqual(code, 2, rules)
            self.assertIn("--allowlist", err)


class AlertTests(Base):
    def test_sentry_dedupe_by_fingerprint_and_mapping(self):
        data = {"issues": [
            {"id": "4501", "shortId": "API-1", "title": "KeyError in checkout", "project": {"slug": "api-backend"},
             "permalink": "https://acme.sentry.io/issues/4501/", "lastSeen": "2026-09-30T07:30:00Z",
             "level": "error", "status": "unresolved"},
            {"id": "4501", "title": "KeyError in checkout (again)", "project": {"slug": "api-backend"},
             "lastSeen": "2026-09-30T07:40:00Z", "status": "unresolved"},
            {"id": "4502", "title": "old one", "project": "api-backend", "lastSeen": "2026-09-29T01:00:00Z",
             "status": "unresolved"},
            {"id": "4503", "title": "fixed", "project": "api-backend", "lastSeen": "2026-09-30T07:00:00Z",
             "status": "resolved"},
        ]}
        first = self.alerts("sentry", data)
        self.assertEqual([i["fingerprint"] for i in first["issues"]], ["4501"])
        i = first["issues"][0]
        self.assertEqual((i["system"], i["labels"], i["priority"]), ("api", ["alert"], "high"))
        self.assertIn("https://acme.sentry.io/issues/4501/", i["body"])
        os.environ["INTAKE_NOW"] = "2026-09-30T08:15:00Z"
        second = self.alerts("sentry", data, first["cursor"])
        self.assertEqual(second["issues"], [])
        self.assertGreater(second["cursor"], first["cursor"])

    def test_mcp_tool_result_envelope(self):
        inner = [{"id": "P1", "incident_key": "db-latency", "title": "DB latency", "status": "triggered",
                  "urgency": "high", "service": {"summary": "api-prod"}, "created_at": "2026-09-30T07:00:00Z",
                  "html_url": "https://acme.pagerduty.com/incidents/P1"}]
        out = self.alerts("pagerduty", {"content": [{"type": "text", "text": json.dumps({"incidents": inner})}]})
        self.assertEqual(out["issues"][0]["system"], "api", "env id api-prod -> api")
        self.assertEqual(out["issues"][0]["fingerprint"], "db-latency")

    def test_datadog_host_and_connections_mapping(self):
        data = [
            {"id": 1, "monitor_id": 77, "title": "5xx high", "tags": ["service:web-prod", "env:prod"],
             "alert_type": "error", "date_happened": 1790751600},
            {"id": 2, "monitor_id": 78, "title": "proxy down", "tags": ["host:payments.stripe-proxy.internal"],
             "alert_type": "error", "date_happened": 1790751600},
            {"id": 3, "monitor_id": 79, "title": "recovered", "tags": ["service:web"], "alert_type": "success",
             "date_happened": 1790751600},
        ]
        out = self.alerts("datadog", data)
        self.assertEqual(sorted(i["system"] for i in out["issues"]), ["web", "web"])
        self.assertEqual(len(out["issues"]), 2, "recovered monitors are not filed")

    def test_grafana_unknown_service_is_unknown(self):
        data = [{"fingerprint": "abc123", "labels": {"alertname": "DiskFull", "instance": "db9.corp:9100",
                                                     "severity": "warning"},
                 "annotations": {"summary": "disk 95%"}, "status": {"state": "active"},
                 "startsAt": "2026-09-30T07:00:00Z"}]
        out = self.alerts("grafana", data)
        self.assertEqual(out["issues"][0]["system"], "unknown")
        self.assertEqual(out["issues"][0]["priority"], "medium")

    def test_alert_without_fingerprint_is_warning(self):
        out = self.alerts("grafana", [{"labels": {}, "annotations": {}, "state": "firing"}])
        # labels json is the fallback fingerprint, so this still files once
        self.assertEqual(len(out["issues"]), 1)
        out = self.alerts("sentry", [{"title": "no id", "status": "unresolved"}])
        self.assertEqual(out["issues"], [])
        self.assertTrue(out["warnings"])

    def test_secrets_redacted(self):
        secrets = {
            "aws": "AKIAABCDEFGHIJKLMNOP",
            "gh": "ghp_" + "a" * 36,
            "slack": "xoxb-1234567890-abcdefghij",
            "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
            "bearer": "Bearer abcdefghijklmnopqrstuvwxyz012345",
            "pw": "hunter2hunter2",
            "urlpw": "s3cr3tpass",
            "qs": "QSVALUE123456",
        }
        detail = (f"failed with key {secrets['aws']} and {secrets['gh']}; slack {secrets['slack']}; "
                  f"jwt {secrets['jwt']}; header Authorization: {secrets['bearer']}; password={secrets['pw']}; "
                  f"db postgres://admin:{secrets['urlpw']}@db.acme.dev/app; "
                  f"cb https://hooks.acme.dev/x?token={secrets['qs']}&a=1")
        data = [{"fingerprint": "leaky", "labels": {"alertname": f"Leak {secrets['gh']}", "service": "api"},
                 "annotations": {"summary": f"token leak {secrets['aws']}", "description": detail},
                 "generatorURL": f"https://grafana.acme.dev/alerting?api_key={secrets['qs']}",
                 "status": "firing", "startsAt": "2026-09-30T07:00:00Z"}]
        out = self.alerts("grafana", data)
        blob = json.dumps(out)
        for name, value in secrets.items():
            self.assertNotIn(value.split(" ")[-1], blob, name)
        self.assertIn("[REDACTED", blob)
        self.assertEqual(out["issues"][0]["system"], "api")
        self.assertIn("db.acme.dev", blob, "hostnames are allowed, only credentials are removed")

    def test_ci_titles_redacted_too(self):
        r = run(1, "ci", "main", "a" * 40, "2026-09-30T07:00:00Z")
        r["displayTitle"] = "oops committed ghp_" + "b" * 36
        self.set_runs({"acme/api": [r], "acme/web": [], "acme/stray": []})
        self.assertNotIn("b" * 36, json.dumps(self.ci(T0)))

    def test_not_json_is_usage_error(self):
        code, _, err = self.cli("alerts", "--source", "sentry", "--since", T0, stdin="<html>")
        self.assertEqual(code, 2)
        self.assertIn("not JSON", err)


class SafetyTests(unittest.TestCase):
    def test_never_calls_project_run_or_writes(self):
        src = (HERE / "intake.py").read_text()
        for forbidden in ("project_run", "project_update", "set-routine\"", "automation_create", "\"-X\""):
            self.assertNotIn(forbidden, src.split('"""', 2)[2], forbidden)

    def test_cli_entrypoint(self):
        p = subprocess.run([sys.executable, str(HERE / "intake.py"), "alerts", "--source", "sentry",
                            "--since", T0, "--kb", "/nonexistent"], input="[]", capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["issues"], [])


if __name__ == "__main__":
    unittest.main()
