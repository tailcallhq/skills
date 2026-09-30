#!/usr/bin/env python3
"""Unit tests for infra_graph.py (stdlib unittest). Run: python3 factory/scripts/test_infra_graph.py

Offline: recorded `kubectl get -o json`, `terraform show -json` and aws
snapshot fixtures. Live mode is exercised with stub `kubectl` / `aws` binaries
on PATH that emulate an over-privileged, a read-only and a broken credential,
and that record every call so the tests can assert nothing was read after a
refused probe.
"""
from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import infra_graph as G  # noqa: E402

FIX = HERE / "fixtures" / "infra_graph"
REPOS = {"repos": [{"full_name": f"acme/{n}"} for n in ("api", "storefront", "payments", "worker", "reporter")]}


def call(*args: str) -> tuple[int, dict | None, str]:
    """Run main(); return (rc, parsed stdout JSON or None, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = G.main(list(args))
    text = out.getvalue()
    return rc, (json.loads(text) if text.strip() else None), err.getvalue()


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repos = Path(cls.tmp.name) / "graph.json"
        cls.repos.write_text(json.dumps(REPOS))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def offline(self, platform, fixture):
        rc, doc, _ = call("--platform", platform, "--input", str(FIX / fixture), "--repos", str(self.repos))
        self.assertEqual(rc, 0)
        return doc

    @staticmethod
    def edges(doc):
        return {(e["from"], e["to"], e["kind"]) for e in doc["edges"]}


class Kubernetes(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        rc, cls.doc, _ = call("--platform", "kubernetes", "--input", str(FIX / "kubectl-get.json"),
                              "--repos", str(cls.repos))
        assert rc == 0

    def test_env_refs_resolve_through_services(self):
        e = self.edges(self.doc)
        self.assertIn(("acme/storefront", "acme/api", "env"), e)          # http://api.shop:8080
        self.assertIn(("acme/storefront", "acme/payments", "env"), e)     # cross-namespace payments.billing.svc
        self.assertIn(("acme/reporter", "acme/api", "env"), e)            # bare `api:8080` in same ns
        self.assertIn(("acme/api", "infra:kubernetes:statefulset/shop/cache", "env"), e)

    def test_external_dependencies(self):
        self.assertIn(("acme/api", "external:orders-db.abc123.eu-west-1.rds.amazonaws.com", "env"), self.edges(self.doc))

    def test_ingress_and_network_policy(self):
        e = self.edges(self.doc)
        self.assertIn(("external:shop.example.com", "acme/api", "ingress"), e)
        self.assertIn(("external:shop.example.com", "acme/storefront", "ingress"), e)
        self.assertIn(("acme/storefront", "acme/api", "network_policy"), e)

    def test_repo_mapping_by_image_and_label(self):
        r = {x["id"]: x for x in self.doc["resources"]}
        self.assertEqual(r["deployment/shop/api"]["repo"], "acme/api")
        self.assertEqual(r["deployment/shop/api"]["repo_evidence"], "image ghcr.io/acme/api:1.4.2")
        self.assertEqual(r["deployment/shop/web"]["repo"], "acme/storefront")      # ECR image path, then label
        self.assertEqual(r["cronjob/shop/report"]["repo"], "acme/reporter")
        self.assertNotIn("repo", r["statefulset/shop/cache"])                        # redis:7 is not ours

    def test_edge_schema_matches_graph_json(self):
        for e in self.doc["edges"]:
            self.assertTrue({"from", "to", "kind", "evidence", "source"} <= e.keys(), e)
            self.assertRegex(e["source"], r"^infra:kubernetes:[\w./-]+$")
        for r in self.doc["resources"]:
            self.assertEqual(r["source"], f"infra:kubernetes:{r['id']}")

    def test_no_secrets(self):
        blob = json.dumps(self.doc)
        self.assertNotIn("PLANTED", blob)
        self.assertNotIn("secretKeyRef", blob)

    def test_deterministic(self):
        _, again, _ = call("--platform", "kubernetes", "--input", str(FIX / "kubectl-get.json"), "--repos", str(self.repos))
        a, b = dict(self.doc), dict(again)
        a.pop("generated_at"), b.pop("generated_at")
        self.assertEqual(a, b)


class Terraform(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        rc, cls.doc, _ = call("--platform", "terraform", "--input", str(FIX / "terraform-show.json"),
                              "--repos", str(cls.repos))
        assert rc == 0

    def test_workload_to_datastore_edges(self):
        e = self.edges(self.doc)
        self.assertIn(("acme/api", "infra:terraform:aws_db_instance.orders", "env"), e)
        self.assertIn(("acme/api", "infra:terraform:aws_elasticache_replication_group.cache", "env"), e)
        self.assertIn(("acme/api", "infra:terraform:aws_sqs_queue.jobs", "env"), e)
        self.assertIn(("acme/worker", "infra:terraform:aws_db_instance.orders", "env"), e)

    def test_messaging_edges(self):
        e = self.edges(self.doc)
        self.assertIn(("acme/worker", "infra:terraform:aws_sqs_queue.jobs", "consumes"), e)
        self.assertIn(("infra:terraform:aws_sns_topic.orders", "infra:terraform:aws_sqs_queue.jobs", "subscription"), e)

    def test_dns_records(self):
        e = self.edges(self.doc)
        self.assertIn(("external:api.example.com", "infra:terraform:aws_route53_record.api", "dns"), e)
        self.assertIn(("infra:terraform:aws_route53_record.api", "external:shop-alb-123.eu-west-1.elb.amazonaws.com", "dns"), e)

    def test_ecs_service_inherits_repo_from_task_definition(self):
        r = {x["id"]: x for x in self.doc["resources"]}
        self.assertEqual(r["module.api.aws_ecs_service.api"]["repo"], "acme/api")

    def test_sensitive_values_never_copied(self):
        blob = json.dumps(self.doc)
        self.assertNotIn("PLANTED", blob)
        for r in self.doc["resources"]:
            self.assertFalse({"password", "auth_token", "values", "container_definitions"} & r.keys())


class Aws(Base):
    def test_snapshot_edges(self):
        doc = self.offline("aws", "aws-snapshot.json")
        e = self.edges(doc)
        self.assertIn(("acme/api", "infra:aws:sqs/shop-jobs", "env"), e)
        self.assertNotIn(("acme/api", "infra:aws:sqs/shop-dlq", "env"), e)   # same host, different queue
        self.assertIn(("acme/api", "infra:aws:elasticache/shop-cache", "env"), e)
        self.assertIn(("infra:aws:lambda/shop-worker", "infra:aws:rds/orders-db", "env"), e)
        self.assertIn(("infra:aws:lambda/shop-worker", "infra:aws:sqs/shop-jobs", "consumes"), e)
        self.assertIn(("infra:aws:sns/orders", "infra:aws:sqs/shop-jobs", "subscription"), e)
        self.assertIn(("infra:aws:route53/api.example.com/A", "external:shop-alb-123.eu-west-1.elb.amazonaws.com", "dns"), e)
        self.assertNotIn("secretsmanager", json.dumps(doc))


class Merge(Base):
    def test_out_merges_per_platform_and_keeps_others(self):
        out = Path(self.tmp.name) / "infra.json"
        for p, f in (("kubernetes", "kubectl-get.json"), ("terraform", "terraform-show.json")):
            rc, _, _ = call("--platform", p, "--input", str(FIX / f), "--repos", str(self.repos), "--out", str(out))
            self.assertEqual(rc, 0)
        doc = json.loads(out.read_text())
        self.assertEqual(sorted(doc["platforms"]), ["kubernetes", "terraform"])
        self.assertEqual(len(doc["edges"]), sum(len(d["edges"]) for d in doc["platforms"].values()))
        self.assertEqual(doc["version"], 1)
        # re-running one platform replaces only that platform
        call("--platform", "kubernetes", "--input", str(FIX / "kubectl-get.json"), "--repos", str(self.repos), "--out", str(out))
        self.assertEqual(json.loads(out.read_text())["platforms"]["terraform"], doc["platforms"]["terraform"])

    def test_bad_input_is_an_error_not_a_crash(self):
        rc, doc, _ = call("--platform", "kubernetes", "--input", "/nonexistent.json")
        self.assertEqual(rc, 0)
        self.assertTrue(doc["errors"])


# ------------------------------------------------------------------ live mode with stub CLIs
STUB = r"""#!/usr/bin/env python3
import os, sys, json
mode = os.environ["STUB_MODE"]
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(" ".join([os.path.basename(sys.argv[0])] + sys.argv[1:]) + "\n")
tool, a = os.path.basename(sys.argv[0]), sys.argv[1:]
is_probe = ("create" in a and "--dry-run=server" in a) or ("create-vpc" in a)
if is_probe:
    if mode == "admin":
        if tool == "aws":
            sys.stderr.write("An error occurred (DryRunOperation) when calling the CreateVpc operation: Request would have succeeded, but DryRun flag is set.\n"); sys.exit(254)
        print("deployment.apps/factory-probe created (server dry run)"); sys.exit(0)
    if mode == "readonly":
        if tool == "aws":
            sys.stderr.write("An error occurred (UnauthorizedOperation) when calling the CreateVpc operation: You are not authorized to perform this operation.\n"); sys.exit(254)
        sys.stderr.write('Error from server (Forbidden): deployments.apps is forbidden: User "viewer" cannot create resource "deployments"\n'); sys.exit(1)
    sys.stderr.write("Unable to connect to the server: dial tcp: lookup k8s.invalid: no such host\n"); sys.exit(1)
