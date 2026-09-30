#!/usr/bin/env python3
"""Plan (never apply) one infra change for one environment: references/infra-changes.md.

    infra_plan.py --platform terraform|kubernetes|helm|imperative --env staging|prod
                  --change CHANGE.json [--kb DIR] [--state FILE] [--root DIR]

Wraps the native plan/diff (Terraform `plan -out` + `show -json`, `kubectl diff`,
`helm diff upgrade`, an imperative CLI's dry run), classifies every resource
(additive / mutating / destructive; doubt -> destructive), computes the blast
radius from the knowledge base, snapshots what is needed to undo the change and
writes everything under `<root>/.agents/infra-plans/<change-id>/<env>/`. Prints:

    {id, env, platform, system, target, classification, reasons[], resources[],
     blast_radius, plan_hash, native_plan_sha256, plan_path, inverse_plan_path,
     verify_checks[], plan_diff?, credential_env_var, state_cmd}

It never applies, never passes an auto-approve / force flag, and never prints or
stores a credential value (only the env var *name*).

Refusals (exit 1): missing / shared / cross-env write credential, a cross-env
probe that is allowed, forbidden flags in the change file, `--env prod` without
`verified_staging: done` in `.agents/factory-state.json` for the same plan hash
and an ask that included prod, re-planning staging after it was applied.
Exit 3: the environment is not in the KB (`runtime.environments[]`): STOP and ask.
Exit 4: the native tool failed. Exit 2: usage error.

Change file (JSON):

    {"id": "chg-20260930-api-db-size", "system": "api", "summary": "...",
     "platform": "terraform", "credential_platform": "AWS",   # -> AWS_WRITE_STAGING / AWS_WRITE_PROD
     "credential_env": "AWS_PROFILE",       # child env var that receives the write credential
     "cross_env_probe": {"argv": [..., "{target}"], "allowed_pattern": "DryRunOperation"},
     "terraform":  {"dir": "infra/api", "var_file": {"staging": "staging.tfvars", "prod": "prod.tfvars"},
                    "workspace": {"staging": "staging", "prod": "prod"}},
     "kubernetes": {"manifests": "k8s/", "namespace": "api", "context": {"staging": "...", "prod": "..."}},
     "helm":       {"release": "api", "chart": "charts/api", "namespace": "api",
                    "values": {"staging": "...", "prod": "..."}, "context": {...}},
     "imperative": {"cli": "flyctl", "dry_run": {"staging": [argv], "prod": [argv]},
                    "inverse": {"staging": [argv], "prod": [argv]},
                    "effects": [{"action": "update", "type": "fly_machine", "name": "api"}],
                    "verify": {"staging": [argv]}}}

Relative paths in the change file resolve against the change file's directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import kb as KB  # noqa: E402
import state as ST  # noqa: E402

ENVS = ("staging", "prod")
OTHER = {"staging": "prod", "prod": "staging"}
PLATFORMS = ("terraform", "kubernetes", "helm", "imperative")
ALIASES = {"k8s": "kubernetes", "tf": "terraform"}
DEFAULT_CRED_ENV = {"kubernetes": "KUBECONFIG", "helm": "KUBECONFIG"}
CLASS_ORDER = {"additive": 0, "mutating": 1, "destructive": 2}
SOAK_MINUTES = {"staging": 15, "prod": 30}
CHANGE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,80}$")

# Flags / commands the policy forbids anywhere in an argv we might run or hand back.
FORBIDDEN_ARGS = re.compile(r"^(-{1,2}auto-approve(=.*)?|--force(=.*)?|--yes|-y|--grace-period=0|--now)$")

# Resource-type heuristics (Terraform types and k8s kinds, matched case-insensitively).
IAM_RE = re.compile(r"(^|[_.])(iam|role|rolebinding|clusterrole|clusterrolebinding|policy|serviceaccount|"
                    r"service_account|permission|grant|acl|access|member|binding)([_.]|$)|"
                    r"^(role|clusterrole|rolebinding|clusterrolebinding|serviceaccount)$", re.I)
DATA_RE = re.compile(r"(db_instance|rds|database|sql|dynamodb|bucket|s3|storage|volume|disk|ebs|efs|"
                     r"redis|elasticache|memorystore|kafka|topic|queue|sqs|pubsub|bigquery|table|"
                     r"persistentvolume|statefulset|snapshot|backup|kms|key_ring|crypto_key)", re.I)
TRAFFIC_RE = re.compile(r"(dns|route53|record|zone|lb|load_?balancer|listener|target_group|ingress|"
                        r"gateway|httproute|virtualservice|security_group|firewall|network_?policy|"
                        r"cdn|cloudfront|cloudflare|certificate|^service$|nat|vpc|subnet|route_table|peering)", re.I)


class Refused(Exception):
    def __init__(self, msg: str, code: int = 1):
        super().__init__(msg)
        self.code = code


# --------------------------------------------------------------------------- helpers

def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def redact(text: str) -> str:
    """Never let a credential-looking string reach a plan artifact."""
    for _, pat in KB.SECRET_PATTERNS:
        text = pat.sub("(redacted)", text)
    return text


def write_artifact(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(redact(text))
    os.chmod(path, 0o600)
    return path


def check_argv(argv, where: str) -> list[str]:
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise Refused(f"{where}: expected a non-empty list of strings")
    for a in argv:
        if FORBIDDEN_ARGS.match(a):
            raise Refused(f"{where}: forbidden flag {a!r} (no --auto-approve, no --force; approvals are human)")
    joined = " ".join(argv)
    if re.search(r"\bkubectl\b.*\bdelete\b", joined):
        raise Refused(f"{where}: `kubectl delete` is not allowed in an imperative step; "
                      f"use --platform kubernetes so a diff precedes it")
    if re.search(r"\b(apply|destroy)\b", joined) and "dry_run" in where:
        raise Refused(f"{where}: a dry run must not call apply/destroy")
    return argv


def scan_forbidden(obj, where="change") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            scan_forbidden(v, f"{where}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            scan_forbidden(v, f"{where}[{i}]")
    elif isinstance(obj, str) and FORBIDDEN_ARGS.match(obj):
        raise Refused(f"{where}: forbidden flag {obj!r} (no --auto-approve, no --force)")


def per_env(val, env):
    return val.get(env) if isinstance(val, dict) else val


# --------------------------------------------------------------------------- runner

class Runner:
    """Runs native CLIs with only this environment's write credential injected."""

    def __init__(self, cred_env: str | None, cred_value: str | None):
        self.cred_env = cred_env
        self.cred_value = cred_value

    def env(self, value: str | None = None) -> dict:
        env = {k: v for k, v in os.environ.items() if "_WRITE_" not in k}
        if self.cred_env:
            env.pop(self.cred_env, None)
            v = self.cred_value if value is None else value
            if v is not None:
                env[self.cred_env] = v
        return env

    def run(self, argv, cwd=None, value=None, extra_env=None) -> subprocess.CompletedProcess:
        check_argv(list(argv), "command")
        if shutil.which(argv[0], path=os.environ.get("PATH")) is None:
            raise Refused(f"`{argv[0]}` not found on PATH", 4)
        env = self.env(value)
        env.update(extra_env or {})
        return subprocess.run(list(argv), cwd=cwd, env=env, text=True, capture_output=True)


