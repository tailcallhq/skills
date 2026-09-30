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
| AWS | `aws` | scoped `Describe*/List*` policy below, or `ReadOnlyAccess` | `{{env.AWS_PROFILE}}` or `{{env.AWS_ACCESS_KEY_ID}}`/`{{env.AWS_SECRET_ACCESS_KEY}}`, `{{env.AWS_REGION}}` | `aws ec2 create-vpc --cidr-block 10.255.0.0/16 --dry-run` must return `UnauthorizedOperation` (not `DryRunOperation`) | `aws`; MCP `awslabs/mcp` AWS API server with `READ_OPERATIONS_ONLY=true` | Indirect (EventBridge API destinations, SNS HTTPS) | yes, `--platform aws` |
| GCP | `gcp` | `roles/viewer` (or `run.viewer`, `cloudsql.viewer`, `redis.viewer`, `pubsub.viewer`, `dns.reader`) | `{{env.CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT}}`, `{{env.CLOUDSDK_CORE_PROJECT}}` | `projects:testIamPermissions` for write permissions must return none | `gcloud`; MCP `googleapis/gcloud-mcp` | Indirect (Pub/Sub push) | via `--platform terraform` / `kubernetes` |
| Terraform | `terraform` (+ `backend` hint) | local: none; s3/gcs: bucket read via AWS/GCP read role; HCP Terraform: team token, workspace **Read** role | `{{env.TF_TOKEN_app_terraform_io}}` / `{{env.TFE_TOKEN}}`, or the backend cloud's vars | local: none; bucket: backend platform's probe then `--probe-passed`; HCP: workspace `permissions` write flags all `false` | `terraform show -json` (never `apply`); MCP `hashicorp/terraform-mcp-server` | HCP Terraform: yes (notifications); else no | yes, `--platform terraform` |
| Docker Compose | `compose` | none (file-only) | none | none (`not-needed`) | `docker compose config --no-interpolate` | No | no (`repo_graph.py` covers it) |
| Cloudflare / DNS | `cloudflare` | custom API token: Zone Read + DNS Read (zone-scoped) | `{{env.CLOUDFLARE_API_TOKEN}}` | `POST /zones/<id>/dns_records` must be 403 | `curl`/`wrangler`; MCP `https://mcp.cloudflare.com/mcp` | Yes (Notifications webhooks) | via `--platform terraform` |
| Vercel / Fly / Render | `vercel`, `fly`, `render` | Vercel: token from a **Viewer**-role account, team/project scoped; Fly: `fly tokens create readonly`; Render: **no read-only key exists** | `{{env.VERCEL_TOKEN}}`, `{{env.FLY_API_TOKEN}}`, `{{env.RENDER_API_KEY}}` | invalid write (env var / Machine / service create) must be 401/403, not 400 | CLI; MCP `mcp.vercel.com`, `fly mcp server`, `mcp.render.com` (not for discovery) | Vercel yes, Render yes, Fly no | no (KB `environments[]` from CLI/API) |
| GitHub Environments | `github-environments` | fine-grained PAT: Actions read + Deployments read + Metadata read (no Administration) | phase-0 `gh` login or `{{env.GH_TOKEN}}` | `gh api -X PUT .../environments/<env> -F wait_timer=-1` must be 403, not 422 | `gh api` | Yes (`deployment`, `deployment_status`) | no (KB `environments[]`) |

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

### AWS

- **Detection** (`aws`): Terraform `hashicorp/aws` provider (0.6) and
  `aws_*` resources (0.5), `serverless.yml` (0.6), `cdk.json` /
  `samconfig.toml` / `buildspec.yml` / `appspec.yml` / Copilot manifests /
  `task-definition*.json` (0.6), `aws-actions/*` workflow steps (0.6), SDKs
  (`@aws-sdk/*`, `aws-sdk`, `aws-cdk-lib`, `aws-sdk-*` / `aws-config` /
  `lambda_runtime` crates, `boto3`, `github.com/aws/aws-sdk-go(-v2)`,
  `software.amazon.awssdk`, `aws-sdk*` gems) (0.5), env names `AWS_*`,
  `CDK_*`. Hints: `region`, `account`.
