# Infrastructure platforms: detection, read-only access, probes, KB entries

Used by factory phase 4c (infra, read-only discovery, optional). It follows
the same detect -> confirm -> connect -> ingest pattern as trackers
(`references/trackers.md`) and monitoring (`references/monitoring.md`), with
one extra step: a **verification probe** between connect and ingest.

Discovery is **strictly read-only**. Factory never creates, changes or
deletes infrastructure during setup or in routines. A routine may only file
an `infra-change` board issue. Changes happen only on an explicit user ask,
through `references/infra-changes.md`, with a separate env-scoped write
credential. That credential is never the one described here.

`scripts/detect_infra.sh` ranks candidates from the repos cloned in phase 2.
It is offline: it never runs a cloud CLI, never touches the network and never
reads `.env`, `*.tfvars`, `*.tfstate` or kubeconfig files. Its confirmation
goes into the **same batched phase-4 prompt** as the tracker and monitoring
questions.

`scripts/infra_graph.py` reads a platform only after the probe for that
platform has been **denied**. It records runtime dependencies as
`infra.json` (schema: `references/knowledge-base.md`), with every edge's
`source` set to `infra:<platform>:<resource>`.

Every role, policy, command and URL below was checked against the vendor's
docs or canonical repo on 2026-09-30. Re-check the **Source** link before you
change an entry.

## Credentials

- The read credential lives in the user's environment and is named in this
  file as `{{env.VAR}}`. Factory never asks for the value in chat, and never
  writes it to `mcp.json`, `state.py`, the KB or a log.
- CLIs (`kubectl`, `aws`, `gcloud`, `terraform`, `gh`, ...) read these
  variables themselves; factory only checks that the variable is **set**
  (`[ -n "${VAR+x}" ]`), never prints it.
