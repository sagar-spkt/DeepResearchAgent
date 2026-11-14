# Reviewed operator bootstrap

Run only from an authorized operator environment after reading infra/README.md. Commands below are concrete examples, not an instruction to apply unreviewed infrastructure. Replace paths with private operator files; never commit backend credentials or secret values.

1. Bootstrap a versioned/encrypted state bucket with public access blocked, GitHub OIDC provider, environment-specific deployment identities and a standard-support Kubernetes version. Use the example OIDC trust subject; attach reviewed environment-scoped permissions separately.
2. Produce exact saved plans:

```sh
terraform -chdir=infra/foundation init -backend-config=/private/path/staging-foundation.backend.hcl
terraform -chdir=infra/foundation plan -var-file=/private/path/staging-foundation.tfvars -out=/private/path/foundation.tfplan
terraform -chdir=infra/foundation show /private/path/foundation.tfplan
```

Apply only the reviewed saved plan after existing authorization. Export the foundation output into a private runtime tfvars file. Include the intended twelve-digit account_id, session_id and an explicitly chosen standard-support kubernetes_version; the provider refuses other accounts and tags runtime resources by session. The foundation creates secret containers, not credential values.

```sh
terraform -chdir=infra/runtime init -backend-config=/private/path/staging-runtime.backend.hcl
terraform -chdir=infra/runtime plan -var-file=/private/path/staging-runtime.tfvars -out=/private/path/runtime.tfplan
terraform -chdir=infra/runtime show /private/path/runtime.tfplan
```

3. Before runtime apply: verify the other environment is inactive, current regional estimate fits remaining monthly budget, quotas/capacity are available, and AWS-side expiry/backup/restore has been implemented and rehearsed. Current deployment preflight intentionally refuses deployment because live restore, inventory and common-lease enforcement evidence is incomplete. The shared infra/control table and reviewed admission are now source prerequisites. Do not bypass it for a paid demo.
4. After authorized runtime provisioning, connect to the exact cluster through an approved CIDR:

```sh
aws eks update-kubeconfig --region us-east-1 --name deepresearch-staging
kubectl create namespace research --dry-run=client -o yaml | kubectl apply -f -
```

5. Verify the Terraform EBS CSI add-on and install reviewed pinned ALB controller and NVIDIA device plugin using their documented IAM requirements. Provision ACM/Route53 and verify DNS ownership. Bootstrap private RDS roles using database/bootstrap.sql. Materialize application and model Kubernetes Secrets from Secrets Manager without logging their values. The application secret requires DATABASE_URL, S3_BUCKET, SQS_QUEUE_URL, COGNITO_ISSUER, COGNITO_CLIENT_ID, VLLM_API_KEY, and any configured tracing/bootstrap-owner values. Create a separate `research-web-env` secret containing AUTH_SESSION_SECRET, COGNITO_DOMAIN, COGNITO_CLIENT_ID and HTTPS APP_BASE_URL. The web chart sets APP_ENV, AUTH_MODE=cognito and API_INTERNAL_URL explicitly, and uses a separate service account without AWS identity. Application pods do not receive Secrets Manager read permissions; materialization belongs to the controlled bootstrap process.
6. Prepare a private values file with immutable backend/web/vLLM digests, model revision, hostname/certificate and ingress.enabled=true, vllm.enabled=true. Validate first:

```sh
helm lint helm/research -f /private/path/staging-values.json
helm template research helm/research --namespace research -f /private/path/staging-values.json > /private/path/rendered.yaml
```

Review rendered manifests and the migration Job; do not publish rendered secrets. With deployment authorization, use `helm upgrade --install research helm/research --namespace research --atomic --timeout 25m -f /private/path/staging-values.json`. Helm rollback cannot reverse database migrations; require compatible migrations or an explicit restoration plan.

7. Validate health, signup/approval/ownership, streaming/reconnect, document ingestion, model auth/network isolation and traces. Publish the exact tested image digests and commit, then promote those digests unchanged through protected production approval. A syntactically valid release manifest is not evidence of staging success.

Full paid deployment remains blocked until expiry and backup/restore are implemented; these examples make intended integration reviewable without claiming that deployment acceptance passed.