# --------------------------------------------------------------------------- classification

def classify_resource(kind: str, action: str, extra: str = "") -> tuple[str, list[str]]:
    """(classification, reasons) for one resource change. Doubt -> destructive."""
    if action in ("delete", "replace"):
        return "destructive", [f"{action} {kind}"]
    if action not in ("create", "update"):
        return "destructive", [f"unknown action {action!r} on {kind} (doubt -> destructive)"]
    if IAM_RE.search(kind):
        return "destructive", [f"IAM {action} on {kind}: may widen access (doubt -> destructive)"]
    if TRAFFIC_RE.search(kind) and (action == "update" or re.search(r"network_?policy", kind, re.I)):
        return "destructive", [f"{action} {kind}: may cut or reroute traffic"]
    if action == "update" and DATA_RE.search(kind):
        return "destructive", [f"update {kind}: data-affecting (doubt -> destructive)"]
    if action == "update" and re.search(r"^\+\s*replicas:\s*0\s*$", extra, re.M):
        return "destructive", [f"update {kind}: scales to 0 replicas (traffic-cutting)"]
    return ("additive", []) if action == "create" else ("mutating", [])


def overall(resources: list[dict]) -> tuple[str, list[str]]:
    if not resources:
        return "additive", ["no resource changes"]
    cls = max((r["classification"] for r in resources), key=CLASS_ORDER.get)
    reasons = [f"{r['address']}: {why}" for r in resources for why in r.get("reasons", [])]
    return cls, reasons


def _res(address, kind, name, action, namespace=None, extra="", **more) -> dict:
    cls, reasons = classify_resource(kind, action, extra)
    r = {"address": address, "type": kind, "name": name, "action": action,
         "classification": cls, "reasons": reasons}
    if namespace is not None:
        r["namespace"] = namespace
    r.update(more)
    return r


# --------------------------------------------------------------------------- terraform

TF_ACTIONS = {("create",): "create", ("update",): "update", ("delete",): "delete",
              ("delete", "create"): "replace", ("create", "delete"): "replace"}


def _mask(value, sensitive):
    if sensitive is True:
        return "(sensitive)"
    if isinstance(value, dict) and isinstance(sensitive, dict):
        return {k: _mask(v, sensitive.get(k)) for k, v in value.items()}
    if isinstance(value, list) and isinstance(sensitive, list):
        return [_mask(v, sensitive[i] if i < len(sensitive) else None) for i, v in enumerate(value)]
    return value


