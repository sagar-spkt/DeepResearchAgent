# Lifecycle increment: reviewed artifacts and provider verification

This increment implements executable lifecycle building blocks, not a successful live deployment. No AWS calls, downloads or infrastructure mutations were made during development. The existing paid deployment admission gate must remain closed until independent safety review and live backup/restore/expiry rehearsal pass.

## Components

- `scripts/platform_lifecycle.py`: explicit pure planning, read-only collection/verification, backup, saved-plan shutdown, fresh teardown inventory, conditional session release and restore preparation. Unsupported deploy, purge and legacy backup-shutdown remain exit2/no execution.
- `scripts/lifecycle/core.py`: canonical artifact hashes, expiry, config binding, runtime resource allowlist, immutable binary Terraform-plan checks and operation ordering.
- `scripts/lifecycle/aws_adapter.py`: lazily injected boto3 clients, account/tag/Cognito/S3 version checks, exact EKS context endpoint/CA matching; quiesce writers; ClickHouse BACKUP and `system.backups` verification; Valkey RDB/checksum; RDS snapshot creation and availability wait; S3 version inventories/provider verification; ingress removal/ALB wait; explicit RDS-protection change and exact saved-plan apply; remaining inventory.
- `scripts/lifecycle/inventory.py`: read-only regional project inventory and transactional completion/archive/admission release. Denied APIs, malformed responses and unknown resource provenance block release. No infrastructure-delete API is called by this module.
- `infra/expiry`: separate retained state, one session-specific Scheduler -> Step Functions -> CodeBuild chain, immutable source version/image and approved intent hash, failure alarm/DLQ. The executor network must be separate from the runtime VPC it destroys. IAM execution policy is an explicit reviewed input, not a wildcard default. Both expiry alarms publish to the KMS-encrypted environment SNS topic from foundation; an operator must add and confirm an alert subscription after foundation apply before treating notifications as operational.

## Commands (review only until safety acceptance)

A config JSON contains account_id, region, environment, session_id, cluster_name, database_identifier, document_bucket, backup_bucket, trace_bucket, user_pool_id, alert_topic_arn and alert_kms_key_arn. Alert ARNs must come from the applied environment foundation output and match the exact environment/account/region. Names must match deepresearch-ENVIRONMENT. Retained buckets must be exactly deepresearch-ENVIRONMENT-{documents,backups,traces}-ACCOUNT_ID, and every S3 API call binds ExpectedBucketOwner. No secret values belong in config. Configurations and saved lifecycle plans without the alert ARNs are rejected; regenerate local plans and config from the reviewed foundation output after apply.

```sh
python3 scripts/platform_lifecycle.py plan-backup --config private-config.json --output private-backup-plan.json
# Review all planned actions and the printed canonical SHA256.
python3 scripts/platform_lifecycle.py backup --plan private-backup-plan.json --approved-plan-hash REVIEWED_SHA256 --execute --output private-backup-receipt.json
python3 scripts/platform_lifecycle.py verify-receipt --receipt private-backup-receipt.json --output private-verification.json
python3 scripts/platform_lifecycle.py plan-shutdown --config private-config.json --receipt private-backup-receipt.json --runtime-dir infra/runtime --var-file private-runtime.tfvars.json --saved-plan private-destroy.tfplan --output private-shutdown-plan.json
# Review JSON and exact Terraform binary plan before approving this separate mutation.
python3 scripts/platform_lifecycle.py shutdown --plan private-shutdown-plan.json --approved-plan-hash REVIEWED_SHA256 --execute --output private-shutdown-receipt.json
python3 scripts/platform_lifecycle.py restore --receipt private-backup-receipt.json --output private-restore-inputs.json
```

