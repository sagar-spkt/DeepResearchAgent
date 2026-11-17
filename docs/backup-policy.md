# Backup AWS permission fragment

`scripts/lifecycle/policies.py` exposes the pure standard-library function `backup_policy(config)`. It validates the existing lifecycle configuration and returns an IAM identity policy document. It makes no AWS calls and attaches nothing to any role. There is no operational workflow integration yet.

The fragment covers the AWS calls made by the current backup adapter and its cooperative lease: metadata/version inventory and versioned reads for the three exact retained buckets; AES256-encrypted writes only to the selected session's `valkey/dump.rdb` and `receipts/backup.json` objects; exact EKS/Cognito/alert metadata reads; the exact source RDS database and session snapshot; control table metadata, admission/lease reads and lease-only writes. It grants no deployment, restore, database modification, resource destruction, object deletion, admission reservation, completion or IAM-management permissions. A snapshot is created only at the session's `deepresearch-<environment>-<session>-backup` ARN with the expected request tags.

The Cognito pool must match the configured Region; session identifiers must fit the existing 16-character expiry limit. Other lifecycle configuration rules remain enforced by `validate_config`. A new session needs a newly generated and reviewed fragment before attachment. Existing backup snapshots with the same name must not be overwritten; RDS creation will fail if the identifier already exists.

## Authorization boundaries

- AWS currently documents resource scoping for `DescribeDBSnapshots`; the fragment permits only the exact DB and session snapshot ARNs. It does not use a wildcard account-wide RDS describe grant. `CreateDBSnapshot` authorizes both the DB and snapshot resources, while tagging is limited to the new snapshot.
- Lease IAM uses `dynamodb:LeadingKeys` with an explicit non-null condition. Reads cover `active-session` and `lifecycle-lock`; `UpdateItem` covers only `lifecycle-lock`. Lease release removes lease attributes and preserves unrelated marker items/attributes. There is no `dynamodb:TransactWriteItems` or `dynamodb:DeleteItem` action in the fragment: transaction operations use underlying item permissions, and backup does not reserve or release admissions. IAM does not enforce lease ownership attributes: the existing conditional expressions and lease checks remain necessary, and a compromised principal holding this policy could disrupt the shared lease. Deployment markers use separate `app-writers#staging#SESSION` items; this backup policy cannot create or delete them.
- S3 reads include all retained versions because `verify_receipt` checks the complete inventory, including earlier sessions. Writes are limited to the two executor-owned objects in the current session; ClickHouse backup-prefix writes are deliberately excluded. The generated policy adds Allow statements; it does not negate broader permissions from other policies.
- `sts:GetCallerIdentity` needs no identity-policy allow. No role assumption or cluster access policy is granted here.

Kubernetes access entries/RBAC, the client configuration mount used by `clickhouse-client`, ClickHouse S3 backup credentials, and private networking still require separate implementation and review. This fragment is for the executor's AWS calls; it does not grant S3 access to the ClickHouse pod. It does not include Terraform state access, ECR publication, deployment, destruction, or expiry executor bootstrap permissions. Customer-managed RDS/S3 encryption would require a separate KMS review; the current data resources use service-managed encryption.

Offline tests check exact resources, prefixes, confirmation-independent permission boundaries and rejection of malformed identifiers. The test helper supports only the generated operators; it is not an IAM simulator. AWS Access Analyzer/policy validation, effective permissions including boundaries/SCPs/resource policies, and a live synthetic backup remain unverified. This source does not establish deployment or recovery readiness.

## Official references checked

- [RDS service authorization](https://docs.aws.amazon.com/service-authorization/latest/reference/list_rds.html): snapshot/database resource types and create/describe/tag actions.
- [DynamoDB service authorization](https://docs.aws.amazon.com/service-authorization/latest/reference/list_dynamodb.html) and [transaction IAM](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis-iam.html): item actions and leading-key conditions.
- [S3 service authorization](https://docs.aws.amazon.com/service-authorization/latest/reference/list_s3.html): bucket/version reads and encrypted object writes.
- [GetCallerIdentity](https://docs.aws.amazon.com/STS/latest/APIReference/API_GetCallerIdentity.html): permission behavior for identity verification.
