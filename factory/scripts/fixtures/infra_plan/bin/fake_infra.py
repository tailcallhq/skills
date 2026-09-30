#!/usr/bin/env python3
"""Fake terraform / kubectl / helm / flyctl for test_infra_plan.py (symlinked by name).

Reads outputs from $FAKE_INFRA (a directory):
  tf_show.json      `terraform show -json` output (and `plan -out` writes a dummy binary)
  kubectl_diff.txt  `kubectl diff` output (exit 1 when non-empty, like kubectl)
  helm_diff.txt     `helm diff upgrade` output (exit 2 when non-empty, --detailed-exitcode)
  helm_history.json `helm history -o json`
  can_i.<context>   `kubectl auth can-i` answer for that context (default "no")
  probe.txt         output of any other CLI invocation (cross-env probes, dry runs)
Every call appends `<tool> <argv...> | cred=<names of *_WRITE_* / cred vars present>` to $FAKE_INFRA/calls.log.
"""
import json, os, sys
from pathlib import Path

tool = Path(sys.argv[0]).name
args = sys.argv[1:]
d = Path(os.environ["FAKE_INFRA"])
present = sorted(k for k in os.environ if "_WRITE_" in k or k in ("KUBECONFIG", "AWS_PROFILE", "FLY_API_TOKEN"))
with open(d / "calls.log", "a") as fh:
    fh.write(f"{tool} {' '.join(args)} | cred={','.join(present)} val={os.environ.get(os.environ.get('FAKE_CRED_VAR', ''), '')}\n")

def read(name, default=""):
    p = d / name
    return p.read_text() if p.exists() else default

if tool == "terraform":
    sub = [a for a in args if not a.startswith("-")]
    if sub[:1] == ["plan"]:
        for a in args:
            if a.startswith("-out="):
                Path(a[5:]).write_bytes(b"TFPLAN" + read("tf_show.json").encode())
        sys.exit(int(read("tf_plan_exit", "0")))
    if sub[:1] == ["show"]:
        print(read("tf_show.json"))
    sys.exit(0)

if tool == "kubectl":
    if "auth" in args and "can-i" in args:
        ctx = args[args.index("--context") + 1] if "--context" in args else ""
        ans = read(f"can_i.{ctx}", "no").strip()
        print(ans)
        sys.exit(0 if ans == "yes" else 1)
    if "diff" in args:
        out = read("kubectl_diff.txt")
        sys.stdout.write(out)
        sys.exit(1 if out.strip() else 0)
    if "get" in args:
        kind, name = args[args.index("get") + 1], args[args.index("get") + 2]
        print(f"apiVersion: v1\nkind: {kind}\nmetadata:\n  name: {name}\n  resourceVersion: \"42\"\n"
              f"  uid: abc\n  managedFields:\n  - manager: kubectl\nspec:\n  replicas: 3\nstatus:\n  ready: 3")
        sys.exit(0)
    sys.exit(0)

if tool == "helm":
    if "diff" in args:
        out = read("helm_diff.txt")
        sys.stdout.write(out)
        sys.exit(2 if out.strip() else 0)
    if "history" in args:
        print(read("helm_history.json", "[]"))
    sys.exit(0)

sys.stdout.write(read("probe.txt"))
sys.exit(int(read("probe_exit", "0")))