- `mcp_add` credential handling is the same as for trackers (see "How Forge
  stores MCP credentials" in `references/trackers.md`): `bearer_token` and
  `headers` expand `{{env.VAR}}`, but **stdio `env` is passed literally**. For
  a stdio MCP server, the secret must be exported in the user's shell so the
  child process inherits it. Only non-secret settings go in `env`.
- Prefer a **dedicated** read identity (ServiceAccount, IAM role, GCP service
  account, scoped API token) over the user's personal admin login. If the
  user only has an admin login, the probe refuses it; tell them which read
  role to create (below) and mark 4c `skipped` for that platform.

## Verification probe (all platforms)

Before any read, factory runs the platform's probe: **an attempted write that
must be denied**.

| Probe result | Meaning | Factory does |
|---|---|---|
| denied (403 / Forbidden / AccessDenied / UnauthorizedOperation) | the credential cannot write | `passed`: read the platform |
| allowed (exit 0, or AWS `DryRunOperation`) | the credential **can write** | `refused`: read nothing, exit 3, tell the user to supply a read-only credential |
| anything else (network error, CLI missing, unknown error) | cannot tell | `inconclusive`: read nothing, exit 4, report the stderr tail |

Every probe is non-mutating even when it is allowed: Kubernetes
`--dry-run=server` and AWS `--dry-run` are authorised exactly like the real
call but never persist anything. For platforms without a dry-run write, the
probe is a scope/role inspection that must show no write permission. The
probe result is recorded in `infra.json` (`platforms.<p>.probe`) and in
`state.py`. **Factory refuses the platform if the probe succeeds**, and it
re-runs the probe on every resume and in every routine before reading.

## Summary

| Platform | Detection (`detect_infra.sh` platform id) | Least-privilege read | Credential | Probe (must be denied) | MCP / CLI | Outbound webhooks | `infra_graph.py` |
|---|---|---|---|---|---|---|---|
| Kubernetes | `kubernetes` | `view` ClusterRole, or the `factory-read` ClusterRole below (`get,list,watch`) | `{{env.KUBECONFIG}}` (+ `--context`) | `kubectl create deployment factory-probe ... --dry-run=server` must be Forbidden; `kubectl auth can-i create deployments --all-namespaces` must print `no` | `kubectl`; MCP `containers/kubernetes-mcp-server` with `read_only = true` | No (audit webhook backend is control-plane config) | yes, `--platform kubernetes` |

## Entries

### Kubernetes

- **Detection** (`kubernetes`): `Chart.yaml` (0.65), `kustomization.yaml` /
  `skaffold.yaml` / `Tiltfile` / `helmfile.yaml` (0.6), resource kinds
  `Deployment|StatefulSet|DaemonSet|Service|Ingress|NetworkPolicy|CronJob|HorizontalPodAutoscaler|Gateway|HTTPRoute`
  in YAML (0.6), workflow deploy steps (`kubectl apply|rollout|set|...`,
  `helm upgrade|install`, `azure/k8s-*`, `google-github-actions/get-gke-credentials`)
  (0.55), Terraform `hashicorp/kubernetes|helm` provider (0.55), `k8s/` or
  `kubernetes/` dir (0.5), `helm/` / `charts/` dir (0.4), SDKs
  `@kubernetes/client-node`, `kube` / `k8s-openapi`, `kubernetes`,
  `k8s.io/client-go` (0.5), env names `KUBECONFIG`, `KUBE_*`, `HELM_*`.
  Hints: `namespace`, `chart`.
- **Read role** (source:
  [RBAC user-facing roles](https://kubernetes.io/docs/reference/access-authn-authz/rbac/#user-facing-roles)):
  the built-in **`view`** ClusterRole "allows read-only access to see most
  objects in a namespace" and **does not allow viewing Secrets**. Bind it to a
  dedicated ServiceAccount:

  ```sh
  kubectl create serviceaccount factory-read -n kube-system
  kubectl create clusterrolebinding factory-read --clusterrole=view \
    --serviceaccount=kube-system:factory-read
  # namespace-scoped instead: kubectl create rolebinding factory-read \
  #   --clusterrole=view --serviceaccount=kube-system:factory-read -n <ns>
  ```

  Tighter alternative: exactly what `infra_graph.py` reads, nothing else
  (no Secrets, no ConfigMaps, no Pods/logs, no exec):

  ```yaml
  apiVersion: rbac.authorization.k8s.io/v1
  kind: ClusterRole
  metadata: {name: factory-read}
  rules:
  - apiGroups: [""]
    resources: [services, namespaces]
    verbs: [get, list, watch]
  - apiGroups: [apps]
    resources: [deployments, statefulsets, daemonsets]
    verbs: [get, list, watch]
  - apiGroups: [batch]
    resources: [cronjobs, jobs]
    verbs: [get, list, watch]
  - apiGroups: [networking.k8s.io]
    resources: [ingresses, networkpolicies]
    verbs: [get, list, watch]
  ```

  Bind with `kubectl create clusterrolebinding factory-read --clusterrole=factory-read --serviceaccount=kube-system:factory-read`.
  Creating the role/binding is itself a write; the **user** runs it, factory
  only shows it.
- **Credential**: a kubeconfig for that ServiceAccount at
  `{{env.KUBECONFIG}}` (the user exports `KUBECONFIG=<path>`), selected with
  `infra_graph.py --context <ctx>`. The kubeconfig file itself is never read
  by factory or `detect_infra.sh`.
- **Probe** (must match `probe_kubernetes` in `infra_graph.py`):

  ```sh
  kubectl [--context <ctx>] create deployment factory-probe \
    --image=registry.invalid/factory-probe -n <first --namespace or default> \
    --dry-run=server -o name
  ```

  Must fail with `Forbidden`. [Dry-run authorization](https://kubernetes.io/docs/reference/using-api/api-concepts/#dry-run-authorization)
  is identical to the real request, so a denied dry run proves the write is
  denied, and an allowed one persists nothing. The human-readable
  cross-check shown to the user is
  [`kubectl auth can-i`](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_auth/kubectl_auth_can-i/):
  `kubectl auth can-i create deployments --all-namespaces` must print `no`.
  Exit 0 from the dry run = `refused` (exit 3).
- **MCP**: [`containers/kubernetes-mcp-server`](https://github.com/containers/kubernetes-mcp-server)
  (Red Hat; Kubernetes and OpenShift). Stdio: `command: npx`,
  `args: ["-y", "kubernetes-mcp-server@latest", "--config", "<file>"]` with
  `read_only = true` in the TOML config ("only exposes tools annotated with
  `readOnlyHint=true`", [configuration reference](https://github.com/containers/kubernetes-mcp-server/blob/main/docs/configuration.md)).
  It uses the kubeconfig inherited from the shell. Server-side `read_only` is
  defence in depth; the RBAC role is the real control. Otherwise: `kubectl`.
- **Outbound webhooks**: **No** event webhooks for workloads. The
  [audit webhook backend](https://kubernetes.io/docs/tasks/debug/debug-cluster/audit/#webhook-backend)
  is API-server configuration (not available on most managed clusters).
  Drift intake is polling (`infra_graph.py` re-run by a routine).
- **`infra_graph.py --platform kubernetes`**: `kubectl get <kind> -o json
  --chunk-size=500` (paginated) for `deployments, statefulsets, daemonsets,
  cronjobs, services, ingresses, networkpolicies`, per `--namespace` or `-A`,
  `--jobs` in parallel. Resources: workloads (images, labels), Services
  (`<svc>.<ns>.svc.cluster.local`, ports, `ExternalName`), Ingress hosts.
  Edges: workload -> Service from literal `env[].value` hosts (short names
  normalised to cluster FQDN; `valueFrom` secrets/configmaps are **never**
  read), `external:<host>` -> Service from Ingress rules (`https` when in
  `tls`), workload -> workload from NetworkPolicy `podSelector` peers.
  Workloads map to repos via image `<reg>/<org>/<name>` and
  `app.kubernetes.io/{name,instance,part-of}` / `app` labels.
