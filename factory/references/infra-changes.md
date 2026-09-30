# Infra changes: explicit ask, staging first, approval-gated

Factory's infrastructure access is **read-only** (`references/infra.md`). This
document defines the only way factory ever *changes* infrastructure: an
escalated, on-demand mode that exists only when the user explicitly asks for a
concrete change. It is not a phase. Follow it in order, every time. **No step
is skippable**, and no run improvises a variation of it.

Tooling:

| Tool | Role |
|---|---|
| `scripts/infra_plan.py` | plans one change for one environment (never applies), classifies, computes blast radius, writes the plan + inverse plan, refuses unsafe inputs |
| `scripts/state.py set-change` / `change-next` | records every step in `.agents/factory-state.json` (`infra_changes.<id>`); enforces order and that `applied_*` never re-runs |
| `scripts/kb.py` | environments (`runtime.environments[]`), connections, monitoring; `propose` records the decision |

Model tier: planning and classification review are `intelligent`, effort
`medium` (`references/parallelism.md`). Never fan this out, and never delegate
an approval or an apply to a sub-agent.

## Resume first

A change has an id `chg-<yyyymmdd>-<system>-<slug>`. Before doing anything for a
change, run `state.py change-next <id> --json` and continue at `step`:

- Never redo a `done` step. Never re-run an `applied_*` step, whatever its status.
- `applied_*: in_progress` or `blocked` means an apply was started and may have
  partially landed. **Do not apply again.** Compare live state with the plan
  (the `state_matches_plan` verify check). If it matches, mark it `done` and
  continue with verification. If it does not match, report the drift and offer
  the inverse plan as a **new** change. `state.py` refuses to move an apply
  back to `pending` or `skipped`.

## 1. Explicit ask

An infra change starts **only** when the user names a concrete change, for
example "raise the api staging DB to 50 GB, then prod". The ask fixes
`env_scope`:

- `staging`: the ask did not mention prod. The `*_prod` steps are recorded as
  `skipped`. Prod later needs a new explicit ask.
- `staging+prod`: the ask names prod too. Prod still goes through staging.

Setup phases, routines, board runs, fan-out agents and `project_run` issue
runs **never** originate a change. When they find a reason for one (drift,
an alert, an undersized resource), they file a board issue labelled
`infra-change` that describes the change and why. Only a human turning that
issue into an explicit ask starts this flow.

Vague asks ("fix the infra", "make it faster") are not a change. Propose a
concrete one and wait for the user to confirm it in their own words.

## 2. Classify (printed first)

Before anything else, print the classification with its reasons:

| class | meaning |
|---|---|
| `additive` | only creates new resources that nothing yet depends on |
| `mutating` | updates existing resources in place without the destructive traits below |
| `destructive` | any **delete**, **replace** (delete + create), **data-affecting** change (databases, buckets, volumes, queues, keys, stateful sets), **traffic-cutting** change (DNS, load balancers, ingress, security groups, network policy, scaling to 0) or **IAM change that may widen access** (roles, policies, bindings, service accounts, grants) |

**Doubt -> destructive.** An unknown action or resource type is destructive.
`infra_plan.py` classifies every resource from the native plan and takes the
maximum. You may raise its classification but never lower it (`state.py`
refuses a lower value once `classified` is done).

Record: `state.py set-change <id> classified done --platform <p> --system <s>
--classification <c> --env-scope staging|staging+prod`.

## 3. Separate, env-scoped write credential

- Changes use a **write** credential that is separate from the read-only
  discovery credential, and scoped to **one environment**:
  `{{env.<PLATFORM>_WRITE_STAGING}}` and `{{env.<PLATFORM>_WRITE_PROD}}`
  (for example `AWS_WRITE_STAGING`, `K8S_WRITE_PROD`). The user exports it in
  their own shell. Factory never asks for, prints, stores or writes the value
  anywhere: not in chat, state, KB, plans, `mcp.json` or commits.
- `infra_plan.py` refuses to run when:
  - the credential for this environment is missing;
  - the staging and prod credentials are the same value;
  - the credential equals a `*_READ*` discovery credential.