def terraform_resources(show: dict) -> list[dict]:
    out = []
    for rc in show.get("resource_changes") or []:
        ch = rc.get("change") or {}
        acts = tuple(ch.get("actions") or [])
        if acts in (("no-op",), ("read",), ()):
            continue
        action = TF_ACTIONS.get(acts, "+".join(acts))
        out.append(_res(rc["address"], rc.get("type", "?"), rc.get("name", "?"), action,
                        before=_mask(ch.get("before"), ch.get("before_sensitive")),
                        after=_mask(ch.get("after"), ch.get("after_sensitive"))))
    return out


def terraform_inverse(resources: list[dict], tf_dir: str, var_file: str | None) -> dict:
    steps = []
    for r in resources:
        if r["action"] == "create":
            steps.append({"address": r["address"], "action": "delete",
                          "how": ["terraform", f"-chdir={tf_dir}", "plan", "-destroy", f"-target={r['address']}",
                                  "-out=inverse.tfplan"] + ([f"-var-file={var_file}"] if var_file else [])})
        elif r["action"] == "update":
            steps.append({"address": r["address"], "action": "restore", "restore": r.get("before")})
        else:  # delete / replace
            steps.append({"address": r["address"], "action": "recreate", "restore": r.get("before"),
                          "warning": "recreating does not restore data; take/verify a backup before applying"})
    return {"strategy": "revert the change in code (git revert) and re-plan; the per-resource steps below are "
                        "the expected inverse and must match that re-plan", "steps": steps}


def plan_terraform(spec: dict, env: str, run: Runner, out: Path, base: Path) -> dict:
    tf = spec.get("terraform") or {}
    if not tf.get("dir"):
        raise Refused("change.terraform.dir is required")
    tf_dir = str((base / tf["dir"]).resolve())
    var_file = per_env(tf.get("var_file"), env)
    var_file = str((base / var_file).resolve()) if var_file else None
    extra_env = {"TF_IN_AUTOMATION": "1", "TF_INPUT": "0"}
    ws = per_env(tf.get("workspace"), env)
    if ws:
        extra_env["TF_WORKSPACE"] = ws
    plan_bin = out / "plan.tfplan"
    p = run.run(["terraform", f"-chdir={tf_dir}", "init", "-input=false"], extra_env=extra_env)
    if p.returncode != 0:
        raise Refused(f"terraform init failed: {redact(p.stderr.strip())[:400]}", 4)
    argv = ["terraform", f"-chdir={tf_dir}", "plan", "-input=false", "-lock-timeout=60s", f"-out={plan_bin}"]
    if var_file:
        argv.append(f"-var-file={var_file}")
    p = run.run(argv, extra_env=extra_env)
    if p.returncode != 0:
        raise Refused(f"terraform plan failed: {redact(p.stderr.strip())[:400]}", 4)
    if plan_bin.exists():
        os.chmod(plan_bin, 0o600)
    s = run.run(["terraform", f"-chdir={tf_dir}", "show", "-json", str(plan_bin)], extra_env=extra_env)
    if s.returncode != 0:
        raise Refused(f"terraform show failed: {redact(s.stderr.strip())[:400]}", 4)
    show = json.loads(s.stdout)
    resources = terraform_resources(show)
    write_artifact(out / "plan.json", json.dumps({"resources": resources, "native": _mask_show(show)}, indent=2))
    verify_argv = ["terraform", f"-chdir={tf_dir}", "plan", "-input=false", "-detailed-exitcode", "-lock=false"] + \
        ([f"-var-file={var_file}"] if var_file else [])
    return {"resources": resources, "native_bytes": plan_bin.read_bytes() if plan_bin.exists() else s.stdout.encode(),
            "inverse": terraform_inverse(resources, tf_dir, var_file),
            "state_check": {"id": "state_matches_plan", "kind": "command", "argv": verify_argv,
                            "expect_exit": 0, "env": {k: v for k, v in extra_env.items() if k == "TF_WORKSPACE"},
                            "means": "no remaining diff after apply"},
            "target": ws or env}


def _mask_show(show: dict) -> dict:
    show = json.loads(json.dumps(show))
    for rc in show.get("resource_changes") or []:
        ch = rc.get("change") or {}
        for side in ("before", "after"):
            ch[side] = _mask(ch.get(side), ch.get(f"{side}_sensitive"))
    show.pop("prior_state", None)  # may carry unmasked values; the per-resource `before` is kept instead
    show.pop("variables", None)
    return show


# --------------------------------------------------------------------------- kubernetes

DIFF_HDR = re.compile(r"^diff -u -N (\S+) (\S+)$")


def parse_k8s_object(fname: str) -> tuple[str, str, str]:
    """`apps.v1.Deployment.api.web` -> (Deployment, api, web). Namespace '' when cluster-scoped."""
    parts = Path(fname).name.split(".")
    for i, p in enumerate(parts):
        if p[:1].isupper():
            ns = parts[i + 1] if i + 1 < len(parts) else ""
            return p, ns, ".".join(parts[i + 2:])
    return "Unknown", "", Path(fname).name


