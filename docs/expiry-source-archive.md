# Staging expiry source archive

`scripts/package_expiry_archive.py` creates the source ZIP consumed by the `infra/expiry` CodeBuild executor. It is a local packaging step; it does not upload the archive, create an S3 version, or install expiry infrastructure. `.github/workflows/publish-expiry-image.yml` builds and publishes the separate CodeBuild runner image to the immutable `deepresearch-staging/expiry-runner` ECR repository and reports its digest. It requires the repository checks on that exact commit, the protected `staging` GitHub environment, and the dedicated ECR-only OIDC publisher role. The foundation ECR repository and OIDC role must be provisioned before running the workflow.

Run it only after producing the exact staging runtime destroy plan and expiry intent from the current admitted session. The `plan-expiry` command requires `--runtime-state-bucket` and `--runtime-backend`; it validates the backend's exact bucket, staging key, region, encryption and lock settings, then runs Terraform init against that backend before planning. The bucket, exact `deepresearch/staging/runtime.tfstate` key and backend file SHA-256 are included in the expiry intent hash. The intent must reference `artifacts/expiry.tfplan`, bind the matching binary plan hash, and have a canonical SHA-256 approved for that session. The JSON runtime variables must name the same staging session. The archived backend file must match the intent.

Example, with private files already prepared under an ignored local directory:

```sh
python3 scripts/package_expiry_archive.py \
  --workspace "$PWD" \
  --session-id "$STAGING_SESSION_ID" \
  --approved-intent-sha256 "$APPROVED_EXPIRY_INTENT_SHA256" \
  --intent artifacts/expiry-intent.json \
  --saved-plan artifacts/expiry.tfplan \
  --runtime-variables artifacts/runtime.auto.tfvars.json \
  --runtime-backend artifacts/runtime.backend.hcl \
  --approved-state-bucket "$STAGING_TERRAFORM_STATE_BUCKET" \
  --output local-data/aws-preflight/expiry-source.zip
```

The command includes only the lifecycle Python sources, runtime Terraform source and provider lock, the expiry intent, exact saved plan, session-bound JSON variable file, and runtime backend config. The backend config is stored at `artifacts/runtime.backend.hcl` for expiry-time hash verification and at `infra/runtime/artifacts/runtime.backend.hcl` for the CodeBuild command's `terraform -chdir=infra/runtime init -backend-config=artifacts/runtime.backend.hcl`. Both archive entries contain identical bytes. It rejects symlinked inputs, traversal, mismatched plan/intent/session hashes, credential or unapproved runtime-variable fields, a state bucket that differs from both the intent and explicit approval, unencrypted or wrong-state backends, and an existing output path. Entries are sorted with fixed timestamps and stored without compression; identical inputs produce identical archive bytes. The newly created file uses mode `0600`, and the command prints only the archive path, SHA-256, and mode.

The ZIP contains a Terraform binary plan and infrastructure identifiers, so treat it as private deployment material. Do not commit it, attach it to a public or ordinary GitHub artifact, or print its contents in logs. The expiry configuration must upload it directly to the designated versioned source bucket with the reviewed KMS key, then record and verify the exact S3 `VersionId` and object checksum before applying `infra/expiry`. The published CodeBuild image digest is only the image input; archive upload, expiry Terraform installation, executor policy review, and live readiness checks remain separate. Archive packaging and image publication do not make the expiry path ready or authorize application writers.

## Prepare the expiry stack inputs

After reviewing the immutable executor image and the runtime destroy plan, create a private JSON input file with exactly these fields: `approved_intent_sha256`, `runtime_state_bucket`, `management_vpc_id`, `management_subnet_ids`, `management_security_group_ids`, `source_bucket`, `source_kms_key_arn`, `runner_image_digest`, and `runner_repository_arn`. Values must come from the reviewed intent and current management/foundation outputs. The image value must be `repository@sha256:<64 lowercase hex digits>`, and the repository ARN, KMS key, and intent account/region must agree. Keep this file and the generated Terraform variables private.

Run the preparation command from the repository root, providing the same reviewed workspace that produced the ZIP:

```sh
python3 scripts/prepare_expiry_infra.py \
  --workspace "$PWD" \
  --intent artifacts/expiry-intent.json \
  --deployment-inputs local-data/aws-preflight/expiry-inputs.json \
  --archive local-data/aws-preflight/expiry-source.zip \
  --output local-data/aws-preflight/expiry.auto.tfvars.json
```

The command checks the archive's source files against the current checkout, verifies its exact intent, backend and saved plan, asks local Terraform to inspect that binary plan, and requires the inspected delete set to match the reviewed intent. It then explicitly uploads the ZIP under a content-addressed session key with a SHA-256 checksum, and verifies the returned non-null S3 version ID, KMS key, and server-validated checksum from the successful `PutObject` response. It performs no object read and does not need `GetObject`/`GetObjectVersion` or KMS decrypt access. The uploading identity needs `s3:PutObject` on the exact object prefix and `kms:GenerateDataKey` on the selected key, in addition to any bucket/key policies; S3 returns a null or absent version ID if versioning is not active, which the command rejects. It writes mode `0600` `infra/expiry` variables with the intent's four-hour `not_before` timestamp, exact uploaded object version, immutable image reference, separate management/runtime VPC IDs, and generated executor IAM policy documents. The policy documents are split to stay below the 6,144-character managed-policy size limit and capped at ten attachments. It never runs Terraform or applies the expiry stack. A failed upload or response verification stops before variables are written; if the upload succeeded but later local file creation fails, the immutable uploaded version remains for operator reconciliation.