- **Cross-env probe:** the staging credential must be *denied* on prod (and
  the prod credential on staging). For Kubernetes/Helm the probe is
  `kubectl auth can-i create deployments --context <other>`. Other platforms
  declare `cross_env_probe` in the change file: an attempted write against
  `{target}` (the other environment's id) that must fail. An allowed probe
  stops the flow and asks the user to scope the credential.
- Only this environment's credential is passed to the native CLI. Every other
  `*_WRITE_*` variable is stripped from its environment.

## 4. Environment resolution from the KB

"Staging" and "prod" are resolved from the knowledge base, never guessed:
`systems/<system>.md` -> `runtime.environments[]` (`name`, `id`, `platform`,
`url`; written by `kb.py add-env`).

- No such system, or no `staging` entry: **STOP and ask** the user which
  resources are staging. Record the answer with `kb.py add-env <system>
  staging --id ... --source user`, then continue. `infra_plan.py` exits `3`
  here.
- Never plan against an environment whose name or id you inferred from a
  hostname, a branch name or a resource tag.
- A Kubernetes/Helm change may map each environment to a kube context in the
  change file. Otherwise the KB `id` is used as the context.

## 5. Plan, don't act

Write the change file (JSON; format in the `infra_plan.py` docstring) next to
the code it changes, then run:

```bash
python3 <skill>/scripts/infra_plan.py --platform terraform|kubernetes|helm|imperative \
  --env staging --change <change.json> --kb ~/workspaces/knowledge
```

It wraps the native planner and never applies:

| platform | plan | inverse plan | state check after apply |
|---|---|---|---|
| Terraform | `init`, `plan -out=plan.tfplan`, `show -json` | per resource: created -> targeted `plan -destroy`; updated -> restore `before`; deleted/replaced -> recreate from `before` (with a data warning) | `plan -detailed-exitcode` is clean |
| Kubernetes | `kubectl diff -f` | live objects snapshotted (`kubectl get -o yaml`, runtime fields stripped) for restore; created objects deleted only after a diff | `kubectl diff` is empty |
| Helm | `helm diff upgrade --detailed-exitcode` | `helm rollback` to the pre-apply revision (or `uninstall` for a new release) | `helm diff` is empty |
| imperative CLI | the declared dry run (`--dry-run` / `--preview`) | a declared inverse command (required) | declared verify command, else manual |

It writes `.agents/infra-plans/<id>/<env>/` (`plan.json`, the native plan,
`inverse.json`, `summary.json`, snapshots). The files are mode 0600, sensitive
values are masked and binaries are git-ignored. It prints
`{classification, reasons, resources[], blast_radius, plan_hash, plan_path,
inverse_plan_path, verify_checks[], confirm_by_typing?, state_cmd}`.

**Show the user the whole thing**: classification and reasons first, then every
resource and action, then the blast radius (the target system, its KB
dependents and dependencies, the edges touched, the environment id and URL, the
monitors). Then the inverse plan and the verify checks. Never summarise away a
resource.

Record: `state.py set-change <id> planned_staging done --plan-hash <plan_hash>
--inverse-plan-path <path>` (the script prints this as `state_cmd`).

`plan_hash` is a hash of the change file plus every source file it references,
so it is the same for staging and prod. Editing the change after staging makes
prod refuse.

## 6. Approval #1 (staging), apply, verify

1. **Approval #1.** Ask one yes/no question naming the change, the
   classification, the environment id and the resource count. Only an explicit
   "yes" in the user's own words counts. Silence, "looks fine" about something
   else, or a batch approval of other things do not. For a destructive change,
   the user also types the name of each destructive resource
   (`confirm_by_typing`). Record `approved_staging done`.
2. **Apply** exactly the approved plan (Terraform: `terraform apply
   plan.tfplan`, where a saved plan never needs an auto-approve flag;
   Kubernetes: `kubectl apply -f` right after the diff; Helm: `helm upgrade`
   with the planned values; imperative: the dry-run command without its dry-run
   flag). Before running the command, record `applied_staging in_progress`.
   Record `done` immediately after it finishes, or `blocked --blocker <what
   happened>` if it failed.
3. **Verify**: run every item in `verify_checks`:
   - `state_matches_plan`: live state equals the plan (no remaining diff);
   - `health`: the environment URL answers 2xx;
   - `kb_connection:*`: each KB edge of the system still resolves from its
     consumer;
   - `monitoring_soak`: if monitoring is connected, watch the listed monitors
     for the soak window (15 min staging, 30 min prod) and compare with the
     pre-apply baseline.
4. **A failed check means roll back.** Apply the inverse plan; it goes through
   its own diff and approval, because it is itself a change. Report what failed
   and what was restored, mark `verified_staging blocked`, and **stop**. Never
   continue to prod after a failed staging verification.
5. All checks pass: record `verified_staging done`.

## 7. Approval #2 (prod)

Only if `env_scope` is `staging+prod` and `verified_staging` is `done`.
Otherwise record the `*_prod` steps as `skipped` (staging-only asks) or stop.

1. **Re-plan against prod.** Run `infra_plan.py --env prod` with the same change
   file. Never replay the staging plan. The script refuses unless the state
   shows `verified_staging: done` for the same `plan_hash` and an ask that
   included prod. It adds `plan_diff`: resources only in prod, resources only
   in staging, and actions that differ. Show it. Explain any difference before
   asking, because a prod plan that differs from what was verified on staging
   is new risk.
2. **Approval #2**, same rules as #1. A **destructive** prod change requires
   the user to type back each destructive resource name exactly
   (`confirm_by_typing`). A mismatch counts as a "no".
3. Apply, then verify with the prod `verify_checks`. A failed check means the
   prod inverse plan (with its own approval), a report, and a stop. Record
   `planned_prod`, `approved_prod`, `applied_prod`, `verified_prod` as you go,
   exactly as for staging.

## 8. Record

- `kb.py` records the decision in `decisions.md` (newest first): date, the
  change, the classification, both approvals (who approved and when), the plan
  hash, the plan and inverse-plan paths, the verification results and the
  board issue link. It goes through `kb.py propose` (a PR, never a push to
  main).
- Update what changed in the KB: `kb.py add-env` for new or changed
  environment ids or URLs, `kb.py add-connection` for new edges
  (`--source infra:<platform>:<resource>`), and facts under `runtime`.
- Link or close the `infra-change` board issue with the PR and the outcome.
- `state.py set-change <id> recorded done`.

## Hard rules

- No `-auto-approve` / `--auto-approve`, no `--force`, no `--yes`/`-y`, no
  `--grace-period=0`. `infra_plan.py` refuses change files that contain them.
- No `kubectl delete` without an immediately preceding `kubectl diff` of the
  same objects. Imperative steps may not call `kubectl delete` at all.
- No path that bypasses staging: no prod-first plan, no "same as staging, skip
  it", no hotfix exception. If the user insists on prod without staging,
  explain the policy and stop.
- One change id, one plan hash. A different change, or a rollback, gets a new
  id.
- Plans live under `.agents/infra-plans/<id>/` only. They are never committed
  and never pasted into the KB (link to them instead).
- Never secrets: no credential values in the chat, state, plans, the KB, board
  issues or commits. Only env var *names*.
- Approvals are never batched with other approvals and never delegated to a
  routine or a sub-agent.
- Routines never touch infra. They may only file `infra-change` issues.
