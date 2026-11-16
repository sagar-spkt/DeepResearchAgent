# ClickHouse backup integration

The observability chart assigns only ClickHouse the `research-clickhouse-backup` service account. Its runtime Pod Identity role can read/write the current environment/session's `sessions/<environment>/<session>/clickhouse/` backup prefix. Langfuse web/worker retain their separate traces identity; Valkey receives neither identity nor client configuration. No policy grants object deletion or access to another session. Restoring a previous session therefore requires a separately reviewed temporary read policy for that receipt's exact prefix; do not broaden this writer policy.

## Required private configuration

Before installing the chart, materialize the existing Secret named by `clickhouseClientSecretName` (default `research-clickhouse-client`) in namespace `research`. It must contain a `config.xml` key holding valid ClickHouse client XML with the intended username, password, loopback host and native port. The chart mounts the Secret directory read-only at `/etc/clickhouse-client`; `AWSAdapter.ch()` already uses `/etc/clickhouse-client/config.xml`. No password belongs in Helm values, command arguments, SQL strings, source control or rendered manifests. Missing Secret/key leaves the pod unready instead of invoking an unauthenticated client. Kubernetes Secret updates propagate to the mounted directory; recreate the pod after rotating related server credentials.

The ClickHouse image continues to consume its server credentials from the existing observability Secret. Ensure its credentials and the client-config Secret agree, and provision a database account with the required `BACKUP`, read, and separately controlled `RESTORE` privileges. The chart does not create the Secret or invent credentials. The mounted file is mode 0444 inside the ClickHouse pod so the pinned container's non-root client can read it; restrict Kubernetes Secret reads and pod exec permissions to the controlled operations identity.

The ConfigMap enables the S3 environment credential chain. `AWS_EC2_METADATA_DISABLED=true` prevents fallback to the node instance profile. EKS Pod Identity must inject container credential endpoint and authorization-token file settings for the dedicated service account. The AWS agent's projected token is independent of the Kubernetes API token; the chart disables automatic Kubernetes API token mounting.

## Evidence and remaining gates

Helm rendering tests verify the identity/config mounts are present only on ClickHouse and reject invalid Secret names. Lifecycle regression tests verify the new Pod Identity can be removed only with its exact cluster, role, namespace and service account. These tests do not prove credentials are accepted by a live ClickHouse server or IAM.

Before allowing backup-based shutdown, verify the selected immutable ClickHouse image implements Pod Identity's container authorization-token-file credential flow, perform a synthetic S3 BACKUP, verify versioned backup objects, and restore them into an isolated database. Check failures prevent downstream deletion. The official credential-chain documentation names container credentials but does not establish compatibility for every pinned image; token-file support is an explicit staging gate. No deployment, backup, restore, shutdown or IAM attachment is executed by these source changes.

References: [ClickHouse S3 credentials](https://clickhouse.com/docs/integrations/s3#managing-credentials), [ClickHouse backup/restore](https://clickhouse.com/docs/operations/backup), [AWS EKS Pod Identity minimum SDK versions](https://docs.aws.amazon.com/eks/latest/userguide/pod-id-minimum-sdk.html).
