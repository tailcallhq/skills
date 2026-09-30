#!/usr/bin/env python3
"""Tests for infra_plan.py. Run: python3 factory/scripts/test_infra_plan.py

terraform / kubectl / helm / flyctl are fake executables on PATH
(fixtures/infra_plan/bin); the KB is a real `kb.py init` checkout and the
state file is driven through state.py.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import infra_plan as P  # noqa: E402
import kb as K  # noqa: E402
import state as S  # noqa: E402

FX = HERE / "fixtures" / "infra_plan"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1", "KB_TODAY": "2026-09-30"}


def quiet(fn, *args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(list(args))
    return code, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    envs = ("staging", "prod")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "ws"
        self.root.mkdir()
        self.fake = self.tmp / "fake"
        self.fake.mkdir()
        self.kb = self.tmp / "knowledge"
        self.state = self.root / ".agents" / "factory-state.json"
        self._env = dict(os.environ)
        for k in list(os.environ):
            if "_WRITE_" in k or k in ("FACTORY_STATE", "KB_DIR"):
                del os.environ[k]
        os.environ.update(GIT_ENV)
        os.environ["PATH"] = f"{FX / 'bin'}{os.pathsep}{os.environ['PATH']}"
        os.environ["FAKE_INFRA"] = str(self.fake)
        os.environ["AWS_WRITE_STAGING"] = "stg-profile"
        os.environ["AWS_WRITE_PROD"] = "prod-profile"
        os.environ["K8S_WRITE_STAGING"] = "/kube/stg"
        os.environ["K8S_WRITE_PROD"] = "/kube/prod"
        self.assertEqual(quiet(S.main, "--file", str(self.state), "init", "--org", "acme")[0], 0)
        self.assertEqual(quiet(K.main, "init", str(self.kb))[0], 0)
        kb = ("--kb", str(self.kb))
        quiet(K.main, "add-system", "api", "--purpose", "orders API", *kb)
        for sysname in ("web", "billing"):
            quiet(K.main, "add-system", sysname, *kb)
        for env in self.envs:
            self.assertEqual(quiet(K.main, "add-env", "api", env, "--id", f"api-{env}", "--platform", "aws",
                                   "--url", f"https://api.{env}.acme.dev", "--source", "user", *kb)[0], 0)
        quiet(K.main, "add-connection", "web", "api", "--protocol", "http", "--source", "user", *kb)
        quiet(K.main, "add-connection", "api", "billing", "--protocol", "grpc", "--source", "user", *kb)
        quiet(K.main, "add-monitor", "api", "--kind", "sentry", "--org", "acme", "--project", "api",
              "--source", "user", *kb)
        self.src = self.tmp / "change"
        (self.src / "infra" / "api").mkdir(parents=True)
        (self.src / "infra" / "api" / "main.tf").write_text('resource "aws_sqs_queue" "jobs" {}\n')
        (self.src / "infra" / "api" / "staging.tfvars").write_text("size = 1\n")
        (self.src / "k8s").mkdir()
        (self.src / "k8s" / "deploy.yaml").write_text("kind: Deployment\n")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers
    def change(self, platform="terraform", cid="chg-api-1", **extra) -> Path:
        spec = {"id": cid, "system": "api", "summary": "test change", "platform": platform}
        if platform == "terraform":
            spec.update({"credential_platform": "AWS", "credential_env": "AWS_PROFILE",
                         "cross_env_probe": {"argv": ["aws", "sqs", "create-queue", "--queue-name", "probe",
                                                      "--profile-target", "{target}"],
                                             "allowed_pattern": "QueueUrl"},
                         "terraform": {"dir": "infra/api", "var_file": {"staging": "infra/api/staging.tfvars"},
                                       "workspace": {"staging": "staging", "prod": "prod"}}})
        elif platform == "kubernetes":
            spec.update({"credential_platform": "K8S",
                         "kubernetes": {"manifests": "k8s", "namespace": "api",
                                        "context": {"staging": "stg-cluster", "prod": "prod-cluster"}}})
        elif platform == "helm":
            spec.update({"credential_platform": "K8S",
                         "helm": {"release": "api", "chart": "charts/api", "namespace": "api",
                                  "context": {"staging": "stg-cluster", "prod": "prod-cluster"}}})
        spec.update(extra)
        p = self.src / f"{cid}.json"
        p.write_text(json.dumps(spec))
        return p

    def fx(self, name, as_name):
        shutil.copy(FX / name, self.fake / as_name)

    def run_plan(self, platform, env, change):
        code, out, err = quiet(P.main, "--platform", platform, "--env", env, "--change", str(change),
                               "--kb", str(self.kb), "--state", str(self.state), "--root", str(self.root))
        return code, (json.loads(out) if out.strip() else None), err

    def ok(self, platform, env, change):
        code, out, err = self.run_plan(platform, env, change)
        self.assertEqual(code, 0, err)
        return out

    def calls(self) -> str:
        p = self.fake / "calls.log"
        return p.read_text() if p.exists() else ""

    def sc(self, cid, step, status, *extra):
        code, _, err = quiet(S.main, "--file", str(self.state), "set-change", cid, step, status, *extra)
        self.assertEqual(code, 0, err)

    def staging_verified(self, cid, out, scope="staging+prod"):
        self.sc(cid, "classified", "done", "--platform", out["platform"], "--classification",
                out["classification"], "--env-scope", scope)
        self.sc(cid, "planned_staging", "done", "--plan-hash", out["plan_hash"],
                "--inverse-plan-path", out["inverse_plan_path"])
        for step in ("approved_staging", "applied_staging", "verified_staging"):
            self.sc(cid, step, "done")


class TestClassification(Base):
    def tf(self, fixture):
        self.fx(fixture, "tf_show.json")
        return self.ok("terraform", "staging", self.change())

    def test_additive(self):
        out = self.tf("tf_additive.json")
        self.assertEqual(out["classification"], "additive")
        self.assertEqual([r["action"] for r in out["resources"]], ["create", "create"])  # no-op dropped
        self.assertNotIn("confirm_by_typing", out)

    def test_mutating(self):
        out = self.tf("tf_mutating.json")
        self.assertEqual(out["classification"], "mutating")

    def test_destructive_delete_and_replace(self):
        out = self.tf("tf_destructive.json")
        self.assertEqual(out["classification"], "destructive")
        acts = {r["address"]: r["action"] for r in out["resources"]}
        self.assertEqual(acts, {"aws_db_instance.main": "replace", "aws_sqs_queue.old": "delete"})
        self.assertEqual(out["confirm_by_typing"], ["main", "old"])

    def test_iam_widening_is_destructive(self):
        for fx in ("tf_iam_widening.json", "tf_iam_create.json"):
            out = self.tf(fx)
            self.assertEqual(out["classification"], "destructive", fx)
            self.assertTrue(any("IAM" in r for r in out["reasons"]), out["reasons"])

    def test_traffic_update_is_destructive(self):
        self.assertEqual(self.tf("tf_traffic.json")["classification"], "destructive")

    def test_k8s_fixtures(self):
        cases = {"k8s_additive.diff": "additive", "k8s_mutating.diff": "mutating",
                 "k8s_destructive.diff": "destructive", "k8s_rbac.diff": "destructive"}
        for fx, want in cases.items():
            self.fx(fx, "kubectl_diff.txt")
            out = self.ok("kubernetes", "staging", self.change("kubernetes"))
            self.assertEqual(out["classification"], want, fx)
        self.assertTrue(any("0 replicas" in r for r in P.overall(P.kubectl_diff_resources(
            (FX / "k8s_destructive.diff").read_text()))[1]))

    def test_unknown_action_is_destructive(self):
        self.assertEqual(P.classify_resource("aws_thing", "wobble")[0], "destructive")

    def test_helm(self):
        self.fx("helm_mutating.diff", "helm_diff.txt")
        (self.fake / "helm_history.json").write_text('[{"revision": 7, "status": "deployed"}]')
        out = self.ok("helm", "staging", self.change("helm"))
        self.assertEqual(out["classification"], "mutating")
        self.assertEqual({r["action"] for r in out["resources"]}, {"update", "create"})
        inv = json.loads(Path(out["inverse_plan_path"]).read_text())
        self.assertEqual(inv["steps"][0]["argv"][-3:], ["7", "-n", "api"])


class TestPlanOutput(Base):
    def test_terraform_plan_artifacts_blast_radius_and_checks(self):
        self.fx("tf_mutating.json", "tf_show.json")
        out = self.ok("terraform", "staging", self.change())
        d = self.root / ".agents" / "infra-plans" / "chg-api-1" / "staging"
        self.assertEqual(Path(out["plan_path"]), d / "plan.json")
        self.assertTrue((d / "plan.tfplan").exists())
        self.assertTrue((self.root / ".agents" / "infra-plans" / ".gitignore").exists())
        br = out["blast_radius"]
        self.assertEqual(br["systems"], ["api", "billing", "web"])
        self.assertEqual(br["dependents"], ["web"])
        self.assertEqual(br["dependencies"], ["billing"])
        self.assertEqual(br["environment"]["id"], "api-staging")
        ids = [c["id"] for c in out["verify_checks"]]
        self.assertEqual(ids[:2], ["state_matches_plan", "health"])
        self.assertIn("monitoring_soak", ids)
        self.assertIn("connection:web->api:http", ids)
        self.assertIn("-detailed-exitcode", out["verify_checks"][0]["argv"])
        self.assertTrue(out["plan_hash"].startswith("sha256:"))
        self.assertIn("set-change chg-api-1 planned_staging done", out["state_cmd"])

    def test_native_calls_never_apply_and_use_only_env_credential(self):
        self.fx("tf_additive.json", "tf_show.json")
        os.environ["FAKE_CRED_VAR"] = "AWS_PROFILE"
        self.ok("terraform", "staging", self.change())
        log = self.calls()
        self.assertNotIn(" apply", log)
        self.assertNotIn("auto-approve", log)
        self.assertIn("-out=", log)
        for line in log.splitlines():
            self.assertNotIn("_WRITE_", line.split("| cred=")[1].split(" val=")[0], line)  # write vars stripped
            self.assertTrue(line.endswith("val=stg-profile"), line)                     # only the staging cred

    def test_sensitive_values_and_state_not_in_artifacts(self):
        self.fx("tf_mutating.json", "tf_show.json")
        out = self.ok("terraform", "staging", self.change())
        text = Path(out["plan_path"]).read_text()
        self.assertIn("(sensitive)", text)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("should-not-leak", text)
        self.assertNotIn("stg-profile", json.dumps(out))
        self.assertEqual(out["credential_env_var"], "AWS_WRITE_STAGING")


class TestInverse(Base):
    def test_terraform_inverse(self):
        self.fx("tf_destructive.json", "tf_show.json")
        out = self.ok("terraform", "staging", self.change())
        inv = json.loads(Path(out["inverse_plan_path"]).read_text())
        by = {s["address"]: s for s in inv["steps"]}
        self.assertEqual(by["aws_sqs_queue.old"]["action"], "recreate")
        self.assertEqual(by["aws_sqs_queue.old"]["restore"], {"name": "old"})
        self.assertIn("backup", by["aws_db_instance.main"]["warning"])
        self.fx("tf_additive.json", "tf_show.json")
        out = self.ok("terraform", "staging", self.change())
        inv = json.loads(Path(out["inverse_plan_path"]).read_text())
        step = inv["steps"][0]
        self.assertEqual(step["action"], "delete")
        self.assertIn("-destroy", step["how"])
        self.assertNotIn("-auto-approve", step["how"])
        self.fx("tf_mutating.json", "tf_show.json")
        inv = json.loads(Path(self.ok("terraform", "staging", self.change())["inverse_plan_path"]).read_text())
        self.assertEqual(inv["steps"][0]["restore"]["instance_type"], "t3.small")

    def test_k8s_inverse_snapshots_live_objects(self):
        self.fx("k8s_mutating.diff", "kubectl_diff.txt")
        out = self.ok("kubernetes", "staging", self.change("kubernetes"))
        inv = json.loads(Path(out["inverse_plan_path"]).read_text())
        by = {s["resource"]: s for s in inv["steps"]}
        restore = by["Deployment/api/web"]
        self.assertEqual(restore["action"], "restore")
        snap = Path(restore["manifest"]).read_text()
        self.assertIn("replicas: 3", snap)
        for gone in ("resourceVersion", "uid:", "managedFields", "status:"):
            self.assertNotIn(gone, snap)
        self.assertEqual(restore["argv"][3], "diff")  # diff precedes the restore apply
        created = by["ConfigMap/api/feature-flags"]
        self.assertEqual(created["action"], "delete")
        self.assertIn("diff", created["precondition"])
        self.assertIn("--context", created["argv"])
        self.assertIn("stg-cluster", created["argv"])


class TestRefusals(Base):
    def test_prod_refused_without_staging_record(self):
        self.fx("tf_additive.json", "tf_show.json")
        code, _, err = self.run_plan("terraform", "prod", self.change())
        self.assertEqual(code, 1)
        self.assertIn("verified_staging: done", err)
        self.assertEqual(self.calls(), "")  # refused before any native call

    def test_prod_refused_when_staging_not_yet_verified(self):
        self.fx("tf_additive.json", "tf_show.json")
        ch = self.change()
        out = self.ok("terraform", "staging", ch)
        self.sc("chg-api-1", "classified", "done", "--platform", "terraform", "--classification", "additive",
                "--env-scope", "staging+prod")
        self.sc("chg-api-1", "planned_staging", "done", "--plan-hash", out["plan_hash"],
                "--inverse-plan-path", out["inverse_plan_path"])
        self.sc("chg-api-1", "approved_staging", "done")
        self.sc("chg-api-1", "applied_staging", "done")
        code, _, err = self.run_plan("terraform", "prod", ch)
        self.assertEqual(code, 1)
        self.assertIn("verified_staging", err)

    def test_prod_allowed_after_verified_staging_same_hash_and_diffs_plans(self):
        self.fx("tf_additive.json", "tf_show.json")
        ch = self.change()
        out = self.ok("terraform", "staging", ch)
        self.staging_verified("chg-api-1", out)
        self.fx("tf_destructive.json", "tf_show.json")  # prod differs from staging
        os.environ["FAKE_CRED_VAR"] = "AWS_PROFILE"
        prod = self.ok("terraform", "prod", ch)
        self.assertEqual(prod["env"], "prod")
        self.assertEqual(prod["plan_hash"], out["plan_hash"])
        self.assertEqual(prod["blast_radius"]["environment"]["id"], "api-prod")
        d = prod["plan_diff"]
        self.assertFalse(d["identical"])
        self.assertIn("aws_db_instance.main", d["only_in_prod"])
        self.assertIn("aws_sqs_queue.jobs", d["only_in_staging"])
        self.assertEqual(prod["confirm_by_typing"], ["main", "old"])
        self.assertIn("TF_WORKSPACE", json.dumps(prod["verify_checks"][0]))
        self.assertTrue(self.calls().strip().splitlines()[-1].endswith("val=prod-profile"))

    def test_prod_refused_when_change_edited_after_staging(self):
        self.fx("tf_additive.json", "tf_show.json")
        ch = self.change()
        self.staging_verified("chg-api-1", self.ok("terraform", "staging", ch))
        (self.src / "infra" / "api" / "main.tf").write_text('resource "aws_sqs_queue" "other" {}\n')
        code, _, err = self.run_plan("terraform", "prod", ch)
        self.assertEqual(code, 1)
        self.assertIn("plan hash", err)

    def test_prod_refused_when_ask_was_staging_only(self):
        self.fx("tf_additive.json", "tf_show.json")
        ch = self.change()
        self.staging_verified("chg-api-1", self.ok("terraform", "staging", ch), scope="staging")
        code, _, err = self.run_plan("terraform", "prod", ch)
        self.assertEqual(code, 1)
        self.assertIn("did not include prod", err)

    def test_staging_replan_refused_after_apply(self):
        self.fx("tf_additive.json", "tf_show.json")
        ch = self.change()
        self.staging_verified("chg-api-1", self.ok("terraform", "staging", ch))
        code, _, err = self.run_plan("terraform", "staging", ch)
        self.assertEqual(code, 1)
        self.assertIn("already applied", err)

    def test_cross_env_same_credential_refused(self):
        self.fx("tf_additive.json", "tf_show.json")
        os.environ["AWS_WRITE_PROD"] = "stg-profile"
        code, _, err = self.run_plan("terraform", "staging", self.change())
        self.assertEqual(code, 1)
        self.assertIn("cross-env credential", err)

    def test_cross_env_probe_allowed_refused(self):
        self.fx("tf_additive.json", "tf_show.json")
        (self.fake / "probe.txt").write_text('{"QueueUrl": "https://sqs/probe"}')
        code, _, err = self.run_plan("terraform", "staging", self.change())
        self.assertEqual(code, 1)
        self.assertIn("allowed on prod", err)
        self.assertIn("{target}", (self.src / "chg-api-1.json").read_text())
        self.assertIn("--profile-target api-prod", self.calls())

    def test_k8s_cross_env_can_i_refused(self):
        self.fx("k8s_additive.diff", "kubectl_diff.txt")
        (self.fake / "can_i.prod-cluster").write_text("yes")
        code, _, err = self.run_plan("kubernetes", "staging", self.change("kubernetes"))
        self.assertEqual(code, 1)
        self.assertIn("cross-env credential", err)
        (self.fake / "can_i.prod-cluster").write_text("no")
        self.ok("kubernetes", "staging", self.change("kubernetes"))

    def test_missing_write_credential_and_discovery_reuse_refused(self):
        self.fx("tf_additive.json", "tf_show.json")
        del os.environ["AWS_WRITE_STAGING"]
        code, _, err = self.run_plan("terraform", "staging", self.change())
        self.assertEqual((code, "AWS_WRITE_STAGING" in err), (1, True))
        os.environ["AWS_WRITE_STAGING"] = "shared"
        os.environ["AWS_READ"] = "shared"
        code, _, err = self.run_plan("terraform", "staging", self.change())
        self.assertEqual(code, 1)
        self.assertIn("discovery credential", err)

    def test_unknown_staging_stops(self):
        self.fx("tf_additive.json", "tf_show.json")
        ch = self.change()
        spec = json.loads(ch.read_text())
        spec["system"] = "web"  # web has no environments recorded
        ch.write_text(json.dumps(spec))
        code, _, err = self.run_plan("terraform", "staging", ch)
        self.assertEqual(code, 3)
        self.assertIn("STOP and ask", err)
        spec["system"] = "ghost"
        ch.write_text(json.dumps(spec))
        self.assertEqual(self.run_plan("terraform", "staging", ch)[0], 3)

    def test_forbidden_flags_refused(self):
        self.fx("tf_additive.json", "tf_show.json")
        for bad in ("-auto-approve", "--force"):
            ch = self.change(extra_args=[bad])
            code, _, err = self.run_plan("terraform", "staging", ch)
            self.assertEqual(code, 1, bad)
            self.assertIn("forbidden flag", err)
        with self.assertRaises(P.Refused):
            P.check_argv(["kubectl", "--context", "x", "delete", "deploy", "web"], "imperative.inverse.staging")

    def test_imperative_requires_inverse_and_effects(self):
        im = {"cli": "flyctl", "dry_run": {"staging": ["flyctl", "scale", "count", "2", "--dry-run"]},
              "effects": [{"action": "update", "type": "fly_machine", "name": "api"}]}
        ch = self.change("imperative", credential_platform="FLY", credential_env="FLY_API_TOKEN",
                         cross_env_probe={"argv": ["flyctl", "apps", "create", "{target}-probe"], "allowed_pattern": "created"},
                         imperative=im)
        os.environ["FLY_WRITE_STAGING"] = "fly-stg"
        code, _, err = self.run_plan("imperative", "staging", ch)
        self.assertEqual(code, 1)
        self.assertIn("inverse", err)
        im["inverse"] = {"staging": ["flyctl", "scale", "count", "3"]}
        ch = self.change("imperative", credential_platform="FLY", credential_env="FLY_API_TOKEN",
                         cross_env_probe={"argv": ["flyctl", "apps", "create", "{target}-probe"], "allowed_pattern": "created"},
                         imperative=im)
        out = self.ok("imperative", "staging", ch)
        self.assertEqual(out["classification"], "mutating")
        self.assertEqual(out["verify_checks"][0]["kind"], "manual")

    def test_secret_in_change_file_refused(self):
        ch = self.change(summary="token=ghp_" + "a" * 36)
        code, _, _ = self.run_plan("terraform", "staging", ch)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