if tool == "kubectl" and "get" in a:
    kind = a[a.index("get") + 1]
    if kind == "networkpolicies":
        sys.stderr.write("Error from server (Forbidden): networkpolicies is forbidden\n"); sys.exit(1)
    items = [i for i in json.load(open(os.environ["STUB_FIXTURE"]))["items"] if i["kind"].lower() + "s" == kind or (kind == "ingresses" and i["kind"] == "Ingress") or (kind == "networkpolicies" and i["kind"] == "NetworkPolicy")]
    print(json.dumps({"kind": "List", "items": items})); sys.exit(0)
if tool == "aws":
    if "list-queues" in a:
        sys.stderr.write("An error occurred (ThrottlingException): Rate exceeded\n"); sys.exit(254)
    print("{}"); sys.exit(0)
sys.exit(0)
"""


class Live(Base):
    def setUp(self):
        self.bin = Path(tempfile.mkdtemp(dir=self.tmp.name))
        for tool in ("kubectl", "aws"):
            p = self.bin / tool
            p.write_text(STUB)
            p.chmod(p.stat().st_mode | stat.S_IEXEC)
        self.log = self.bin / "calls.log"
        self.log.write_text("")
        self.env = {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "STUB_LOG": str(self.log),
                    "STUB_FIXTURE": str(FIX / "kubectl-get.json")}
        self.old = {k: os.environ.get(k) for k in (*self.env, "STUB_MODE")}
        os.environ.update(self.env)
        G.time.sleep = lambda s: None  # no real backoff in tests

    def tearDown(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def calls(self):
        return [line for line in self.log.read_text().splitlines() if line]

    def test_over_privileged_kubernetes_credential_is_refused_and_nothing_is_read(self):
        os.environ["STUB_MODE"] = "admin"
        rc, doc, err = call("--platform", "kubernetes", "--namespace", "shop")
        self.assertEqual(rc, G.PROBE_REFUSED)
        self.assertIsNone(doc)
        self.assertIn("REFUSED", err)
        self.assertEqual(len(self.calls()), 1, self.calls())
        self.assertIn("--dry-run=server", self.calls()[0])

    def test_over_privileged_aws_credential_is_refused(self):
        os.environ["STUB_MODE"] = "admin"
        rc, _, err = call("--platform", "aws")
        self.assertEqual(rc, G.PROBE_REFUSED)
        self.assertEqual(self.calls(), ["aws ec2 create-vpc --cidr-block 10.255.0.0/16 --dry-run"])

    def test_unreachable_cluster_is_inconclusive_not_passed(self):
        os.environ["STUB_MODE"] = "down"
        rc, _, _ = call("--platform", "kubernetes")
        self.assertEqual(rc, G.PROBE_INCONCLUSIVE)
        self.assertEqual(len(self.calls()), 1)

    def test_read_only_kubernetes_credential_reads_and_collects_errors(self):
        os.environ["STUB_MODE"] = "readonly"
        rc, doc, _ = call("--platform", "kubernetes", "--repos", str(self.repos), "--jobs", "4")
        self.assertEqual(rc, 0)
        self.assertEqual(doc["probe"]["result"], "passed")
        self.assertIn(("acme/storefront", "acme/api", "env"), self.edges(doc))
        self.assertTrue(any("networkpolicies" in e for e in doc["errors"]))   # forbidden kind -> errors[], not abort
        gets = [c for c in self.calls() if " get " in c]
        self.assertEqual(len(gets), len(G.K8S_KINDS))
        self.assertTrue(all("--chunk-size=500" in c and "-A" in c for c in gets))
        self.assertFalse(any(v in c for c in self.calls() for v in (" apply", " delete", " patch", " edit", " scale")))

    def test_read_only_aws_credential_retries_throttling_then_records_error(self):
        os.environ["STUB_MODE"] = "readonly"
        rc, doc, _ = call("--platform", "aws", "--jobs", "4")
        self.assertEqual(rc, 0)
        self.assertEqual(sum("list-queues" in c for c in self.calls()), 5)   # 1 + 4 retries
        self.assertTrue(any("list-queues" in e for e in doc["errors"]))
        verbs = {c.split()[2] for c in self.calls()[1:]}
        self.assertTrue(all(v.startswith(("describe-", "list-")) for v in verbs), verbs)

    def test_terraform_remote_backend_requires_probe_attestation(self):
        rc, _, err = call("--platform", "terraform")
        self.assertEqual(rc, G.PROBE_INCONCLUSIVE)
        self.assertIn("--probe-passed", err)


if __name__ == "__main__":
    unittest.main(verbosity=1)
