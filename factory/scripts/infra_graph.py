#!/usr/bin/env python3
"""Read-only runtime dependency graph (infra.json) from one infra platform.

Usage:
    infra_graph.py --platform kubernetes|terraform|aws [--jobs 8] [--out infra.json]
                   [--input FILE ...] [--repos graph.json] [--org ORG]
                   [--namespace NS ...] [--context CTX] [--dir TF_DIR ...]
                   [--backend local|remote] [--probe-passed]

Two modes:
  --input FILE   offline: parse recorded output (`kubectl get -o json`,
                 `terraform show -json`, or an aws snapshot written by this
                 script). No credential is used, so no probe is needed.
  (live)         runs the platform CLI with the user's READ-ONLY credential.
                 The verification probe from references/infra.md runs first:
                 an attempted write that must be DENIED. If the write is
                 allowed the credential is refused (exit 3) and nothing is
                 read. If the probe is inconclusive we exit 4.

Never writes to a platform, never prints secret values: only resource names,
images, hostnames and ports are kept, and `terraform show -json` values are
reduced to an allowlist of non-secret attributes.

Output (schema: references/knowledge-base.md "infra.json"): resources[] and
edges[] whose `source` is `infra:<platform>:<resource>`, merged into --out per
platform (other platforms' entries are kept). errors[] never abort the run.
Exit 0 on success or partial failure, 2 bad args, 3 probe refused, 4 probe
inconclusive. Stdlib only.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

VERSION = 1
PLATFORMS = ("kubernetes", "terraform", "aws")
PROBE_REFUSED, PROBE_INCONCLUSIVE = 3, 4
THROTTLE_RE = re.compile(r"throttl|TooManyRequests|Rate exceeded|RequestLimitExceeded|\b429\b|SlowDown", re.I)
FORBIDDEN_RE = re.compile(r"forbidden|UnauthorizedOperation|AccessDenied|not authorized|permission denied|\b403\b", re.I)
HOST_RE = re.compile(r"^(?:(?P<scheme>[a-z][a-z0-9+.-]*)://)?(?:[^@/\s]*@)?"
                     r"(?P<host>[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*)"
                     r"(?::(?P<port>\d{1,5}))?(?:[/?#].*)?$")
IMAGE_RE = re.compile(r"^(?:(?P<reg>[^/]+\.[^/]+|localhost(?::\d+)?)/)?(?P<path>[a-z0-9._/-]+?)(?::[\w.-]+)?(?:@sha256:[0-9a-f]+)?$")
K8S_KINDS = ("deployments", "statefulsets", "daemonsets", "cronjobs", "services", "ingresses", "networkpolicies")
WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "CronJob", "Job", "Pod"}
REPO_LABELS = ("app.kubernetes.io/name", "app.kubernetes.io/instance", "app.kubernetes.io/part-of", "app")


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def now():
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- command runner
def run(cmd, timeout=120, retries=4):
    """Run a read-only CLI call. Retries with backoff on throttling. Returns (rc, out, err)."""
    delay = 1.0
    for attempt in range(retries + 1):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as e:
            return 127, "", f"{cmd[0]}: {e}"
        if p.returncode == 0 or not THROTTLE_RE.search(p.stderr) or attempt == retries:
            return p.returncode, p.stdout, p.stderr.strip()
        time.sleep(delay)
        delay *= 2
    return 1, "", "unreachable"


# ---------------------------------------------------------------- probes
def probe_kubernetes(args):
    ns = (args.namespace or ["default"])[0]
    cmd = ["kubectl", *ctx_args(args), "create", "deployment", "factory-probe", "--image=registry.invalid/factory-probe",
           "-n", ns, "--dry-run=server", "-o", "name"]
    return classify_probe(cmd, run(cmd, timeout=30, retries=1))


def probe_aws(args):
    cmd = ["aws", "ec2", "create-vpc", "--cidr-block", "10.255.0.0/16", "--dry-run"]
    cmd += ["--region", args.region] if args.region else []
    rc, out, err = run(cmd, timeout=30, retries=1)
    if "DryRunOperation" in err:  # dry run says the call WOULD have succeeded
        rc = 0
    return classify_probe(cmd, (rc, out, err))


def classify_probe(cmd, res):
    rc, _, err = res
    shown = " ".join(cmd)
    if rc == 0:
        return {"command": shown, "result": "refused", "detail": "write was ALLOWED; this credential can write"}
    if FORBIDDEN_RE.search(err):
        return {"command": shown, "result": "passed", "detail": "write denied"}
    return {"command": shown, "result": "inconclusive", "detail": err[-300:]}


def ctx_args(args):
    return ["--context", args.context] if args.context else []


# ---------------------------------------------------------------- repo mapping
class RepoMap:
    def __init__(self, graph_path=None, org=None):
        self.org = org
        self.repos = set()
        if graph_path:
            g = json.loads(Path(graph_path).read_text())
            self.repos = {r["full_name"] for r in g.get("repos", [])}
            if not self.org and self.repos:
                self.org = sorted(self.repos)[0].split("/")[0]
        self.by_name = {}
        for r in self.repos:
            self.by_name.setdefault(r.split("/", 1)[1].lower(), r)

    def _known(self, full):
        return full if (not self.repos or full in self.repos) else None

    def from_image(self, image):
        m = IMAGE_RE.match(image or "")
        if not m:
            return None
        parts = m.group("path").split("/")
        if len(parts) >= 2:
            full = f"{parts[-2]}/{parts[-1]}"
            if full in self.repos or (not self.repos and self.org and parts[-2].lower() == self.org.lower()):
                return full
        return self.by_name.get(parts[-1].lower()) if self.repos else None

    def from_label(self, value):
        return self.by_name.get(str(value).lower()) if self.repos else None

    def resolve(self, images, labels):
        for img in images:
            r = self.from_image(img)
            if r:
                return r, f"image {img}"
        for k in REPO_LABELS:
            if k in labels:
                r = self.from_label(labels[k])
                if r:
                    return r, f"label {k}={labels[k]}"
        return None, None


# ---------------------------------------------------------------- model
class Graph:
    """Platform-neutral model: resources expose hosts; refs point at hosts."""

    def __init__(self, platform, repomap):
        self.platform, self.repomap = platform, repomap
        self.res = {}          # id -> resource dict
        self.exposes = {}      # host -> [resource id]
        self.refs = []         # (from_id, host, port, protocol, evidence, kind, url_key)
        self.direct = []       # (from_id, to_id_or_external, kind, evidence, protocol)
        self.errors = []

    def add(self, rid, kind, name, *, namespace=None, images=(), labels=None, hosts=(), extra=None):
        r = {"id": rid, "kind": kind, "name": name}
        if namespace:
            r["namespace"] = namespace
        if images:
            r["images"] = sorted(set(images))
        repo, why = self.repomap.resolve(r.get("images", []), labels or {})
        if repo:
            r["repo"], r["repo_evidence"] = repo, why
        if hosts:
            r["endpoints"] = sorted(set(h.lower() for h in hosts))
        if extra:
            r.update(extra)
        r["source"] = f"infra:{self.platform}:{rid}"
        self.res[rid] = r
        for h in hosts:
            self.exposes.setdefault(h.lower(), []).append(rid)
        return r

    def expose_url(self, url, rid):
        """Register a resource reachable only by full URL (e.g. SQS: one host, many queues)."""
        self.exposes.setdefault(url_key(url), []).append(rid)

    def ref(self, from_id, value, evidence, normalize=None, kind="env"):
        value = str(value).strip()
        m = HOST_RE.match(value)
        if not m:
            return
        host = m.group("host").lower()
        if normalize:
            host = normalize(host)
        if not host:
            return
        self.refs.append((from_id, host, m.group("port"), m.group("scheme"), evidence, kind, url_key(value)))

    def node(self, rid):
        r = self.res.get(rid)
        if r is None:
            return rid
        return r.get("repo") or r["source"]

    def edges(self):
        out = {}

        def put(frm, to, kind, evidence, proto, src):
            if frm == to:
                return
            e = {"from": frm, "to": to, "kind": kind}
            if proto:
                e["protocol"] = proto
            e.update({"evidence": evidence, "source": src})
            out.setdefault((frm, to, kind, evidence), e)

        for frm, host, port, proto, ev, kind, ukey in self.refs:
            targets = self.exposes.get(ukey) or self.exposes.get(host)
            src = f"infra:{self.platform}:{frm}"
            if targets:
                for t in targets:
                    for tt in self.res[t].get("selects", [t]):
                        put(self.node(frm), self.node(tt), kind, ev, proto, src)
            elif "." in host and not host.endswith((".cluster.local", ".svc", ".internal", ".local")):
                put(self.node(frm), f"external:{host}", kind, ev, proto, src)
        for frm, to, kind, ev, proto in self.direct:
            targets = self.res[to].get("selects", [to]) if to in self.res else [to]
            for tt in targets:
                put(self.node(frm) if frm in self.res else frm, self.node(tt), kind, ev, proto,
                    f"infra:{self.platform}:{frm if frm in self.res else to}")
        return sorted(out.values(), key=lambda e: (e["from"], e["to"], e["kind"], e["evidence"]))

    def result(self):
        return {"resources": sorted(self.res.values(), key=lambda r: r["id"]),
                "edges": self.edges(), "errors": self.errors}


# ---------------------------------------------------------------- kubernetes
def k8s_items(doc):
    if isinstance(doc, dict) and doc.get("kind", "").endswith("List"):
        return doc.get("items", [])
    return [doc] if isinstance(doc, dict) and doc.get("kind") else []


def pod_spec(obj):
    spec = obj.get("spec", {})
    if obj["kind"] == "CronJob":
        spec = spec.get("jobTemplate", {}).get("spec", {})
    if obj["kind"] == "Pod":
        return obj.get("metadata", {}).get("labels", {}), spec
    t = spec.get("template", {})
    return t.get("metadata", {}).get("labels", {}), t.get("spec", {})


def selector_matches(sel, labels):
    return bool(sel) and all(labels.get(k) == v for k, v in sel.items())


def build_kubernetes(items, g):
    workloads, services = [], []
    for o in items:
        kind, md = o.get("kind"), o.get("metadata", {})
        ns, name = md.get("namespace", "default"), md.get("name", "?")
        rid = f"{kind.lower()}/{ns}/{name}"
        if kind in WORKLOAD_KINDS:
            plabels, ps = pod_spec(o)
            conts = ps.get("containers", []) + ps.get("initContainers", [])
            labels = {**plabels, **md.get("labels", {})}
            g.add(rid, kind, name, namespace=ns, images=[c.get("image", "") for c in conts if c.get("image")],
                  labels=labels)
            workloads.append((rid, ns, plabels))
            norm = k8s_normalizer(ns)
            for c in conts:
                for e in c.get("env", []) or []:
                    if "value" in e:  # valueFrom (secrets/configmaps) is never read
                        g.ref(rid, e["value"], f"env {e.get('name')} ({c.get('name')})", norm)
        elif kind == "Service":
            spec = o.get("spec", {})
            fq = f"{name}.{ns}.svc.cluster.local"
            extra = {"ports": sorted({p.get("port") for p in spec.get("ports", []) if p.get("port")})} if spec.get("ports") else None
            if spec.get("type") == "ExternalName" and spec.get("externalName"):
                extra = {**(extra or {}), "selects": [f"external:{spec['externalName'].lower()}"]}
            g.add(rid, kind, name, namespace=ns, hosts=[fq], extra=extra)
            services.append((rid, ns, spec.get("selector") or {}))
    for rid, ns, sel in services:
        if "selects" in g.res[rid]:
            continue
        g.res[rid]["selects"] = sorted(w for w, wns, lab in workloads if wns == ns and selector_matches(sel, lab))
        if not g.res[rid]["selects"]:
            g.res[rid]["selects"] = [rid]
    for o in items:
        kind, md = o.get("kind"), o.get("metadata", {})
        ns, name = md.get("namespace", "default"), md.get("name", "?")
        if kind == "Ingress":
            spec = o.get("spec", {})
            tls = {h for t in spec.get("tls", []) or [] for h in t.get("hosts", [])}
            for rule in spec.get("rules", []) or []:
                host = rule.get("host") or "*"
                for p in (rule.get("http") or {}).get("paths", []):
                    svc = ((p.get("backend") or {}).get("service") or {}).get("name")
                    if svc and f"service/{ns}/{svc}" in g.res:
                        g.direct.append((f"external:{host}", f"service/{ns}/{svc}", "ingress",
                                         f"ingress {ns}/{name} {host}{p.get('path', '')}",
                                         "https" if host in tls else "http"))
            g.add(f"ingress/{ns}/{name}", kind, name, namespace=ns,
                  extra={"hosts": sorted({r.get("host") for r in spec.get("rules", []) or [] if r.get("host")})})
        elif kind == "NetworkPolicy":
            spec = o.get("spec", {})
            dst = [w for w, wns, lab in workloads if wns == ns and selector_matches(spec.get("podSelector", {}).get("matchLabels", {}), lab)]
            for rule in spec.get("ingress", []) or []:
                for peer in rule.get("from", []) or []:
                    sel = (peer.get("podSelector") or {}).get("matchLabels")
                    if not sel or peer.get("namespaceSelector"):
                        continue
                    for s in (w for w, wns, lab in workloads if wns == ns and selector_matches(sel, lab)):
                        for d in dst:
                            g.direct.append((s, d, "network_policy", f"networkpolicy {ns}/{name}", None))


def k8s_normalizer(ns):
    def norm(host):
        if host.endswith(".cluster.local"):
            return host
        parts = host.split(".")
        if len(parts) == 1:
            return f"{host}.{ns}.svc.cluster.local"
        if len(parts) == 2:
            return f"{host}.svc.cluster.local"
        if len(parts) == 3 and parts[2] == "svc":
            return f"{host}.cluster.local"
        return host
    return norm


def collect_kubernetes(args, g):
    namespaces = args.namespace or [None]
    jobs = [(k, ns) for k in K8S_KINDS for ns in namespaces]

    def one(job):
        kind, ns = job
        cmd = ["kubectl", *ctx_args(args), "get", kind, "-o", "json", "--chunk-size=500"]
        cmd += ["-n", ns] if ns else ["-A"]
        rc, out, err = run(cmd)
        if rc != 0:
            return [], f"kubectl get {kind} {'-n ' + ns if ns else '-A'}: {err[-200:]}"
        try:
            return k8s_items(json.loads(out)), None
        except ValueError as e:
            return [], f"kubectl get {kind}: bad json: {e}"

    items = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
        for its, err in ex.map(one, jobs):
            items += its
            if err:
                g.errors.append(err)
    return items


# ---------------------------------------------------------------- terraform
TF_KEEP = {  # type -> (name attr, host attrs); nothing else is ever copied out
    "aws_db_instance": ("identifier", ("address",)),
    "aws_rds_cluster": ("cluster_identifier", ("endpoint", "reader_endpoint")),
    "aws_elasticache_replication_group": ("replication_group_id", ("primary_endpoint_address", "reader_endpoint_address", "configuration_endpoint_address")),
    "aws_elasticache_cluster": ("cluster_id", ("cluster_address", "configuration_endpoint")),
    "aws_sqs_queue": ("name", ("url",)),
    "aws_sns_topic": ("name", ()),
    "aws_lambda_function": ("function_name", ()),
    "aws_ecs_task_definition": ("family", ()),
    "aws_ecs_service": ("name", ()),
    "aws_lb": ("name", ("dns_name",)),
    "aws_route53_record": ("fqdn", ()),
    "google_sql_database_instance": ("name", ("connection_name",)),
    "google_redis_instance": ("name", ("host",)),
    "google_cloud_run_v2_service": ("name", ("uri",)),
    "google_cloud_run_service": ("name", ()),
    "google_pubsub_topic": ("name", ()),
    "google_pubsub_subscription": ("name", ()),
    "google_dns_record_set": ("name", ()),
    "cloudflare_record": ("name", ()),
    "cloudflare_dns_record": ("name", ()),
}


def tf_resources(module):
    yield from module.get("resources", []) or []
    for child in module.get("child_modules", []) or []:
        yield from tf_resources(child)


def url_key(url):
    """scheme-less, credential-less, lowercased host+path: identity of a URL-addressed resource."""
    u = re.sub(r"^[a-z][a-z0-9+.-]*://", "", str(url).strip().lower())
    u = u.split("@", 1)[-1] if "@" in u.split("/", 1)[0] else u
    return "url:" + u.split("?", 1)[0].rstrip("/")


def arn_name(arn):
    return str(arn).rsplit(":", 1)[-1].rsplit("/", 1)[-1]


def task_family(ref):
    """'arn:...:task-definition/api:7' | 'api:7' | 'api' -> 'api'"""
    return str(ref).rsplit("/", 1)[-1].split(":", 1)[0]


def build_terraform(doc, g, label="state"):
    root = (doc.get("values") or {}).get("root_module") or {}
    arns, families, lambdas = {}, {}, {}
    rows = [r for r in tf_resources(root) if r.get("mode") == "managed" and r.get("type") in TF_KEEP]
    for r in rows:
        typ, v, addr = r["type"], r.get("values") or {}, r["address"]
        name_attr, host_attrs = TF_KEEP[typ]
        hosts = []
        for a in host_attrs:
            if v.get(a) and typ != "aws_sqs_queue":
                m = HOST_RE.match(str(v[a]))
                if m and "." in m.group("host"):
                    hosts.append(m.group("host"))
        images = []
        if typ == "aws_ecs_task_definition":
            try:
                images = [c.get("image", "") for c in json.loads(v.get("container_definitions") or "[]")]
            except ValueError:
                g.errors.append(f"{label}: {addr}: container_definitions not json")
        if typ == "aws_lambda_function" and v.get("image_uri"):
            images = [v["image_uri"]]
        extra = {"type": typ}
        if v.get("port"):
            extra["ports"] = [v["port"]]
        g.add(addr, typ, str(v.get(name_attr) or r.get("name")), images=[i for i in images if i], hosts=hosts, extra=extra)
        if typ == "aws_sqs_queue" and v.get("url"):
            g.expose_url(v["url"], addr)
        if v.get("arn"):
            arns[v["arn"]] = addr
        if typ == "aws_ecs_task_definition":
            families[v.get("family")] = addr
        if typ == "aws_lambda_function":
            lambdas[v.get("function_name")] = addr
    for r in rows:
        typ, v, addr = r["type"], r.get("values") or {}, r["address"]
        sens = r.get("sensitive_values") or {}
        if typ == "aws_ecs_task_definition":
            try:
                for c in json.loads(v.get("container_definitions") or "[]"):
                    for e in c.get("environment", []) or []:
                        g.ref(addr, e.get("value", ""), f"env {e.get('name')} ({c.get('name')})")
            except ValueError:
                pass
        elif typ == "aws_lambda_function":
            senv = ((sens.get("environment") or [{}])[0] or {}).get("variables") or {}
            for blk in v.get("environment") or []:
                for k, val in (blk.get("variables") or {}).items():
                    if senv is True or (isinstance(senv, dict) and senv.get(k)):
                        continue
                    g.ref(addr, val, f"env {k}")
        elif typ == "aws_ecs_service":
            fam = task_family(v.get("task_definition", ""))
            if fam in families:
                g.res[addr].update({k: g.res[families[fam]][k] for k in ("repo", "repo_evidence", "images") if k in g.res[families[fam]]})
                g.direct.append((addr, families[fam], "runs", f"{addr} task_definition", None))
        elif typ == "aws_route53_record":
            fq = str(v.get("fqdn") or v.get("name")).rstrip(".")
            for rec in v.get("records") or []:
                g.ref(addr, rec, f"dns {v.get('type')} {fq}", kind="dns")
            alias = (v.get("alias") or [{}])[0] if v.get("alias") else {}
            if alias.get("name"):
                g.ref(addr, alias["name"], f"dns ALIAS {fq}", kind="dns")
            g.res[addr]["hosts"] = [fq]
            g.direct.append((f"external:{fq.lower()}", addr, "dns", f"dns {v.get('type')} {fq}", None))
    for r in (x for x in tf_resources(root) if x.get("mode") == "managed"):
        typ, v, addr = r.get("type"), r.get("values") or {}, r.get("address")
        if typ == "aws_sns_topic_subscription" and v.get("topic_arn") in arns and v.get("endpoint") in arns:
            g.direct.append((arns[v["topic_arn"]], arns[v["endpoint"]], "subscription", f"{addr} ({v.get('protocol')})", v.get("protocol")))
        elif typ == "aws_lambda_event_source_mapping":
            fn = lambdas.get(arn_name(v.get("function_name", "")))
            src = arns.get(v.get("event_source_arn"))
            if fn and src:
                g.direct.append((fn, src, "consumes", addr, None))


# ---------------------------------------------------------------- aws (live snapshot)
AWS_CALLS = {  # key -> read-only CLI call (the CLI paginates automatically)
    "rds": ["rds", "describe-db-instances"],
    "elasticache": ["elasticache", "describe-replication-groups"],
    "sqs": ["sqs", "list-queues"],
    "sns_subs": ["sns", "list-subscriptions"],
    "lambda": ["lambda", "list-functions"],
    "lambda_esm": ["lambda", "list-event-source-mappings"],
    "ecs_clusters": ["ecs", "list-clusters"],
    "zones": ["route53", "list-hosted-zones"],
}


def aws_cli(args, *call):
    cmd = ["aws", *call, "--output", "json"]
    if args.region:
        cmd += ["--region", args.region]
    rc, out, err = run(cmd)
    if rc != 0:
        raise RuntimeError(f"aws {' '.join(call[:2])}: {err[-200:]}")
    return json.loads(out or "{}")


def collect_aws(args, g):
    os.environ.setdefault("AWS_RETRY_MODE", "adaptive")
    os.environ.setdefault("AWS_MAX_ATTEMPTS", "10")
    snap = {}

    def one(item):
        k, call = item
        try:
            return k, aws_cli(args, *call), None
        except (RuntimeError, ValueError) as e:
            return k, None, str(e)

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
        for k, v, err in ex.map(one, AWS_CALLS.items()):
            if err:
                g.errors.append(err)
            else:
                snap[k] = v
    # second level: ECS services + task definitions, Route53 record sets
    tds, records = {}, {}
    for cl in (snap.get("ecs_clusters") or {}).get("clusterArns", []):
        try:
            arns = aws_cli(args, "ecs", "list-services", "--cluster", cl).get("serviceArns", [])
            for i in range(0, len(arns), 10):  # describe-services takes at most 10
                for s in aws_cli(args, "ecs", "describe-services", "--cluster", cl, "--services", *arns[i:i + 10]).get("services", []):
                    snap.setdefault("ecs_services", []).append({k: s.get(k) for k in ("serviceName", "serviceArn", "taskDefinition", "clusterArn")})
        except (RuntimeError, ValueError) as e:
            g.errors.append(str(e))
    wanted = sorted({s["taskDefinition"] for s in snap.get("ecs_services", []) if s.get("taskDefinition")})

    def td(arn):
        try:
            return arn, aws_cli(args, "ecs", "describe-task-definition", "--task-definition", arn)["taskDefinition"], None
        except (RuntimeError, ValueError, KeyError) as e:
            return arn, None, str(e)

    def rrs(zone):
        try:
            return zone, aws_cli(args, "route53", "list-resource-record-sets", "--hosted-zone-id", zone).get("ResourceRecordSets", []), None
        except (RuntimeError, ValueError) as e:
            return zone, None, str(e)

    zones = [z["Id"] for z in (snap.get("zones") or {}).get("HostedZones", [])]
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
        for arn, v, err in ex.map(td, wanted):
            (g.errors.append(err) if err else tds.__setitem__(arn, v))
        for z, v, err in ex.map(rrs, zones):
            (g.errors.append(err) if err else records.__setitem__(z, v))
    snap["task_definitions"], snap["record_sets"] = tds, records
    return snap


def build_aws(snap, g):
    arns, fns = {}, {}
    for db in (snap.get("rds") or {}).get("DBInstances", []):
        ep = (db.get("Endpoint") or {})
        g.add(f"rds/{db['DBInstanceIdentifier']}", "rds", db["DBInstanceIdentifier"],
              hosts=[ep["Address"]] if ep.get("Address") else [], extra={"ports": [ep["Port"]]} if ep.get("Port") else None)
    for rg in (snap.get("elasticache") or {}).get("ReplicationGroups", []):
        hosts = []
        for ng in rg.get("NodeGroups", []):
            for k in ("PrimaryEndpoint", "ReaderEndpoint"):
                if (ng.get(k) or {}).get("Address"):
                    hosts.append(ng[k]["Address"])
        if (rg.get("ConfigurationEndpoint") or {}).get("Address"):
            hosts.append(rg["ConfigurationEndpoint"]["Address"])
        g.add(f"elasticache/{rg['ReplicationGroupId']}", "elasticache", rg["ReplicationGroupId"], hosts=hosts)
    for url in (snap.get("sqs") or {}).get("QueueUrls", []):
        name = url.rstrip("/").rsplit("/", 1)[-1]
        rid = f"sqs/{name}"
        g.add(rid, "sqs", name)
        g.expose_url(url, rid)
        m = re.match(r"https://sqs\.([\w-]+)\.amazonaws\.com/(\d+)/(.+)$", url)
        if m:
            arns[f"arn:aws:sqs:{m.group(1)}:{m.group(2)}:{m.group(3)}"] = rid
    for f in (snap.get("lambda") or {}).get("Functions", []):
        rid = f"lambda/{f['FunctionName']}"
        g.add(rid, "lambda", f["FunctionName"])
        fns[f["FunctionName"]] = rid
        arns[f.get("FunctionArn", "")] = rid
        for k, val in ((f.get("Environment") or {}).get("Variables") or {}).items():
            g.ref(rid, val, f"env {k}")
    for m in (snap.get("lambda_esm") or {}).get("EventSourceMappings", []):
        fn = arns.get(m.get("FunctionArn")) or fns.get(arn_name(m.get("FunctionArn", "")))
        src = arns.get(m.get("EventSourceArn"))
        if fn and src:
            g.direct.append((fn, src, "consumes", f"event-source-mapping {m.get('UUID', '')}", None))
    for s in (snap.get("sns_subs") or {}).get("Subscriptions", []):
        topic = s.get("TopicArn", "")
        tid = f"sns/{arn_name(topic)}"
        if tid not in g.res:
            g.add(tid, "sns", arn_name(topic))
        arns[topic] = tid
        if s.get("Endpoint") in arns:
            g.direct.append((tid, arns[s["Endpoint"]], "subscription", f"sns subscription ({s.get('Protocol')})", s.get("Protocol")))
    for arn, t in (snap.get("task_definitions") or {}).items():
        rid = f"ecs-task/{t.get('family')}"
        conts = t.get("containerDefinitions", [])
        g.add(rid, "ecs-task", t.get("family", "?"), images=[c.get("image", "") for c in conts if c.get("image")])
        for c in conts:
            for e in c.get("environment", []) or []:  # `secrets` (SSM/SecretsManager refs) are never read
                g.ref(rid, e.get("value", ""), f"env {e.get('name')} ({c.get('name')})")
    for s in snap.get("ecs_services", []):
        fam = task_family(s.get("taskDefinition", ""))
        rid = f"ecs-service/{arn_name(s.get('clusterArn', ''))}/{s['serviceName']}"
        g.add(rid, "ecs-service", s["serviceName"])
        if f"ecs-task/{fam}" in g.res:
            g.res[rid].update({k: g.res[f"ecs-task/{fam}"][k] for k in ("repo", "repo_evidence", "images") if k in g.res[f"ecs-task/{fam}"]})
            g.direct.append((rid, f"ecs-task/{fam}", "runs", f"ecs service {s['serviceName']}", None))
    for zone, sets in (snap.get("record_sets") or {}).items():
        for rs in sets:
            if rs.get("Type") not in ("A", "AAAA", "CNAME"):
                continue
            fq = rs["Name"].rstrip(".").lower()
            rid = f"route53/{fq}/{rs['Type']}"
            g.add(rid, "route53-record", fq, extra={"hosts": [fq]})
            g.direct.append((f"external:{fq}", rid, "dns", f"dns {rs['Type']} {fq}", None))
            for rr in rs.get("ResourceRecords", []):
                g.ref(rid, rr.get("Value", ""), f"dns {rs['Type']} {fq}", kind="dns")
            if (rs.get("AliasTarget") or {}).get("DNSName"):
                g.ref(rid, rs["AliasTarget"]["DNSName"].rstrip("."), f"dns ALIAS {fq}", kind="dns")


# ---------------------------------------------------------------- driver
def load_inputs(paths, g):
    docs = []
    for p in paths:
        try:
            docs.append(json.loads(Path(p).read_text()))
        except (OSError, ValueError) as e:
            g.errors.append(f"{p}: {e}")
    return docs


def build(args):
    repomap = RepoMap(args.repos, args.org)
    g = Graph(args.platform, repomap)
    probe = {"result": "not-needed", "detail": "offline input"} if args.input else None
    if not args.input:
        if args.platform == "kubernetes":
            probe = probe_kubernetes(args)
        elif args.platform == "aws":
            probe = probe_aws(args)
        elif args.backend == "local":
            probe = {"result": "not-needed", "detail": "local state file; no credential"}
        elif args.probe_passed:
            probe = {"result": "passed", "detail": "attested: backend probe from references/infra.md run by caller"}
        else:
            probe = {"result": "inconclusive", "detail": "remote terraform backend: run the backend platform's probe, then pass --probe-passed"}
        if probe["result"] != "passed" and probe["result"] != "not-needed":
            return g, probe
    if args.platform == "kubernetes":
        items = [i for d in load_inputs(args.input, g) for i in k8s_items(d)] if args.input else collect_kubernetes(args, g)
        build_kubernetes(items, g)
    elif args.platform == "terraform":
        if args.input:
            docs = list(zip(args.input, load_inputs(args.input, g)))
        else:
            def show(d):
                rc, out, err = run(["terraform", f"-chdir={d}", "show", "-json"], timeout=600, retries=0)
                if rc != 0:
                    return d, None, f"terraform show -json in {d}: {err[-200:]}"
                try:
                    return d, json.loads(out), None
                except ValueError as e:
                    return d, None, f"terraform show -json in {d}: {e}"
            docs = []
            with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
                for d, doc, err in ex.map(show, args.dir or ["."]):
                    (g.errors.append(err) if err else docs.append((d, doc)))
        for label, doc in docs:
            build_terraform(doc, g, label)
    elif args.platform == "aws":
        snap = load_inputs(args.input, g)[0] if args.input else collect_aws(args, g)
        if args.snapshot and not args.input:
            Path(args.snapshot).write_text(json.dumps(snap, indent=1, sort_keys=True))
        build_aws(snap or {}, g)
    return g, probe


def merge_out(path, platform, doc):
    out = {"version": VERSION, "platforms": {}}
    p = Path(path)
    if p.exists():
        try:
            out = json.loads(p.read_text())
        except ValueError:
            pass
    out.setdefault("platforms", {})[platform] = doc
    out["version"], out["generated_at"] = VERSION, doc["generated_at"]
    # flat views for KB tooling: same shapes as graph.json
    out["resources"] = [r | {"platform": k} for k, d in sorted(out["platforms"].items()) for r in d["resources"]]
    out["edges"] = [e for _, d in sorted(out["platforms"].items()) for e in d["edges"]]
    out["errors"] = [f"{k}: {x}" for k, d in sorted(out["platforms"].items()) for x in d["errors"]]
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(out, indent=2) + "\n")
    tmp.replace(p)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--platform", required=True, choices=PLATFORMS)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--out", help="infra.json to write/merge (default: stdout)")
    ap.add_argument("--input", action="append", help="recorded JSON (offline mode); repeatable")
    ap.add_argument("--repos", help="graph.json from repo_graph.py, to map workloads to repos")
    ap.add_argument("--org", help="GitHub org; image <reg>/<org>/<name> maps to <org>/<name>")
    ap.add_argument("--namespace", action="append", help="kubernetes: namespace(s) to read (default: all)")
    ap.add_argument("--context", help="kubernetes: kubeconfig context")
    ap.add_argument("--region", help="aws: region")
    ap.add_argument("--snapshot", help="aws live: also write the raw read-only snapshot here")
    ap.add_argument("--dir", action="append", help="terraform: initialised root module dir(s)")
    ap.add_argument("--backend", choices=("local", "remote"), default="remote",
                    help="terraform live: 'local' state needs no credential probe")
    ap.add_argument("--probe-passed", action="store_true",
                    help="terraform remote backend: caller already ran the backend probe and it was denied")
    args = ap.parse_args(argv)
    if args.jobs < 1:
        ap.error("--jobs must be >= 1")
    t0 = time.time()
    g, probe = build(args)
    if probe["result"] == "refused":
        log(f"REFUSED: probe `{probe['command']}` was allowed. This credential can write; "
            "use a read-only credential (references/infra.md). Nothing was read.")
        return PROBE_REFUSED
    if probe["result"] == "inconclusive":
        log(f"probe inconclusive: {probe['detail']}. Nothing was read.")
        return PROBE_INCONCLUSIVE
    doc = {"generated_at": now(), "probe": probe, **g.result()}
    log(f"{args.platform}: {len(doc['resources'])} resources, {len(doc['edges'])} edges, "
        f"{len(doc['errors'])} errors in {time.time() - t0:.1f}s")
    if args.out:
        merge_out(args.out, args.platform, doc)
    else:
        print(json.dumps({"version": VERSION, "platform": args.platform, **doc}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