Commands without explicit execute/hash cannot mutate AWS. Approval mismatches fail before constructing clients. Output files are private/exclusive; mutating commands reserve a pending journal before external work. Partial failure keeps the journal and reports that mutations may have begun; do not blindly retry. Inspect snapshot IDs/prefixes in the approved plan and reconcile first. Restore preparation verifies retained provider data, permits old receipts, and produces snapshot inputs. It reports snapshot_provider_verified=true but database_restore_verified=false; snapshot existence is not restoration evidence.

## First-provision failure cleanup

`plan-bootstrap-cleanup` and `bootstrap-cleanup` provide a separate source path for a failed initial runtime apply, before application installation begins. Prepare a fresh runtime destroy plan from the exact staging state, review its binary and canonical intent hash, then execute only that reviewed plan:

```sh
python3 scripts/platform_lifecycle.py plan-bootstrap-cleanup --config private-config.json --runtime-dir infra/runtime --var-file private-runtime.tfvars.json --saved-plan private-bootstrap-cleanup.tfplan --output private-bootstrap-cleanup-plan.json
# Review the intent hash, Terraform JSON scope, session tags, and exact binary plan.
python3 scripts/platform_lifecycle.py bootstrap-cleanup --plan private-bootstrap-cleanup-plan.json --approved-plan-hash REVIEWED_SHA256 --execute --output private-bootstrap-cleanup-receipt.json
```

The path is limited to the exact `infra/runtime` destroy allowlist, active account/environment/session admission, and renewable lease. It rechecks the saved binary/hash and refuses unless EKS is active or in terminal `FAILED` state and correctly tagged (or absent); when active it assumes the exact session cluster-admin role and requires the `research` namespace to be absent. Any database must be available and session-tagged. The runtime destroy plan retains the RDS final snapshot when a database exists. Post-cleanup provider inventory must be complete and confirm runtime resources are gone; the management NAT/VPC are tracked separately and intentionally remain until their external finalizer runs. Foundation and management networking are outside the runtime destroy plan. A still-creating/deleting EKS cluster, a present application namespace, unrecognized resources, unavailable database, failed final snapshot, or incomplete post-cleanup inventory blocks success. If Terraform apply fails after RDS protection is disabled, the command attempts to restore deletion protection before returning the failure; if restoration also fails, preserve the journal and reconcile immediately. This source path has not been independently reviewed or rehearsed in AWS. Never use it after Helm/application installation starts; normal shutdown requires verified backups.

## Automatic expiry preparation

Reserve admission before provisioning runtime resources. After runtime resources exist, and before opening application writers, `plan-expiry --config ... --workspace REPO --var-file artifacts/runtime.tfvars.json --saved-plan artifacts/expiry.tfplan --output artifacts/expiry-intent.json` produces an intent covering the nested exact backup plan and immutable runtime destroy binary. The CLI reads the admitted start from DynamoDB; preparation cannot reset the session clock. The four-hour trigger and six-hour authorization deadline are anchored to that original admission. First-provision failure recovery still needs a live-tested cleanup path: the exact destroy artifact cannot be prepared before resources exist, so this source does not guarantee automatic cleanup throughout initial provisioning. Review its full hash and scope. Scheduler must fire at its not_before timestamp (four hours), within its six-hour authorization window. Source archive includes the exact reviewed source, dependency/provider locks, runtime configuration/backend reference and saved plan; use private encrypted versioned S3. Never publish plans: they can contain sensitive state.

The CodeBuild executor receives the reviewed intent hash and immutable archive version. Its explicit `expiry --execute --approved-plan-hash ...` authorizes only that preapproved backup plus destroy artifact; a dynamically generated receipt does not expand deletion scope. Backup receipts are uploaded versioned to the retained backup bucket before destruction; final receipt is also published. Keep one session globally active and forbid parallel state changes. The separate management network must reach private EKS/RDS/AWS APIs and must not appear in the runtime destroy plan. Add the executor role as a narrowly reviewed EKS access identity; provision its network-interface permissions and exact backup/runtime AWS permissions. Runner image must contain pinned Terraform, kubectl, AWS CLI, boto3 and provider binaries/cache.

