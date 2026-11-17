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

Executing this path requires a separately reviewed role with exact RDS restore/describe/tag permissions and EC2 subnet/route-table read permissions, security-group describe/create/tag/egress-revoke permissions, plus lifecycle control-table permissions for the admission lease. This source change adds no IAM policy. The clone and its rule-free security group are not Terraform-managed by this operation; the clone continues to incur RDS charges until a separate reviewed cleanup path removes it, and current inventory should block closeout if either resource remains unaccounted.

The restore receipt preserves the original source DB and snapshot identifiers and records `database_restore_verified=true` only for the provider-level clone check. `restore_rehearsed`, `application_restore_rehearsed` and `full_recovery_accepted` remain false. A real recovery rehearsal must still restore ClickHouse data, restore Valkey and verify pending-work behavior, recreate application configuration, then verify identity, ownership, documents, conversation history, retrieval and observability semantics. Keep ingress and application writers closed until those checks are separately completed and accepted.

This command has offline fake-client tests only. It has not been run against AWS; available subnet groups, effective permissions, regional instance capacity, restored query contents and full application recovery remain unverified.