def kubectl_diff_resources(text: str) -> list[dict]:
    out, cur, body = [], None, []

    def flush():
        if cur is None:
            return
        kind, ns, name = cur
        hunks = [l for l in body if l.startswith("@@")]
        blob = "\n".join(body)
        if hunks and all(h.startswith("@@ -0,0 ") for h in hunks):
            action = "create"
        elif hunks and all(re.match(r"@@ -\d+(,\d+)? \+0,0 @@", h) for h in hunks):
            action = "delete"
        else:
            action = "update"
        out.append(_res(f"{kind}/{ns + '/' if ns else ''}{name}", kind, name, action, namespace=ns, extra=blob))

    for line in text.splitlines():
        m = DIFF_HDR.match(line)
        if m:
            flush()
            cur, body = parse_k8s_object(m.group(1)), []
        elif cur is not None:
            body.append(line)
    flush()
    return out


def snapshot_k8s(resources, run: Runner, ctx_args: list[str], out: Path) -> list[dict]:
    """Inverse steps; saves the live object of everything updated/deleted."""
    steps = []
    for r in resources:
        ns_args = ["-n", r["namespace"]] if r.get("namespace") else []
        if r["action"] == "create":
            steps.append({"resource": r["address"], "action": "delete",
                          "precondition": "kubectl diff against the snapshot shows only this object",
                          "argv": ["kubectl", *ctx_args, "delete", r["type"].lower(), r["name"], *ns_args]})
            continue
        p = run.run(["kubectl", *ctx_args, "get", r["type"].lower(), r["name"], *ns_args, "-o", "yaml"])
        if p.returncode != 0:
            raise Refused(f"could not snapshot {r['address']} for the inverse plan: {p.stderr.strip()[:200]}", 4)
        snap = write_artifact(out / "snapshots" / f"{r['type']}.{r.get('namespace') or '_'}.{r['name']}.yaml",
                              _strip_live_fields(p.stdout))
        steps.append({"resource": r["address"], "action": "restore", "manifest": str(snap),
                      "argv": ["kubectl", *ctx_args, "diff", "-f", str(snap)],
                      "then": ["kubectl", *ctx_args, "apply", "-f", str(snap)]})
    return steps


def _strip_live_fields(yaml_text: str) -> str:
    drop = ("resourceVersion:", "uid:", "creationTimestamp:", "generation:", "managedFields:")
    out, skip_indent = [], None
    for line in yaml_text.splitlines():
        indent = len(line) - len(line.lstrip())
        if skip_indent is not None:
            if line.strip() and indent <= skip_indent:
                skip_indent = None
            else:
                continue
        s = line.strip()
        if s.startswith("status:") and indent == 0:
            skip_indent = 0
            continue
        if any(s.startswith(d) for d in drop):
            if s.startswith("managedFields:"):
                skip_indent = indent
            continue
        out.append(line)
    return "\n".join(out) + "\n"


def plan_kubernetes(spec: dict, env: str, run: Runner, out: Path, base: Path, target: str) -> dict:
    k = spec.get("kubernetes") or {}
    if not k.get("manifests"):
        raise Refused("change.kubernetes.manifests is required")
    ctx = per_env(k.get("context"), env) or target
    ctx_args = ["--context", ctx]
    ns_args = ["-n", k["namespace"]] if k.get("namespace") else []
    manifests = str((base / k["manifests"]).resolve())
    argv = ["kubectl", *ctx_args, "diff", "-f", manifests, *ns_args]
    p = run.run(argv)
    if p.returncode not in (0, 1):
        raise Refused(f"kubectl diff failed: {redact(p.stderr.strip())[:400]}", 4)
    write_artifact(out / "plan.diff", p.stdout)
    resources = kubectl_diff_resources(p.stdout)
    write_artifact(out / "plan.json", json.dumps({"resources": resources}, indent=2))
    inverse = {"strategy": "restore the snapshotted live objects (diff first, then apply); "
                           "delete created objects only after a diff", "steps": snapshot_k8s(resources, run, ctx_args, out)}
    return {"resources": resources, "native_bytes": p.stdout.encode(), "inverse": inverse,
            "state_check": {"id": "state_matches_plan", "kind": "command", "argv": argv, "expect_exit": 0,
                            "means": "kubectl diff is empty after apply"},
            "target": ctx}


# --------------------------------------------------------------------------- helm

HELM_LINE = re.compile(r"^(\S*), (\S+), (\w+) \(([^)]*)\) has (changed|been added|been removed):")
HELM_ACTION = {"changed": "update", "been added": "create", "been removed": "delete"}