## Explicit limitations / required acceptance

- **Lifecycle source has independent targeted reviews, but bootstrap cleanup is newly added and unreviewed; there is no live recovery rehearsal or release signoff.** Keep runtime provisioning and paid deploy gate closed until independent review and recovery rehearsal pass.
- ClickHouse StatefulSet needs a mounted `/etc/clickhouse-client/config.xml` providing authentication and tested S3 environment-credential support. Its service account needs a separate reviewed backup-prefix policy; current platform chart may not provide this. The adapter refuses backup if query/provider verification fails. Never put AWS keys into SQL/argv.
- Writer scale-down preserves queued/running application rows for lease recovery; it is an offline quiesce, not graceful completion. Receipt records all writer deployment UIDs/generations. Shutdown requires unchanged identities/generations, so resuming writes and scaling to zero again forces a fresh backup. RDS snapshot provider creation time must follow verified quiescence and precede receipt issuance. It also saves Valkey so pending Langfuse work is retained. Live consistency/replay acceptance remains required.
- ClickHouse backup metadata/object existence and ETags/lengths are provider-verified; Valkey bytes are SHA256-verified. Full ClickHouse restore is the required integrity acceptance test; object metadata is not proof of semantic restoration.
- Snapshots/S3 versions/Cognito are retained. CSI EBS volumes are deliberately not guessed/deleted. The new collector enumerates project EKS/RDS (including other-environment control planes/databases), EC2 instances, VPCs/subnets/ENIs/NAT/EIPs, autoscaling groups, load balancers, EBS, fixed runtime IAM roles and cluster logs. Access denial, unknown CSI/unassociated EIP provenance and identity mismatches prevent completion. This regional project inventory is not an account-wide zero-charge audit; external management/state-backend resources and ongoing retained costs require separate cost evidence.
- Terraform destroy allowlist restricts exact current root runtime address/type pairs, source registry.terraform.io/hashicorp/aws, exact region reference var.region, exact allowed_account_ids reference var.account_id, per-resource identities and the exact reviewed binary. Every taggable runtime resource must carry Project, Environment and Session in tags_all. Untagged resources have explicit role/cluster/network reference checks. Configurations without the new account/session contract are refused; do not weaken inspection for compatibility. IAM/attachment mutations and AWS permissions still need independent review against actual provider plans. No foundation/purge path is enabled.
- Expiry management resources remain in separate state. Their network/endpoint/storage costs must enter the budget. Failure alarm/DLQ configuration and confirmed SNS delivery must be tested; a topic without a confirmed subscriber does not notify an operator. The retained alert KMS key must remain enabled and available during shutdown and restore. Missed expiry, unavailable executor or failed backups can leave billed runtime resources.
- Manual and automatic backups publish a versioned receipt to the retained backup bucket, then re-fetch/check its exact contents before returning success. Shutdown verifies that retained receipt and publishes its own result. Local receipts contain the immutable S3 version pointer.

## Offline verification

`python3 -m unittest discover -s tests -p test_lifecycle.py -v` uses fake clients/runners only. No network or real cloud credentials required.

## Exclusive lifecycle lock and destructive boundaries

An exclusive environment/session lifecycle lock must be held from writer quiescence through final Terraform apply and receipt publication. Every actor capable of changing deployments or runtime infrastructure must honor it, including deployment pipelines, expiry workers and manual operations. GitHub concurrency alone is insufficient. The CLI now acquires a renewable DynamoDB lease from the shared retained control table and checks it before mutating adapter calls and Terraform execution. All other writers must use the same lease; IAM enforcement and live concurrency/recovery verification remain deployment prerequisites. See ADR-0002.

Immediately after ingress/ALB wait and again after RDS-unprotection wait, shutdown rechecks approval lifetime, exact saved artifact hashes, provider backup receipt, deployment UIDs/generations and current quiescence before the next destructive action. These checks reduce waiting-window risk but are not atomic with external writers; they do not replace the exclusive lock. Runtime configuration containing any provisioner metadata (including destroy-time local-exec) is rejected.

