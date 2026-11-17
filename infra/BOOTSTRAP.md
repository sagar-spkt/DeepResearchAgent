# Reviewed operator bootstrap

Run only from an authorized operator environment after reading infra/README.md. Commands below are concrete examples, not an instruction to apply unreviewed infrastructure. Replace paths with private operator files; never commit backend credentials or secret values.

1. Bootstrap a versioned/encrypted state bucket with public access blocked. A GitHub OIDC provider already exists. `deepresearch-staging-github-oidc` and `deepresearch-production-github-oidc` trust only this repository's immutable environment subject and `refs/heads/main`. The staging role's inline policy is limited to the staging deployment operations and the shared lifecycle lease; its DynamoDB access is only `GetItem` on the active-session/lifecycle-lock keys and `UpdateItem` on lifecycle-lock in `deepresearch-control`. Production has no AWS permissions. Manage both roles and the exclusive policy guards through `infra/github-oidc`. The policy regression test checks exact lease table/actions and rejects delete, scan, batch, and runtime-provisioning actions. The current policy has not been applied to AWS by this change; run and review a fresh saved plan before applying it. The current portfolio baseline is EKS Kubernetes `1.37` with `upgrade_policy.support_type = STANDARD`; this is a dated target, not a guarantee that the version is available in every region. Confirm `us-east-1` version and add-on availability, the opposite environment's lifecycle status, and the current support window before planning. Before standard support ends, update the Kubernetes and pinned add-on versions together to a reviewed compatible release and apply before AWS's automatic control-plane upgrade. Never plan a downgrade to reconcile drift after auto-upgrade.
   The historical import plan SHA-256 was `a94d96ca4d02b3d27e968b3bf5a453e306891ddcb5fc4180c4ba6746338d058d`: six imports and zero creates, updates, or deletes. Its reviewed apply wrote role metadata to the existing versioned state bucket and did not change IAM resources. Do not reuse that saved plan for current policy updates.
2. Produce exact saved plans:

```sh
terraform -chdir=infra/foundation init -backend-config=/private/path/staging-foundation.backend.hcl
terraform -chdir=infra/foundation plan -var-file=/private/path/staging-foundation.tfvars -out=/private/path/foundation.tfplan
terraform -chdir=infra/foundation show /private/path/foundation.tfplan
```

The foundation tfvars must include the intended twelve-digit `account_id`, `environment`, `region`, `domain`, and `callback_urls`; the provider's `allowed_account_ids` guard rejects credentials for any other account. Apply only the reviewed saved plan after existing authorization. Export the foundation output into a private runtime tfvars file. Include `account_id`, `session_id`, an explicitly chosen standard-support `kubernetes_version`, and the same-account `operator_principal_arn` (the IAM user or role allowed to assume the generated one-hour EKS administrator role); the runtime provider independently refuses other accounts and tags runtime resources by session. The foundation creates secret containers, not credential values.

```sh
terraform -chdir=infra/runtime init -backend-config=/private/path/staging-runtime.backend.hcl
terraform -chdir=infra/runtime plan -var-file=/private/path/staging-runtime.tfvars -out=/private/path/runtime.tfplan
terraform -chdir=infra/runtime show /private/path/runtime.tfplan
```

3. Before runtime apply: verify the other environment is inactive, current regional estimate fits remaining monthly budget, quotas/capacity are available, and AWS-side expiry/backup/restore implementation has passed offline validation and independent review. The first live recovery rehearsal requires a closed staging environment, synthetic data, and a reviewed provisioning-failure cleanup path as defined in [ADR-0009](../docs/decisions/0009-first-staging-recovery-rehearsal.md). Complete that rehearsal before opening ingress. Current deployment preflight still refuses deployment because operational implementation, inventory and common-lease enforcement evidence are incomplete. The shared infra/control table and reviewed admission are source prerequisites.
4. After authorized runtime provisioning, connect to the exact cluster through an approved CIDR:

```sh
aws eks update-kubeconfig --region us-east-1 --name deepresearch-staging \
  --role-arn "$(terraform -chdir=infra/runtime output -raw cluster_admin_role_arn)"
kubectl create namespace research --dry-run=client -o yaml | kubectl apply -f -
```