- **Read policy** (source:
  [AWS managed policies for job functions](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies_job-functions.html),
  [Service Authorization Reference](https://docs.aws.amazon.com/service-authorization/latest/reference/reference_policies_actions-resources-contextkeys.html)).
  Preferred: the scoped policy below. Every action is access level `List` or
  `Read` in the Service Authorization Reference, and it is exactly what
  `infra_graph.py` calls:

  ```json
  {
    "Version": "2012-10-17",
    "Statement": [{
      "Sid": "FactoryInfraRead",
      "Effect": "Allow",
      "Action": [
        "rds:DescribeDBInstances",
        "elasticache:DescribeReplicationGroups",
        "sqs:ListQueues",
        "sns:ListSubscriptions",
        "lambda:ListFunctions",
        "lambda:ListEventSourceMappings",
        "ecs:ListClusters",
        "ecs:ListServices",
        "ecs:DescribeServices",
        "ecs:DescribeTaskDefinition",
        "route53:ListHostedZones",
        "route53:ListResourceRecordSets"
      ],
      "Resource": "*"
    }]
  }
  ```

  Broad alternative: the AWS managed **`ReadOnlyAccess`** job-function policy.
  AWS warns it "will also have access to read data in storage services like
  Amazon S3 buckets and Amazon DynamoDB tables", so offer it only when the
  user declines the scoped policy. `ViewOnlyAccess` does not read resource
  content beyond list metadata and is not enough for task definitions.
  Note: `lambda:ListFunctions` and `ecs:DescribeTaskDefinition` return
  plain env var values; `infra_graph.py` keeps only hostnames/URLs extracted
  from them, and ECS `secrets` (SSM / Secrets Manager refs) are never read.
- **Credential**: a named profile, `{{env.AWS_PROFILE}}` (preferred; SSO or
  assume-role into the read role), or `{{env.AWS_ACCESS_KEY_ID}}` +
  `{{env.AWS_SECRET_ACCESS_KEY}}` (+ `{{env.AWS_SESSION_TOKEN}}`), and
  `{{env.AWS_REGION}}` or `infra_graph.py --region`. Identity check (needs no
  permissions, per
  [GetCallerIdentity](https://docs.aws.amazon.com/STS/latest/APIReference/API_GetCallerIdentity.html)):
  `aws sts get-caller-identity` shows the user which account/role is in use
  before the probe.
- **Probe** (must match `probe_aws` in `infra_graph.py`):

  ```sh
  aws ec2 create-vpc --cidr-block 10.255.0.0/16 --dry-run [--region <r>]
  ```

  Per [CreateVpc](https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_CreateVpc.html)
  `DryRun` "checks whether you have the required permissions for the action,
  without actually making the request": `DryRunOperation` = allowed (the
  script treats it as exit 0 -> `refused`), `UnauthorizedOperation` = denied
  (`passed`) ([EC2 error codes](https://docs.aws.amazon.com/AWSEC2/latest/APIReference/errors-overview.html)).
  Nothing is created either way. Limitation: this proves only that EC2 writes
  are denied. It catches `AdministratorAccess`/`PowerUserAccess`, the common
  over-privileged case; the scoped policy above is what guarantees the rest.
- **MCP**: [`awslabs/mcp` AWS API MCP Server](https://github.com/awslabs/mcp/tree/main/src/aws-api-mcp-server)
  (AWS Labs). Stdio: `command: uvx`, `args: ["awslabs.aws-api-mcp-server@latest"]`,
  `env: {"AWS_REGION": "<region>", "READ_OPERATIONS_ONLY": "true", "AWS_API_MCP_PROFILE_NAME": "<profile>"}`
  (all non-secret). `READ_OPERATIONS_ONLY` allows only operations whose access
  level is not `Write`; the README notes "IAM permissions remain the primary
  security control". Keys, if not a profile, are exported in the shell.
  Otherwise: `aws` CLI.
- **Outbound webhooks**: **Yes, indirectly**: EventBridge rules can target
  [API destinations](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-api-destinations.html)
  ("HTTPS endpoints that you can invoke as the target of an event bus rule")
  and SNS supports HTTPS subscriptions. Both need an inbound endpoint, which
  forgecode-sdk does not have yet, so v1 polls.
- **`infra_graph.py --platform aws`**: parallel read-only calls (the CLI
  paginates; `AWS_RETRY_MODE=adaptive`, `AWS_MAX_ATTEMPTS=10` for throttling):
  RDS instances (endpoint host, port), ElastiCache replication groups
  (primary/reader/configuration endpoints), SQS queue URLs, SNS
  subscriptions, Lambda functions + event source mappings, ECS clusters ->
  services (`describe-services` in batches of 10) -> task definitions
  (images, `environment` values), Route53 zones -> A/AAAA/CNAME/alias
  records. Edges: Lambda/ECS task -> RDS/Redis/SQS from env hosts/URLs, SNS
  topic -> subscriber, Lambda -> event source (`consumes`), ECS service ->
  task definition (`runs`), `external:<fqdn>` -> Route53 record -> target.
  Images map ECS/Lambda to repos. `--snapshot FILE` keeps the raw read for
  offline re-runs (`--input FILE`). Also: `--platform terraform` covers AWS
  resources defined in Terraform state.

### GCP

- **Detection** (`gcp`): Terraform `hashicorp/google(-beta)` provider (0.6)
  and `google_*` resources (0.5), `cloudbuild.yaml` / `app.yaml` /
  `.gcloudignore` (0.6), `google-github-actions/*` workflow steps (0.6),
  SDKs (`@google-cloud/*`, `firebase-admin`, `google-cloud-*` crates and
  Python packages, `gcp_auth`, `cloud.google.com/go`, `com.google.cloud`,
  `google-cloud-*` gems) (0.5), env names `GOOGLE_CLOUD_*`,
  `GOOGLE_APPLICATION_*`, `GCP_*`, `GCLOUD_*`, `CLOUDSDK_*`. Hints:
  `project`, `region`.
- **Read role** (source: [IAM roles overview](https://cloud.google.com/iam/docs/roles-overview)):
  basic **`roles/viewer`** ("permissions for read-only actions that don't
  affect state, such as viewing (but not modifying) existing resources or
  data"; the newer equivalent is `roles/reader`). Tighter, per what factory
  reads: `roles/run.viewer`, `roles/cloudsql.viewer`, `roles/redis.viewer`,
  `roles/pubsub.viewer`, `roles/dns.reader` (each documented as read-only in
  [IAM roles and permissions](https://cloud.google.com/iam/docs/roles-permissions)).
  Grant to a dedicated service account (the **user** runs this):

  ```sh
  gcloud iam service-accounts create factory-read --project <project>
  for r in roles/run.viewer roles/cloudsql.viewer roles/redis.viewer \
           roles/pubsub.viewer roles/dns.reader; do
    gcloud projects add-iam-policy-binding <project> \
      --member=serviceAccount:factory-read@<project>.iam.gserviceaccount.com --role=$r
  done
  ```

- **Credential**: impersonation, not a key file:
  `{{env.CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT}}` =
  `factory-read@<project>.iam.gserviceaccount.com` (or
  `--impersonate-service-account`, per
  [gcloud authorizing](https://cloud.google.com/sdk/docs/authorizing)), and
  `{{env.CLOUDSDK_CORE_PROJECT}}`. A key file via
  `{{env.GOOGLE_APPLICATION_CREDENTIALS}}` works but is discouraged; its
  path is never opened by factory.
- **Probe** (caller-run; `infra_graph.py` has no `--platform gcp`):
  [`projects.testIamPermissions`](https://cloud.google.com/resource-manager/reference/rest/v1/projects/testIamPermissions)
  "returns permissions that a caller has" and needs no permission itself.
  Ask for write permissions; the response must contain **none** of them:

  ```sh
  curl -s -X POST -H "Authorization: Bearer $(gcloud auth print-access-token)" \
    -H 'Content-Type: application/json' \
    -d '{"permissions":["run.services.create","run.services.update","cloudsql.instances.update","pubsub.topics.create","dns.changes.create","resourcemanager.projects.setIamPolicy"]}' \
    https://cloudresourcemanager.googleapis.com/v1/projects/<project>:testIamPermissions
  ```

  `{}` (no `permissions` key) = `passed`. Any returned permission =
  `refused`. The token is used in the pipe only and never printed.
- **MCP**: [`googleapis/gcloud-mcp`](https://github.com/googleapis/gcloud-mcp)
  (Google). Stdio: `command: npx`, `args: ["-y", "@google-cloud/gcloud-mcp"]`.
  Its README: "the permissions of the gcloud MCP are directly tied to the
  permissions of the active gcloud account", with impersonation recommended
  for least privilege. It inherits `CLOUDSDK_*` from the shell. Otherwise:
  `gcloud`.
- **Outbound webhooks**: **Yes, indirectly**: Pub/Sub
  [push subscriptions](https://cloud.google.com/pubsub/docs/push) POST to an
  HTTPS endpoint (Eventarc / Cloud Monitoring can feed them). Needs an
  inbound endpoint; v1 polls.
- **`infra_graph.py`**: no live GCP collector in v1. GCP resources enter the
  graph through `--platform terraform` (`google_sql_database_instance`,
  `google_redis_instance`, `google_cloud_run_v2_service` / `google_cloud_run_service`,
  `google_pubsub_topic` / `_subscription`, `google_dns_record_set`), and GKE
  through `--platform kubernetes` (`gcloud container clusters get-credentials`
  with the read identity first).

### Terraform (local state, remote backends, HCP Terraform / Terraform Cloud)

- **Detection** (`terraform`): `*.tf` (0.7), `backend "<type>"` or a
  `cloud { }` block (0.6), `hashicorp/setup-terraform` /
  `tfc-workflows-github` or `terraform plan|apply` in workflows (0.5),
  `.terraform.lock.hcl` / `terragrunt.hcl` (0.4), env names `TF_VAR_*`,
  `TF_TOKEN_*`, `TFE_*`, `TF_CLOUD_*`. Hints: `backend` (e.g. `s3`, `gcs`,
  `remote`), `organization`. `*.tfstate` and `*.tfvars` are never read by the
  detector.
- **Which credential** depends on where state lives (the `backend` hint):
  - **local** state (`terraform.tfstate` in the checkout, or no backend):
    no credential; `infra_graph.py --backend local` records probe
    `not-needed`.
  - **`s3` / `gcs` / `azurerm`** backend: the cloud credential of that
    platform, which must pass **that platform's probe** above (AWS / GCP)
    first. Read needs only get/list on the state bucket (`s3:ListBucket`,
    `s3:GetObject` on the key, per the
    [S3 backend docs](https://developer.hashicorp.com/terraform/language/backend/s3);
    `roles/storage.objectViewer` for GCS). The DynamoDB/lockfile lock
    permissions are **not** granted: `terraform show` does not lock. The
    docs also list `s3:PutObject` for normal use; a read identity omits it:

    ```json
    {
      "Version": "2012-10-17",
      "Statement": [
        {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::<bucket>"},
        {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::<bucket>/<key>"}
      ]
    }
    ```
  - **HCP Terraform / Terraform Enterprise** (`cloud {}` or `backend "remote"`):
    a **team API token** for a team with the workspace **Read** role
    ([workspace permissions](https://developer.hashicorp.com/terraform/cloud-docs/users-teams-organizations/permissions/workspace):
    Read = read runs, read variables, read outputs, read state; no plan, no
    apply, no state write). [Team API tokens](https://developer.hashicorp.com/terraform/cloud-docs/users-teams-organizations/api-tokens)
    "allow access to the workspaces that the team has access to, without
    being tied to any specific user". Do not use a user or organization token.
- **Credential env**: HCP Terraform: `{{env.TF_TOKEN_app_terraform_io}}`
  (the CLI's host-specific variable: `TF_TOKEN_` + hostname with periods as
  underscores, per the
  [CLI config docs](https://developer.hashicorp.com/terraform/cli/config/config-file#environment-variable-credentials));
  for TFE use `TF_TOKEN_<your_host>`. The MCP server reads
  `{{env.TFE_TOKEN}}` + `TFE_ADDRESS`. Cloud backends: the AWS / GCP
  variables above.
- **Probe**:
  - local: none (`not-needed`).
  - s3/gcs/azurerm: run the backend platform's probe (AWS
    `ec2 create-vpc --dry-run`, GCP `testIamPermissions`); only when it is
    denied does the caller pass `infra_graph.py --platform terraform --backend remote --probe-passed`.
    Without `--probe-passed` a remote backend is `inconclusive` (exit 4) and
    nothing is read. This is exactly `build()` in `infra_graph.py`.
  - HCP Terraform: read the workspace's effective permissions
    ([Workspaces API](https://developer.hashicorp.com/terraform/cloud-docs/api-docs/workspaces),
    `data.attributes.permissions`); every write flag must be `false`:

    ```sh
    curl -s -H "Authorization: Bearer $TF_TOKEN_app_terraform_io" \
      https://app.terraform.io/api/v2/organizations/<org>/workspaces/<ws> |
      jq -e '.data.attributes.permissions
             | [."can-queue-run", ."can-queue-apply", ."can-queue-destroy",
                ."can-update", ."can-update-variable", ."can-lock",
                ."can-create-state-versions", ."can-force-unlock"]
             | all(. == false)'
    ```

    Exit 0 = `passed` (then `--probe-passed`); exit 1 = `refused`.
  - In every case discovery runs **only** `terraform show -json` (read).
    `terraform plan` belongs to the change flow (`references/infra-changes.md`)
    with a separate credential, and factory **never** runs `terraform apply`
    during discovery, setup or routines.
- **Secrets caveat**: `terraform show -json` "will display sensitive values in
  plain text" ([`terraform show`](https://developer.hashicorp.com/terraform/cli/commands/show)).
  `infra_graph.py` therefore copies out only an allowlist (`TF_KEEP`): the
  name and endpoint attributes of known types, ECS container images and
  plain `environment` values reduced to hosts, and it skips any Lambda env
  var marked in `sensitive_values`. The raw JSON is never written to disk.
- **MCP**: [`hashicorp/terraform-mcp-server`](https://github.com/hashicorp/terraform-mcp-server)
  (HashiCorp). Stdio via Docker: `command: docker`,
  `args: ["run","-i","--rm","-e","TFE_TOKEN","-e","TFE_ADDRESS","hashicorp/terraform-mcp-server"]`
  (`-e VAR` with no value inherits from the shell), `env: {"TFE_ADDRESS": "https://app.terraform.io"}`.
  Leave `ENABLE_TF_OPERATIONS` at its default `false` (it gates the tools that
  create runs). The team Read token is the real control. Otherwise:
  `terraform` CLI.
- **Outbound webhooks**: **Yes**, HCP Terraform
  [workspace notifications](https://developer.hashicorp.com/terraform/cloud-docs/workspaces/settings/notifications)
  (generic webhook on run and drift/health events). Local and cloud-bucket
  backends: no.
- **`infra_graph.py --platform terraform`**: `terraform -chdir=<dir> show
  -json` per `--dir` (initialised root modules, `--jobs` in parallel),
  walking `root_module` + `child_modules`, managed resources of the
  `TF_KEEP` types only: AWS RDS instance/cluster, ElastiCache group/cluster,
  SQS, SNS, Lambda, ECS task definition/service, ALB/NLB, Route53 records;
  GCP Cloud SQL, Memorystore Redis, Cloud Run (v1/v2), Pub/Sub, Cloud DNS;
  Cloudflare DNS records. Edges: env hosts/URLs -> endpoints, ECS service ->
  task definition (`runs`), SNS topic -> subscriber, Lambda -> event source
  (`consumes`), `external:<fqdn>` -> DNS record -> target. Resource ids are
  Terraform addresses (`source: infra:terraform:<address>`).

### Docker Compose

- **Detection** (`compose`): `docker-compose*.yml` / `compose*.yaml` (0.75),
  env names in compose files (0.3, attributed to their platform). Compose is
  usually local dev; treat it as runtime only if the user confirms it runs
  somewhere (a VM, `docker context`), and record it as
  `environments[]: [{name: "local", source: "<repo>/<file>"}]` otherwise.
- **Read role / credential / probe**: **none**. Compose is file-only
  discovery: factory reads the committed file, never a Docker daemon or a
  remote `DOCKER_HOST`, so there is no credential and no probe
  (`not-needed`). The only command it may run is the offline renderer
  [`docker compose config --no-interpolate`](https://docs.docker.com/reference/cli/docker/compose/config/)
  ("parse, resolve and render compose file in canonical format";
  `--no-interpolate` keeps `${VAR}` unexpanded, so no secret from the shell
  or a real `.env` enters the output). Never `up`, `pull`, `push`, `exec`.
- **MCP**: none needed (no canonical Compose MCP for reading files). CLI:
  `docker compose config`.
- **Outbound webhooks**: No.
- **`infra_graph.py`**: no `--platform compose`. Compose services, `ports:`
  and `depends_on`-style references are already extracted per repo by
  `repo_graph.py` (`ports`, env hostnames) with `source: <repo path>`,
  which is the right precedence for a file in the repo.

### Cloudflare / DNS

- **Detection** (`cloudflare`): `wrangler.toml|json|jsonc` (0.8), Terraform
  `cloudflare/cloudflare` provider (0.6), `cloudflare/*` actions or
  `wrangler deploy|publish` in workflows (0.6), SDKs (`wrangler`,
  `@cloudflare/*`, `worker` crate, `cloudflare-go`) (0.5), env names
  `CLOUDFLARE_*`, `CF_API_*`, `CF_ACCOUNT_*`. Hint: `worker`. DNS hosted
  elsewhere is covered by the owning platform (Route53 under AWS, Cloud DNS
  under GCP, both also via Terraform).
- **Read role** (source:
  [API token permissions](https://developers.cloudflare.com/fundamentals/api/reference/permissions/),
  [Create API token](https://developers.cloudflare.com/fundamentals/api/get-started/create-token/)):
  a **custom API token** (never the Global API Key) with, scoped to the
  relevant zones only:
  - Zone / **Zone: Read** ("read access to zone management")
  - Zone / **DNS: Read** ("read access to DNS")
  - optionally Account / **Workers Scripts: Read** for Worker routes.

  No `Edit` / `Write` permission of any kind.
- **Credential**: `{{env.CLOUDFLARE_API_TOKEN}}` (the name `wrangler` and
  the Terraform provider read). Check it is live with
  `GET /client/v4/user/tokens/verify` (documented on the create-token page);
  that call does not reveal scopes.
- **Probe** (caller-run; no `--platform cloudflare` in `infra_graph.py`):
  an attempted DNS record create must be denied. The
  [create DNS record](https://developers.cloudflare.com/api/resources/dns/subresources/records/methods/create/)
  endpoint requires **DNS Write**, while
  [list DNS records](https://developers.cloudflare.com/api/resources/dns/subresources/records/methods/list/)
  accepts DNS Read:

  ```sh
  curl -s -o /dev/null -w '%{http_code}\n' -X POST \
    "https://api.cloudflare.com/client/v4/zones/<zone_id>/dns_records" \
    -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" -H 'Content-Type: application/json' \
    -d '{"type":"A","name":"_factory-probe","content":"not-an-ip","ttl":60}'
  ```

  The payload is deliberately **invalid** (an `A` record with a non-IP
  value), so nothing is ever created. `403` = the write is denied (`passed`).
  Any other status (e.g. `400` validation error) means the token got past
  authorization and **can** write: `refused`. The check fails safe: if
  Cloudflare ever validated before authorizing, a read token would also be
  refused, never wrongly accepted.
- **MCP**: Cloudflare's hosted servers
  ([MCP servers for Cloudflare](https://developers.cloudflare.com/agents/model-context-protocol/mcp-servers-for-cloudflare/)):
  `url: https://mcp.cloudflare.com/mcp` (Cloudflare API server; OAuth lets
  the user pick permissions, or a token as
  `bearer_token: {{env.CLOUDFLARE_API_TOKEN}}`), or the narrower
  `https://dns-analytics.mcp.cloudflare.com/mcp`. With OAuth, grant only the
  read scopes above. Otherwise: `curl` against the API, or `wrangler`.
- **Outbound webhooks**: **Yes**:
  [Notifications webhooks](https://developers.cloudflare.com/notifications/get-started/configure-webhooks/)
  (account-level alert destinations). v1 polls.
- **`infra_graph.py`**: no live Cloudflare collector in v1. Cloudflare DNS
  records enter the graph through `--platform terraform`
  (`cloudflare_record`, `cloudflare_dns_record`: `external:<name>` -> record
  -> target).

### Vercel / Fly.io / Render (PaaS)

Three hosts share one entry because factory treats them the same way: read
the project/app list, domains and environments; never deploy, scale or edit
env vars. None of them has a live collector in `infra_graph.py` v1. Their
facts go into the KB `runtime.environments[]` straight from the API/CLI
output, with `source: infra:<platform>:<project|app|service>`.

The same rule applies to all three: **the probe is an invalid write**. Send a
write request with a body the API must reject. `401`/`403` = denied
(`passed`). A validation error (`400`/`422`) means the token got past
authorization and **can** write (`refused`). No valid write is ever sent.

#### Vercel

- **Detection** (`vercel`): `vercel.json` (0.85), Terraform `vercel/vercel`
  (0.6), `vercel deploy|build|--prod` or `amondnet/vercel-action` in
  workflows (0.6), `vercel` / `@vercel/*` deps (0.5), `VERCEL_*` names.
- **Read role**: Vercel tokens have **no read-only scope**. A token acts with
  the permissions of the user who created it, restricted by its scope: Full
  Account, a team, or a single project
  ([Access tokens](https://vercel.com/docs/accounts/access-tokens): "a
  project-scoped token denies any request to another project, to a
  user-level resource, or to a team-level resource"). Read-only comes from
  the **user's role**: create the token as a member with the **Viewer** role
  ([Access roles](https://vercel.com/docs/rbac/access-roles): Pro Viewer "has
  read-only access to core project functionality", Enterprise Viewer "has
  read-only access to the team's resources and projects"), scoped to the
  team or a project. A token from an Owner/Member account is refused by the
  probe.
- **Credential**: `{{env.VERCEL_TOKEN}}` (read by the `vercel` CLI;
  `--token` is never used on a command line). Team id as `VERCEL_ORG_ID`
  (non-secret).
- **Probe**: an invalid env-var create on a project the token can see
  ([create env vars](https://vercel.com/docs/rest-api/projects/create-one-or-more-environment-variables),
  documented `400` = invalid body, `403` = no permission):

  ```sh
  curl -s -o /dev/null -w '%{http_code}\n' -X POST \
    -H "Authorization: Bearer $VERCEL_TOKEN" -H 'Content-Type: application/json' \
    "https://api.vercel.com/v10/projects/<project>/env" -d '{"key":""}'
  ```

  `403` = `passed`; `400` = `refused`.
- **MCP**: [Vercel MCP](https://vercel.com/docs/agent-resources/vercel-mcp),
  `url: https://mcp.vercel.com` (OAuth; beta). It "grants the AI system ...
  the same access as your Vercel user account" and can deploy code, so it is
  only offered to a Viewer account. Otherwise: `vercel` CLI / REST
  (`GET /v10/projects`, `GET /v9/projects/{id}/domains`).
- **Outbound webhooks**: **Yes**:
  [Webhooks](https://vercel.com/docs/webhooks) (Pro/Enterprise; deployment
  and project events).

#### Fly.io

- **Detection** (`fly`): `fly.toml` / `fly.<env>.toml` (0.85),
  `superfly/*` actions or `flyctl deploy` in workflows (0.6), `FLY_*` names.
  Hints: `app`, `region` (`primary_region`).
- **Read role**: Fly offers a real one. `fly tokens create readonly`
  "limits the token access to reading a single org and its resources"
  ([Access tokens](https://fly.io/docs/security/tokens/)):

  ```sh
  fly tokens create readonly --name factory-read --expiry 720h   # the user runs this
  ```

- **Credential**: `{{env.FLY_API_TOKEN}}` (read by `flyctl`).
- **Probe**: an invalid Machine create through the
  [Machines API](https://fly.io/docs/machines/api/machines-resource/)
  (`POST /v1/apps/{app_name}/machines`) with an empty config:

  ```sh
  curl -s -o /dev/null -w '%{http_code}\n' -X POST \
    -H "Authorization: Bearer $FLY_API_TOKEN" -H 'Content-Type: application/json' \
    "https://api.machines.dev/v1/apps/<app>/machines" -d '{}'
  ```

  `401`/`403` = `passed`; `400`/`422` = `refused`.
- **MCP**: `fly mcp server` (built into `flyctl`,
  [flyctl MCP server](https://fly.io/docs/mcp/flyctl-server/)): stdio,
  `command: fly`, `args: ["mcp","server"]`. It inherits `FLY_API_TOKEN` from
  the shell, and the read-only token is the control. Otherwise: `flyctl`
  (`fly apps list`, `fly status -a <app> --json`, `fly certs list`).
- **Outbound webhooks**: **No** general deploy/alert webhooks are
  documented; poll.

#### Render

- **Detection** (`render`): `render.yaml` blueprint (0.85), a deploy hook
  (`api.render.com/deploy`) or `*/render-deploy*` action in workflows (0.5),
  `RENDER_*` names. Hint: `service` names from the blueprint.
- **Read role**: **Render has no read-only or scoped API key.** An API key is
  created in Account Settings ([Render API](https://render.com/docs/api)) and
  carries the creating user's access to their workspaces. Factory says so,
  offers file-only discovery (`render.yaml`), and connects the API only if the
  user accepts a full-access key. In that case the probe below **will** refuse
  it, so live Render discovery is off by default. This is the documented
  limitation, not a workaround.
- **Credential**: `{{env.RENDER_API_KEY}}`.
- **Probe**: an invalid service create:
  `curl -s -o /dev/null -w '%{http_code}\n' -X POST -H "Authorization: Bearer $RENDER_API_KEY" -H 'Content-Type: application/json' https://api.render.com/v1/services -d '{}'`.
  `401`/`403` = `passed`; `400` = `refused` (expected for every Render key
  today).
- **MCP**: [Render MCP server](https://render.com/docs/mcp-server),
  `url: https://mcp.render.com/mcp` (OAuth, or
  `bearer_token: {{env.RENDER_API_KEY}}`). Render warns it "supports
  potentially destructive operations, including modifying a service's
  environment variables and triggering deploys", so factory does not install
  it for discovery.
- **Outbound webhooks**: **Yes**:
  [Render Webhooks](https://render.com/docs/webhooks) (workspace service
  events such as deploy started). Deploy hooks are inbound, not webhooks.

### GitHub Environments

- **Detection** (`github-environments`): `environment:` keys in
  `.github/workflows/*` (0.65), Terraform `github_repository_environment`
  (0.4). Hint: `environment` names (e.g. `staging`, `production`,
  `github-pages`), which seed the KB `runtime.environments[]` and the
  staging-first rule in `references/infra-changes.md`.
- **Read role**: `gh` is already authenticated in phase 0. For a dedicated
  read token, use a **fine-grained PAT** with repository permissions
  **Actions: read** (`GET /repos/{owner}/{repo}/environments` is listed under
  "Actions" read), **Deployments: read** and **Metadata: read**. Leave
  **Administration** at *No access*: environment create/update/delete
  (`PUT`/`DELETE /repos/{owner}/{repo}/environments/{environment_name}`) is
  "Administration" **write**
  ([permissions for fine-grained PATs](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens)).
  A classic token needs the `repo` scope to read private repos and cannot be
  made read-only, so it always fails the probe for admins.
- **Credential**: the existing `gh auth` login, or `{{env.GH_TOKEN}}` (`gh`
  reads `GH_TOKEN`, then `GITHUB_TOKEN`, per
  [`gh help environment`](https://cli.github.com/manual/gh_help_environment)).
- **Probe**: an invalid environment update; the
  [environments API](https://docs.github.com/en/rest/deployments/environments#create-or-update-an-environment)
  returns `422` for invalid input once authorised:

  ```sh
  gh api -X PUT repos/<owner>/<repo>/environments/<env> -F wait_timer=-1 \
    --silent; echo $?
  ```

  `403` / "Resource not accessible" = `passed`. A `422` validation error =
  `refused` (the caller can administer the repo). Nothing is changed in
  either case. **Note**: phase 0's `gh` login is usually a repo admin, so it
  is expected to be refused here. Factory then reads environments anyway,
  because `GET .../environments` is a read the phase-0 login already
  legitimately has, but it records `probe: refused` and never uses that login
  for any infra step. This is the only platform where the discovery read runs
  on a credential that can write, and it is limited to
  `GET /repos/{owner}/{repo}/environments` + `GET .../deployments`.
- **MCP**: the GitHub MCP server's `actions` / `repos` toolsets (see
  `references/trackers.md`). Otherwise: `gh api`. No extra MCP is added.
- **Outbound webhooks**: **Yes**: repo/org webhooks `deployment`,
  `deployment_status`, `deployment_review`, `deployment_protection_rule`
  ([webhook events](https://docs.github.com/en/webhooks/webhook-events-and-payloads)).
- **`infra_graph.py`**: none. Environments go straight into
  `systems/<system>.md` `runtime.environments[]` with
  `source: infra:github-environments:<owner>/<repo>/<env>`.

## How phase 4c uses this

1. **Detect** (phase 3 output): `detect_infra.sh --jobs 8 <every cloned repo>`
   runs offline. Candidates >= 0.6 are offered as defaults, 0.3-0.6 as
   "maybe". `compose` and `github-environments` need no question.
2. **One batched confirmation** (shared with 4 / 4b), per platform: "Connect
   **AWS** read-only (evidence: ...)? Credential expected in
   `{{env.AWS_PROFILE}}`, least-privilege policy: <link to this section>."
   Factory shows the role/policy to create and never asks for a value.
3. **Credential present?** Check only that the variable is set. If it is
   missing, mark the platform `pending-credential` in `state.py` and move on;
   a later resume re-asks.
4. **Probe** (per platform, in parallel, `--jobs`): the write that must be
   denied, exactly as above. `refused` stops that platform, tells the user
   which read role to create instead, and records `probe: refused` in state.
   `inconclusive` is reported with the stderr tail and is retried on resume.
   Never continue past a failed probe, and never "try the read anyway".
5. **Graph**: `infra_graph.py --platform <p> --jobs 8 --repos graph.json
   --org <org> --out infra.json` (kubernetes / aws / terraform), or the
   CLI/API reads listed per platform. Per-platform errors land in
   `errors[]` and never abort the others.
6. **Ingest**: `kb.py ingest infra.json` (single writer, per
   `references/parallelism.md`). Facts get `source: infra:<platform>:<resource>`
   and `verified: <today>`. Precedence is `user` > `infra:*` > manifest; a
   disagreement with an existing fact becomes a `question` issue, never an
   overwrite.
7. **Routines** (phase 6) re-run 4 -> 5 -> 6 on a schedule (probe first,
   every time) and file drift as `infra-change` board issues. They never
   apply anything.

## Secrets policy

Hostnames, ports, resource names, image names and topology are allowed in
the KB (a private repo). **Secrets never are.** `infra_graph.py` never reads
Kubernetes Secrets/ConfigMaps or `valueFrom`, ECS `secrets`, or
`sensitive_values` in Terraform. `kb.py` also refuses any value matching a
token pattern, including cloud keys: AWS access key ids (`AKIA…`/`ASIA…`)
and secret keys, GCP service-account JSON (`"private_key"`), Cloudflare /
Vercel / Fly / Render / HCP Terraform tokens, GitHub tokens (`ghp_`,
`github_pat_`), and URLs with embedded credentials (`scheme://user:pass@`).
A refused value is dropped and reported, never written.