## Global session admission

Deploy the shared `infra/control` source in a separate retained state only after provider review. It adds the on-demand `deepresearch-control` table in the explicit account/region. The lifecycle runner needs table-scoped DescribeTable, ListTagsOfResource, GetItem, UpdateItem and DeleteItem permissions; admission/release also require TransactWriteItems with the item actions used by their transactions. Review the role against the exact current DynamoDB API/IAM contract. The collector additionally needs read-only service operations from scripts/lifecycle/inventory.py. Alert checks require topic-scoped `sns:GetTopicAttributes`/`sns:ListTagsForResource` and key-scoped `kms:DescribeKey`/`kms:GetKeyRotationStatus`/`kms:ListResourceTags`; they do not enumerate account-wide SNS topics. No resource-delete permissions are added for collection or release. Do not grant these to application pods.

`plan-admission --config ... --output ...` prepares a two-minute reviewed admission plan. `admit --plan ... --approved-plan-hash ... --execute --output ...` atomically reserves six hours across UTC month boundaries and admits one environment/session globally. Three full reservations fit under the twenty-hour ceiling; no unused time is refunded. Backup/shutdown/expiry require this matching admission and acquire a renewable common lease.

An expired active session is deliberately not freed. Use the provider-verified release flow below; an old timer or supplied inventory JSON cannot clear admission. Completed environment/session IDs cannot be reused: admission transactionally checks the immutable completion key before reserving more time. No direct DynamoDB deletion is a supported substitute for release. The control table and expiry management costs must be included in estimates.

Derived shutdown approval is capped by the parent expiry-intent deadline and the parent is rechecked after backup. Commands time out after one hour; a timeout can leave provider work in progress, so reconcile the pending journal and retained receipts before recovery.

## Teardown inventory and admission release

These commands are implemented source with offline tests; live AWS acceptance remains outstanding.

```sh
python3 scripts/platform_lifecycle.py inventory --config private-config.json --output private-inventory.json
python3 scripts/platform_lifecycle.py plan-release --config private-config.json --output private-release-plan.json
# Review the plan and canonical SHA256 before this control-store mutation.
python3 scripts/platform_lifecycle.py release --plan private-release-plan.json --approved-plan-hash REVIEWED_SHA256 --execute --output private-completion.json
```

`inventory` is read-only and returns explicit errors and runtime_remaining. Inspect inventory_complete, runtime_gone and retained_accounted; writing an inventory artifact is not proof of successful teardown. `release` acquires the shared lease and collects again from providers. It never accepts the earlier JSON as proof. Both reviewed backup and final RDS snapshots must be available/encrypted, and retained foundation identities/protections are checked.

Available, unattached EBS data volumes require explicit Project, Environment, Session, Retention=data and Component (clickhouse/valkey/observability) tags. Another session's retained volume also requires a matching provider-read completion record listing that volume ID. Older untagged volumes block release; this command never retags or deletes them. New observability volumes receive these tags through the chart's component StorageClasses. Existing volumes require explicit reconciliation.

After fresh collection (at most five minutes old), a DynamoDB transaction checks current lease ownership/expiry, writes immutable completion#ENVIRONMENT#SESSION with an inventory digest and retained-resource list, and deletes only the matching active admission. Monthly reservations are never refunded. An ambiguous transaction error requires reconciliation of the completion key and active-session row; preserve the private journal and do not blindly retry. The final local artifact includes the collected evidence. Retained storage, snapshots, identity, secrets and expiry management resources may continue to incur costs.

Optional expiry resources are recorded when present. Management networking/state outside this project scope is not automatically classified; project-tagged runtime resources with an unknown session fail closed. All writers still must honor the cooperative lease; provider eventual consistency, real concurrent DynamoDB conditions, missing-resource behavior, IAM and full teardown/restore/re-admission need live rehearsal.