def helm_diff_resources(text: str) -> list[dict]:
    out, cur, body = [], None, []

    def flush():
        if cur:
            ns, name, kind, action = cur
            out.append(_res(f"{kind}/{ns + '/' if ns else ''}{name}", kind, name, action, namespace=ns,
                            extra="\n".join(body)))

    for line in text.splitlines():
        m = HELM_LINE.match(line.strip())
        if m:
            flush()
            cur, body = (m.group(1), m.group(2), m.group(3), HELM_ACTION[m.group(5)]), []
        elif cur:
            body.append(re.sub(r"^\s+([+-])", r"\1", line))
    flush()
    return out


def plan_helm(spec: dict, env: str, run: Runner, out: Path, base: Path, target: str) -> dict:
    h = spec.get("helm") or {}
    for key in ("release", "chart"):
        if not h.get(key):
            raise Refused(f"change.helm.{key} is required")
    ctx = per_env(h.get("context"), env) or target
    ctx_args = ["--kube-context", ctx]
    ns_args = ["-n", h["namespace"]] if h.get("namespace") else []
    chart = h["chart"]
    if (base / chart).exists():
        chart = str((base / chart).resolve())
    values = per_env(h.get("values"), env)
    val_args = ["-f", str((base / values).resolve())] if values else []
    argv = ["helm", *ctx_args, "diff", "upgrade", h["release"], chart, *ns_args, *val_args,
            "--allow-unreleased", "--no-color", "--detailed-exitcode"]
    p = run.run(argv)
    if p.returncode not in (0, 2):
        raise Refused(f"helm diff failed: {redact(p.stderr.strip())[:400]}", 4)
    write_artifact(out / "plan.diff", p.stdout)
    resources = helm_diff_resources(p.stdout)
    write_artifact(out / "plan.json", json.dumps({"resources": resources}, indent=2))
    hist = run.run(["helm", *ctx_args, "history", h["release"], *ns_args, "--max", "1", "-o", "json"])
    revision = None
    if hist.returncode == 0 and hist.stdout.strip():
        try:
            revision = (json.loads(hist.stdout) or [{}])[-1].get("revision")
        except ValueError:
            revision = None
    if revision:
        steps = [{"action": "rollback", "revision": revision,
                  "argv": ["helm", *ctx_args, "rollback", h["release"], str(revision), *ns_args]}]
    else:
        steps = [{"action": "uninstall", "precondition": "helm diff shows only this release's objects",
                  "argv": ["helm", *ctx_args, "uninstall", h["release"], *ns_args]}]
    return {"resources": resources, "native_bytes": p.stdout.encode(),
            "inverse": {"strategy": "helm rollback to the revision live before the apply", "steps": steps},
            "state_check": {"id": "state_matches_plan", "kind": "command",
                            "argv": [a for a in argv if a != "--allow-unreleased"], "expect_exit": 0,
                            "means": "helm diff is empty after apply"},
            "target": ctx}


# --------------------------------------------------------------------------- imperative

def plan_imperative(spec: dict, env: str, run: Runner, out: Path, base: Path, target: str) -> dict:
    im = spec.get("imperative") or {}
    dry = per_env(im.get("dry_run"), env)
    inverse = per_env(im.get("inverse"), env)
    if not dry:
        raise Refused(f"change.imperative.dry_run.{env} is required (plan, don't act)")
    if not inverse:
        raise Refused(f"change.imperative.inverse.{env} is required: no imperative change without an inverse")
    check_argv(dry, f"imperative.dry_run.{env}")
    check_argv(inverse, f"imperative.inverse.{env}")
    if im.get("cli") and dry[0] != im["cli"]:
        raise Refused(f"imperative.dry_run.{env} must invoke {im['cli']!r}")
    effects = im.get("effects") or []
    if not effects:
        raise Refused("change.imperative.effects[] is required: declare every resource the command touches")
    p = run.run(dry, cwd=str(base))
    if p.returncode != 0:
        raise Refused(f"imperative dry run failed: {redact(p.stderr.strip())[:400]}", 4)
    write_artifact(out / "plan.txt", p.stdout)
    resources = [_res(f"{e.get('type', '?')}/{e.get('name', '?')}", e.get("type", "?"), e.get("name", "?"),
                      e.get("action", "unknown")) for e in effects]
    write_artifact(out / "plan.json", json.dumps({"resources": resources, "dry_run": dry}, indent=2))
    verify = per_env(im.get("verify"), env)
    check = ({"id": "state_matches_plan", "kind": "command", "argv": check_argv(verify, "imperative.verify"),
              "expect_exit": 0, "means": "declared post-condition holds"} if verify else
             {"id": "state_matches_plan", "kind": "manual",
              "means": "compare live state with the declared effects; no verify command was declared"})
    return {"resources": resources, "native_bytes": p.stdout.encode(),
            "inverse": {"strategy": "run the declared inverse command", "steps": [{"action": "inverse", "argv": inverse}]},
            "state_check": check, "target": target}


# --------------------------------------------------------------------------- KB, credentials, state

