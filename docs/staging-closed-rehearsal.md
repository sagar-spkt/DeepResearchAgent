# Closed staging deployment path

`.github/workflows/deploy.yml` has two manual modes. The default `public` mode still requires `STAGING_RECOVERY_ACCEPTED=true` and `STAGING_EKS_NETWORK_READY=true`. A normal push to `main` continues to select `public`; it cannot select the closed mode. The closed mode is only accepted for a manual dispatch on `main`, with the exact phrase `I AUTHORIZE CLOSED STAGING REHEARSAL`.

Closed mode uses the same reviewed staging chart and immutable images, but passes `vllm.enabled=false` and `ingress.enabled=false`. It does not run the staging DNS alias command and its job summary does not show a public URL. The chart still configures `MODEL_PROVIDER=vllm`; no fake provider or silent fallback is selected. With no vLLM Deployment, model-backed requests are unavailable. Do not admit users or send research requests in this mode. A successful Helm rollout is only closed application deployment evidence; it does not mean recovery has passed.

## Closed-mode readiness variables

Before dispatching closed mode, set each staging GitHub Environment variable below to the exact string `true` only after the named control is independently verified. Missing, empty, or any other value blocks the deploy job. These are **readiness** flags; none sets `STAGING_RECOVERY_ACCEPTED` or accepts backup/restore results.

- `STAGING_EKS_NETWORK_READY`: the in-VPC deployment runner and restricted EKS API access have been verified.
- `STAGING_LIFECYCLE_ADMISSION_READY`: the matching staging admission is available. The deploy job also checks the live admission and four-hour deadline when it acquires the lifecycle lease.
- `STAGING_EXPIRY_READY`: session-scoped expiry has been applied and the CodeBuild, Scheduler, and Step Functions path is configured before application writers start.
- `STAGING_EXPIRY_ARTIFACTS_READY`: the exact versioned, KMS-encrypted source archive; immutable runner image; reviewed executor IAM policy; and Kubernetes access entry/namespace RoleBinding are present and scoped to this staging session.
- `STAGING_BOOTSTRAP_CLEANUP_READY`: the separately reviewed provisioning-failure cleanup path is available for a failed first install, before there is data requiring backup.

A readiness flag is not a substitute for the artifacts or live verification it names. Do not set flags to get the workflow to run. The workflow does not install expiry infrastructure, construct missing artifacts, or accept restore evidence.

## Recovery blockers that remain

An empty `research` namespace cannot pass the current backup adapter: it scales and verifies five existing Deployments (`research-api`, `research-web`, `research-worker`, `langfuse-web`, and `langfuse-worker`) and later requires the named ClickHouse and Valkey backup pods, mounted ClickHouse client configuration, and S3 backup access. Therefore expiry cannot be treated as a verified backup/recovery path merely because its scheduler and runner exist. The first install still needs an independently reviewed bootstrap-cleanup path before starting writers.

The current `restore` lifecycle command verifies the retained receipt/provider objects and prepares inputs; it deliberately reports `database_restore_verified=false` and `application_restore_rehearsed=false`. No part of this workflow restores an RDS snapshot into an isolated instance, checks synthetic account/conversation/document records, restores ClickHouse/Valkey, or compares observability history. Public recovery acceptance must remain unset until those checks are implemented and executed with synthetic data against the real staging recovery path.

`infra/expiry` also requires operator-supplied inputs: source bucket/key/version and KMS key; immutable runner image digest and ECR repository ARN; exact reviewed `executor_policy_json`; management/runtime VPC IDs, management subnet IDs and security groups; alert topic; expiry intent SHA; and the exact runtime variable file and saved Terraform destroy plan in the source archive. The archive must contain the reviewed source/dependency locks, runtime backend reference and configuration, intent and saved binary plan. Current repository source does not build this archive or runner image automatically. The executor's backup policy must cover exact staging RDS snapshot creation/verification and retained S3 object/receipt operations in addition to the reviewed runtime destroy actions; the separate network policy covers only CodeBuild ENIs. Alert topic subscription, EKS endpoint access, namespace RBAC, and effective IAM permissions need live verification.

The currently documented empty EKS/RDS inventory and the absent live archive/image/policy/recovery evidence mean these prerequisites cannot be closed by changing GitHub variables alone. Running the closed workflow is not a safe substitute for them.
