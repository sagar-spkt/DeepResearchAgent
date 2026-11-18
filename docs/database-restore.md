# Isolated RDS restore

The lifecycle restore path can create an isolated RDS instance from the exact encrypted snapshot in a reviewed backup receipt. It does not alter the original receipt, source database, Terraform runtime state, application configuration, ClickHouse, or Valkey. A successful provider check confirms only that RDS completed the clone with the requested encryption, private networking, and session tags. It is not full recovery acceptance.

Create and review a local plan from the private backup receipt:

```sh
python3 scripts/platform_lifecycle.py plan-database-restore \
  --receipt private-backup-receipt.json \
  --output private-database-restore-plan.json
```

Review the source snapshot ARN and source database identifier, the distinct target identifier `deepresearch-ENVIRONMENT-restore-SESSION`, the fixed `db.t4g.small` restore size, the private DB subnet group, and the session tags. The plan expires after one hour and binds the complete input receipt hash. The target is never replaced: if that identifier already exists, restore stops before requesting creation.

Execute only the exact reviewed plan and receipt, while holding the session admission and lifecycle lease:

```sh
python3 scripts/platform_lifecycle.py database-restore \
  --plan private-database-restore-plan.json \
  --receipt private-backup-receipt.json \
  --approved-plan-hash REVIEWED_PLAN_SHA256 \
  --execute \
  --output private-database-restore-receipt.json
```

Before creation, the executor rechecks the retained backup receipt and source snapshot with RDS, verifies the exact snapshot ARN, source DB, encryption and Project/Environment/Session tags, and checks the runtime DB subnet group. The group must be complete and session-tagged; every member subnet must have public-address assignment disabled and a route table without a direct internet-gateway route. The executor creates a separate session-tagged security group with no ingress or egress rules, removes its default egress rule, and refuses any pre-existing group that is not exactly scoped and rule-free. RDS creation explicitly uses that group and subnet group, sets `PubliclyAccessible=false` and `MultiAZ=false`, and applies Project/Environment/Session/Component tags plus the source snapshot ARN. The executor waits for availability and checks the resulting provider identifier, ARN, encryption, private flag, subnet group, exact active security-group membership, instance class and tags before issuing a database-restore receipt. Failed waiters or mismatched provider state do not issue success; a pending journal means reconcile the target before retrying.

Executing this path requires a separately reviewed role with exact RDS restore/describe/tag permissions and EC2 subnet/route-table read permissions, security-group describe/create/tag/egress-revoke permissions, plus lifecycle control-table permissions for the admission lease. The verifier also uses the existing session-tag-scoped ingress authorize/revoke permissions and reads EKS node-group, Auto Scaling group and instance metadata. This source change adds no IAM policy. The clone and its security group are not Terraform-managed by this operation; the clone continues to incur RDS charges until a separate reviewed cleanup path removes it, and current inventory should block closeout if either resource remains unaccounted.

The restore receipt preserves the original source DB and snapshot identifiers, and binds the verified clone endpoint and port. `database_restore_verified=true` means only that RDS completed the private, encrypted clone check. `restore_rehearsed`, `application_restore_rehearsed` and `full_recovery_accepted` remain false.

## Compare synthetic application data on the clone

Create a dedicated, approved ordinary Cognito test user with the reserved `recovery+SESSION@example.test` address. Through the private staging application, give that user one conversation, a completed research result, at least one ready attached text document, a saved memory and a query whose expected retrieval includes that document. Do not use a real user's account or private documents. Record the synthetic user's exact email, conversation UUID and distinctive query for the reviewed verifier operation. The short lived verifier Job captures a hash-only fixture from the paused source database after the backup receipt is written, then compares it directly with the clone in the same process. It never copies messages, report text, uploaded content, embedding vectors, database passwords or source rows into the output artifact.

After the clone is available, run the reviewed plan's verifier operation. It checks out a lifecycle lease, verifies the exact active `deepresearch-staging` CPU node group and its live instance security groups in the restore VPC, and confirms every verifier node can already read the paused source database. It then temporarily authorizes only those group references on the isolated restore SG at TCP 5432. It creates one short lived Job using the immutable backend image, the existing application secret and `research-application` service account. The Job receives the backup and restore receipts plus synthetic fixture selectors through a temporary Kubernetes Secret; the password stays in the existing application secret. The verifier overrides only the database endpoint from the provider-verified restore receipt and uses PostgreSQL read-only transaction mode.

```sh
python3 scripts/platform_lifecycle.py verify-database-restore \
  --execute \
  --plan /private/path/database-restore-plan.json \
  --approved-plan-hash EXACT_REVIEWED_PLAN_SHA256 \
  --backup-receipt /private/path/backup-receipt.json \
  --receipt /private/path/database-restore-receipt.json \
  --user-email "recovery+${STAGING_SESSION_ID}@example.test" \
  --conversation-id SYNTHETIC_CONVERSATION_UUID \
  --query "distinctive synthetic phrase" \
  --output /private/path/application-recovery-verification.json
```

The wrapper removes the Job, input Secret and ingress rule IDs on success, ordinary failures and handled interrupt/termination signals, then verifies the restore SG has no ingress. A failure to clean any resource is reported as a failed operation and requires operator reconciliation before retrying. Forced process or host termination cannot run cleanup; after any interrupted runner, inspect the verifier Job, input Secret and restore SG and remove leftovers before retrying. The verifier refuses unless the plan, backup receipt, restore receipt, clone endpoint, target ARN, staging session and exact source snapshot bind together. It compares scoped user, conversation, messages, runs/events, documents, attachments, chunks, saved memory and schema-version hashes; checks retained S3 document bytes; and runs the same attached-document retrieval query. Its receipt records `application_data_verified=true` and `retrieval_verified=true` when these checks match. It deliberately leaves `application_restore_rehearsed`, `restore_rehearsed` and `full_recovery_accepted` false because identity, ClickHouse, Valkey, observability and pending-work restoration are not covered.

This application verifier does not restore or verify ClickHouse, Valkey, Langfuse trace history, or pending-work behavior. Those operations still require a separate reviewed and tested restoration path, and the normal staging recovery gate must remain closed until they pass. Keep ingress and application writers closed during this rehearsal.

The restore and in-cluster fixture verifier have offline fake-client/database tests only. They have not been run against AWS; available subnet groups, effective permissions, regional instance capacity, the actual restored query results, ClickHouse/Valkey restoration and full application recovery remain unverified.