def resolve_env(kb_dir: Path, system: str, env: str) -> tuple[dict, KB.Doc]:
    doc = KB.load_system(kb_dir, system)
    if doc is None:
        raise Refused(f"STOP and ask: system {system!r} is not in the knowledge base ({kb_dir}/systems/); "
                      f"record it and its environments (`kb.py add-system`, `kb.py add-env`) first", 3)
    envs = ((doc.meta.get("runtime") or {}).get("environments")) or []
    match = [e for e in envs if isinstance(e, dict) and e.get("name") == env]
    if not match:
        raise Refused(f"STOP and ask: no `{env}` environment for {system!r} in the KB "
                      f"(runtime.environments[] has {[e.get('name') for e in envs if isinstance(e, dict)]}); "
                      f"ask the user which resources are {env} and record them with `kb.py add-env` - never guess", 3)
    e = match[0]
    if not e.get("id"):
        raise Refused(f"STOP and ask: the KB `{env}` environment of {system!r} has no id", 3)
    return e, doc


def blast_radius(kb_dir: Path, system: str, doc: KB.Doc, env_entry: dict, resources: list[dict]) -> dict:
    conns = KB.Connections.load(kb_dir)
    edges = [r for r in conns.rows if system in (r["from"], r["to"])]
    neighbours = sorted({r["to"] if r["from"] == system else r["from"] for r in edges} - {system})
    return {"systems": [system] + neighbours, "direct": system, "dependents": sorted({r["from"] for r in edges if r["to"] == system}),
            "dependencies": sorted({r["to"] for r in edges if r["from"] == system}),
            "edges": [{"from": r["from"], "to": r["to"], "protocol": r["protocol"]} for r in edges],
            "environment": {"name": env_entry["name"], "id": env_entry["id"], "url": env_entry.get("url")},
            "resources": len(resources),
            "by_action": {a: sum(1 for r in resources if r["action"] == a)
                          for a in sorted({r["action"] for r in resources})},
            "monitoring": [f"{m.get('kind')}:{m.get('project') or m.get('url') or ''}"
                           for m in (doc.meta.get("monitoring") or []) if isinstance(m, dict)]}


def verify_checks(state_check: dict, env_entry: dict, blast: dict, env: str) -> list[dict]:
    checks = [state_check]
    if env_entry.get("url"):
        checks.append({"id": "health", "kind": "http", "url": env_entry["url"], "expect": "2xx",
                       "means": f"{env} endpoint healthy after apply"})
    for e in blast["edges"]:
        checks.append({"id": f"connection:{e['from']}->{e['to']}:{e['protocol']}", "kind": "kb_connection",
                       **e, "means": "the KB connection still resolves (DNS/endpoint reachable from its consumer)"})
    if blast["monitoring"]:
        checks.append({"id": "monitoring_soak", "kind": "monitoring_soak", "monitors": blast["monitoring"],
                       "minutes": SOAK_MINUTES[env], "means": "no new errors/alerts vs. the pre-apply baseline"})
    return checks


def credentials(spec: dict, platform: str, env: str) -> tuple[str, str, str | None]:
    """(write-cred var name, child env var, value). Refuses missing or shared credentials."""
    cp = (spec.get("credential_platform") or platform).upper().replace("-", "_")
    if not re.match(r"^[A-Z][A-Z0-9_]*$", cp):
        raise Refused(f"bad credential_platform {cp!r}")
    name, other = f"{cp}_WRITE_{env.upper()}", f"{cp}_WRITE_{OTHER[env].upper()}"
    value = os.environ.get(name)
    if not value:
        raise Refused(f"write credential {{{{env.{name}}}}} is not set. Infra changes use a separate, "
                      f"{env}-scoped write credential; never the read-only discovery one")
    if os.environ.get(other) and os.environ.get(other) == value:
        raise Refused(f"cross-env credential: {name} and {other} are the same credential; "
                      f"each environment needs its own write credential")
    for k, v in os.environ.items():
        if k != name and v == value and re.search(r"_READ(_|$)", k):
            raise Refused(f"{name} equals the discovery credential {k}; a write credential must be separate")
    cred_env = spec.get("credential_env") or DEFAULT_CRED_ENV.get(platform)
    if not cred_env:
        raise Refused("change.credential_env is required: the env var the native CLI reads the credential from "
                      "(e.g. AWS_PROFILE, GOOGLE_APPLICATION_CREDENTIALS, FLY_API_TOKEN)")
    return name, cred_env, value


def probe_cross_env(spec: dict, platform: str, env: str, run: Runner, other_target: str | None,
                    namespace: str | None) -> dict:
    """The staging credential must be denied on prod (and vice versa): an allowed probe refuses."""
    if other_target is None:
        return {"probed": False, "reason": f"no {OTHER[env]} environment in the KB"}
    probe = spec.get("cross_env_probe")
    if probe:
        argv = [a.replace("{target}", other_target) for a in check_argv(probe.get("argv"), "cross_env_probe.argv")]
        pattern = probe.get("allowed_pattern")
    elif platform in ("kubernetes", "helm"):
        argv = ["kubectl", "--context", other_target, "auth", "can-i", "create", "deployments"] + \
            (["-n", namespace] if namespace else [])
        pattern = r"^yes\b"
    else:
        raise Refused(f"change.cross_env_probe is required for {platform}: an attempted write against "
                      f"{OTHER[env]} ({{target}}) that must be denied for the {env} credential")
    p = run.run(argv)
    text = (p.stdout or "") + (p.stderr or "")
    allowed = bool(re.search(pattern, text, re.M)) if pattern else p.returncode == 0
    if allowed:
        raise Refused(f"cross-env credential: the {env} write credential is allowed on {OTHER[env]} "
                      f"({other_target}); scope it to {env} before planning")
    return {"probed": True, "target": other_target, "denied": True}