5. Verify the Terraform EBS CSI add-on and install reviewed pinned ALB controller and NVIDIA device plugin using their documented IAM requirements. The selected ALB baseline is Helm chart `1.14.1` / controller `v2.14.1`, verified from the official AWS chart package; validate its IAM policy before use. Provision ACM/Route53 and verify DNS ownership. Bootstrap private RDS roles using database/bootstrap.sql. Materialize application and model Kubernetes Secrets from Secrets Manager without logging their values. The application secret requires DATABASE_URL, S3_BUCKET, SQS_QUEUE_URL, COGNITO_ISSUER, COGNITO_CLIENT_ID, VLLM_API_KEY, and any configured tracing/bootstrap-owner values. Create a separate `research-web-env` secret containing AUTH_SESSION_SECRET, COGNITO_DOMAIN, COGNITO_CLIENT_ID and HTTPS APP_BASE_URL. The web chart sets APP_ENV, AUTH_MODE=cognito and API_INTERNAL_URL explicitly, and uses a separate service account without AWS identity. Application pods do not receive Secrets Manager read permissions; materialization belongs to the controlled bootstrap process.

For the ALB controller, use the reviewed service account and scoped IAM role. AWS's current guide describes controller `v2.14.1`; the official chart package `1.14.1` embeds that release. The immutable multi-platform image digest `sha256:13abee6e31c2f25ee91f9ae91aabe4f5bcaf6a70eccd372b36835786d2e0813e` was read from AWS Public ECR, and rendering confirmed that the chart accepts the standard `tag@digest` reference. The actual controller installation remains gated on the reviewed AWS deployment plan, least-privilege IAM, and cluster prerequisites.

```sh
helm repo add eks https://aws.github.io/eks-charts
helm repo update eks
helm pull aws-load-balancer-controller --repo https://aws.github.io/eks-charts \
  --version 1.14.1 --untar --untardir /private/operator-charts
helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller \
  --namespace kube-system --version 1.14.1 \
  --set clusterName=deepresearch-staging --set region=us-east-1 --set vpcId="$VPC_ID" \
  --set serviceAccount.create=false --set serviceAccount.name=aws-load-balancer-controller \
  --set-string image.tag=v2.14.1@sha256:13abee6e31c2f25ee91f9ae91aabe4f5bcaf6a70eccd372b36835786d2e0813e
```

Helm installs CRDs from the chart's `crds/` directory on initial installation. Before upgrading an existing controller, apply the version-matched CRDs from the exact extracted chart package first, then upgrade using the reviewed immutable controller artifact:

```sh
kubectl apply -f /private/operator-charts/aws-load-balancer-controller/crds/crds.yaml
# Then repeat helm upgrade --install with the exact chart and image digest above.
```

Recheck versions against the [AWS EKS Kubernetes lifecycle](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions.html) and [AWS Load Balancer Controller Helm guide](https://docs.aws.amazon.com/eks/latest/userguide/lbc-helm.html) before any cluster operation. Lint/render checks do not prove compatibility with a live EKS cluster. Do not execute these commands until the staging deployment authorization and preflight gates are satisfied.
6. Prepare a private values file with immutable backend/web/vLLM digests, model revision, hostname/certificate and ingress.enabled=true, vllm.enabled=true. Validate first:

```sh
helm lint helm/research -f /private/path/staging-values.json
helm template research helm/research --namespace research -f /private/path/staging-values.json > /private/path/rendered.yaml
```

Review rendered manifests and the migration Job; do not publish rendered secrets. With deployment authorization, use `helm upgrade --install research helm/research --namespace research --atomic --timeout 25m -f /private/path/staging-values.json`. Helm rollback cannot reverse database migrations; require compatible migrations or an explicit restoration plan.

7. Validate health, signup/approval/ownership, streaming/reconnect, document ingestion, model auth/network isolation and traces. Publish the exact tested image digests and commit, then promote those digests unchanged through protected production approval. A syntactically valid release manifest is not evidence of staging success.

Full paid deployment remains blocked until expiry and backup/restore are implemented; these examples make intended integration reviewable without claiming that deployment acceptance passed.
