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