def change_hash(spec: dict, base: Path) -> str:
    """Env-independent hash of the change: the change file plus every source file it references.
    Staging and prod plans of the same change share it; any edit between them changes it."""
    h = hashlib.sha256(canon(spec))
    refs = []
    for sect, keys in (("terraform", ("dir", "var_file")), ("kubernetes", ("manifests",)),
                       ("helm", ("chart", "values"))):
        s = spec.get(sect) or {}
        for k in keys:
            v = s.get(k)
            refs += list(v.values()) if isinstance(v, dict) else ([v] if v else [])
    for ref in sorted(set(refs)):
        p = (base / ref)
        files = sorted(x for x in p.rglob("*") if x.is_file() and ".terraform" not in x.parts) if p.is_dir() \
            else ([p] if p.is_file() else [])
        for f in files:
            h.update(str(f.relative_to(base)).encode() + b"\0" + f.read_bytes())
    return "sha256:" + h.hexdigest()


def check_state(state_file: Path, cid: str, env: str, phash: str, platform: str) -> dict | None:
    state = None
    if state_file.exists():
        with ST.locked(state_file):
            state = ST.load(state_file)
    change = ((state or {}).get("infra_changes") or {}).get(cid)
    if change and change.get("platform") and change["platform"] != platform:
        raise Refused(f"change {cid} was classified for platform {change['platform']!r}, not {platform!r}")
    steps = (change or {}).get("steps") or {}
    if env == "staging":
        if steps.get("applied_staging") in ("in_progress", "done", "blocked"):
            raise Refused(f"change {cid}: staging was already applied ({steps['applied_staging']}); "
                          f"never re-plan or re-apply it. Continue with `state.py change-next {cid}`")
        return change
    if not change or steps.get("verified_staging") != "done":
        raise Refused(f"refused: --env prod requires `verified_staging: done` for change {cid} in {state_file} "
                      f"(currently {steps.get('verified_staging', 'no record')}); staging always goes first")
    if change.get("plan_hash") != phash:
        raise Refused(f"refused: change {cid} was verified on staging with plan hash {change.get('plan_hash')}, "
                      f"but the change now hashes to {phash}; the staging-verified change is not the one "
                      f"being planned for prod. Open a new change id and start again at staging")
    if change.get("env_scope") != "staging+prod":
        raise Refused(f"refused: the ask for change {cid} did not include prod (env_scope "
                      f"{change.get('env_scope')!r}); prod needs a new explicit ask")
    if steps.get("applied_prod") in ("in_progress", "done", "blocked"):
        raise Refused(f"change {cid}: prod was already applied ({steps['applied_prod']}); never re-apply")
    return change


def plan_diff(staging: list[dict], prod: list[dict]) -> dict:
    def key(r):
        return (r["type"], r["name"])
    s = {key(r): r for r in staging}
    p = {key(r): r for r in prod}
    return {"only_in_prod": [p[k]["address"] for k in sorted(p.keys() - s.keys())],
            "only_in_staging": [s[k]["address"] for k in sorted(s.keys() - p.keys())],
            "action_differs": [{"resource": p[k]["address"], "staging": s[k]["action"], "prod": p[k]["action"]}
                               for k in sorted(p.keys() & s.keys()) if s[k]["action"] != p[k]["action"]],
            "identical": sorted(p.keys()) == sorted(s.keys()) and all(s[k]["action"] == p[k]["action"] for k in p)}


# --------------------------------------------------------------------------- main

def plan(args) -> dict:
    platform = ALIASES.get(args.platform, args.platform)
    if platform not in PLATFORMS:
        raise Refused(f"unknown platform {args.platform!r}; expected one of {', '.join(PLATFORMS)}", 2)
    change_file = Path(args.change).resolve()
    try:
        spec = json.loads(change_file.read_text())
    except (OSError, ValueError) as e:
        raise Refused(f"cannot read change file {change_file}: {e}", 2)
    base = change_file.parent
    KB.refuse_secrets(json.dumps(spec))
    scan_forbidden(spec)
    cid, system = spec.get("id"), spec.get("system")
    if not cid or not CHANGE_ID_RE.match(cid):
        raise Refused("change.id must be a lowercase slug (e.g. chg-20260930-api-db-size)")
    if not system:
        raise Refused("change.system (KB system id) is required")
    if spec.get("platform") and ALIASES.get(spec["platform"], spec["platform"]) != platform:
        raise Refused(f"--platform {platform} does not match change.platform {spec['platform']!r}")
    env = args.env
    root = Path(args.root).resolve()
    state_file = Path(args.state or os.environ.get("FACTORY_STATE") or root / ST.DEFAULT_FILE)
    kb_dir = Path(args.kb or os.environ.get("KB_DIR") or KB.DEFAULT_KB).expanduser().resolve()

    phash = change_hash(spec, base)
    check_state(state_file, cid, env, phash, platform)           # prod gate before touching anything
    env_entry, doc = resolve_env(kb_dir, system, env)            # unknown env: STOP and ask
    try:
        other_entry, _ = resolve_env(kb_dir, system, OTHER[env])
    except Refused:
        if env == "prod":
            raise
        other_entry = None
    cred_name, cred_env, cred_value = credentials(spec, platform, env)
    run = Runner(cred_env, cred_value)

    sect = spec.get(platform) or {}
    target_of = lambda e: (per_env(sect.get("context"), e["name"]) if e else None) or (e or {}).get("id")  # noqa: E731
    probe = probe_cross_env(spec, platform, env, run, target_of(other_entry) if other_entry else None,
                            sect.get("namespace"))

    out = root / ".agents" / "infra-plans" / cid / env
    if out.exists():
        shutil.rmtree(out)  # a re-plan replaces the previous (unapplied) plan for this env
    out.mkdir(parents=True)
    ignore = root / ".agents" / "infra-plans" / ".gitignore"
    if not ignore.exists():
        ignore.write_text("# plan binaries can embed provider data; never commit them\n*.tfplan\nsnapshots/\n")

    if platform == "terraform":
        res = plan_terraform(spec, env, run, out, base)
    elif platform == "kubernetes":
        res = plan_kubernetes(spec, env, run, out, base, target_of(env_entry))
    elif platform == "helm":
        res = plan_helm(spec, env, run, out, base, target_of(env_entry))
    else:
        res = plan_imperative(spec, env, run, out, base, target_of(env_entry))

    resources = res["resources"]
    classification, reasons = overall(resources)
    blast = blast_radius(kb_dir, system, doc, env_entry, resources)
    inverse_path = write_artifact(out / "inverse.json", json.dumps(
        {"id": cid, "env": env, "platform": platform, "target": res["target"], **res["inverse"]}, indent=2))
    result = {
        "id": cid, "env": env, "platform": platform, "system": system, "summary": spec.get("summary"),
        "target": res["target"], "classification": classification, "reasons": reasons,
        "resources": [{k: v for k, v in r.items() if k not in ("before", "after")} for r in resources],
        "blast_radius": blast, "plan_hash": phash, "native_plan_sha256": sha256(res["native_bytes"]),
        "plan_path": str(out / "plan.json"), "inverse_plan_path": str(inverse_path),
        "verify_checks": verify_checks(res["state_check"], env_entry, blast, env),
        "credential_env_var": cred_name, "cross_env_probe": probe,
    }
    if env == "prod":
        staging_plan = out.parent / "staging" / "plan.json"
        if not staging_plan.exists():
            raise Refused(f"refused: no staging plan at {staging_plan} to diff against; staging goes first")
        result["plan_diff"] = plan_diff(json.loads(staging_plan.read_text())["resources"], resources)
        if classification == "destructive":
            result["confirm_by_typing"] = sorted({r["name"] for r in resources if r["classification"] == "destructive"})
    elif classification == "destructive":
        result["confirm_by_typing"] = sorted({r["name"] for r in resources if r["classification"] == "destructive"})
    step = f"planned_{env}"
    extra = (f" --platform {platform} --system {system} --plan-hash {phash} --inverse-plan-path {inverse_path}"
             if env == "staging" else "")
    result["state_cmd"] = f"state.py set-change {cid} {step} done{extra}"
    write_artifact(out / "summary.json", json.dumps(result, indent=2))
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="plan (never apply) one infra change for one environment")
    ap.add_argument("--platform", required=True, help="terraform | kubernetes (k8s) | helm | imperative")
    ap.add_argument("--env", required=True, choices=ENVS)
    ap.add_argument("--change", required=True, help="change file (JSON)")
    ap.add_argument("--kb", help="knowledge-base checkout (default $KB_DIR or ~/workspaces/knowledge)")
    ap.add_argument("--state", help="factory state file (default $FACTORY_STATE or <root>/.agents/factory-state.json)")
    ap.add_argument("--root", default=".", help="workspace root; plans go to <root>/.agents/infra-plans/")
    a = ap.parse_args(argv)
    try:
        print(json.dumps(plan(a), indent=2))
    except Refused as e:
        print(f"infra_plan.py: {e}", file=sys.stderr)
        return e.code
    except (KB.KBError, ST.StateError) as e:
        print(f"infra_plan.py: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
