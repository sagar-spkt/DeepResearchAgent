# ADR-0007: Manage repository GitHub OIDC trust roles separately

- Status: accepted
- Date: 2026-11-16

## Context

The AWS account's previous staging and production GitHub roles trusted a different repository and combined trust with broad deployment and destructive maintenance policies. Those roles and their exclusive permission policies were removed. Replacement roles now trust this repository's immutable OIDC subject, the matching GitHub environment, and `refs/heads/main`, but were initially created outside Terraform.

## Decision

Manage both repository-level trust roles in the shared `infra/github-oidc` root and a single versioned S3 Terraform state key. Keep this state separate from session-scoped foundation/runtime/expiry roots. Protect role deletion and both exclusive empty-policy guards with `prevent_destroy`. The root owns only role trust and metadata; it attaches no inline or managed permission policies and detects policy attachment drift. Add AWS permissions only after the operational workflows, exact resource scopes, protected branch/environment rules, the owner-started manual production gate, and failure behavior are separately reviewed. The manual gate is self-authorization, not independent review; see ADR-0008.

The root pins the AWS provider and uses the existing account OIDC provider. Its trust conditions require `aud=sts.amazonaws.com`, the exact repository/environment `sub`, and `ref=refs/heads/main`. It never grants `id-token: write` to a workflow or activates an AWS credentials step by itself.

## Consequences

- Terraform plans can detect trust or role metadata drift without mixing identity lifecycle with temporary staging infrastructure.
- The state key is shared because both roles are repository-level; changes still require an exact reviewed plan and the `research-operator` profile. Both roles and four exclusive empty-policy guards were imported using a reviewed six-import plan on 2026-11-16. The saved import plan proposed zero AWS resource changes; the immediate post-import AWS-backed plan returned no changes.
- Imported live roles must match the reviewed source before applying any plan. Terraform state import writes metadata to the versioned, encrypted state bucket but does not mutate the IAM roles.
- The roles cannot deploy until separately reviewed permission policies and operational workflows are implemented.

## References

- [AWS GitHub OIDC role guidance](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_create_for-idp_oidc.html)
- [Terraform AWS provider 6.20.0 `aws_iam_role`](https://registry.terraform.io/providers/hashicorp/aws/6.20.0/docs/resources/iam_role)
- [Terraform AWS provider 6.20.0 `aws_iam_role_policy_attachments_exclusive`](https://registry.terraform.io/providers/hashicorp/aws/6.20.0/docs/resources/iam_role_policy_attachments_exclusive)
- [Terraform AWS provider 6.20.0 `aws_iam_role_policies_exclusive`](https://registry.terraform.io/providers/hashicorp/aws/6.20.0/docs/resources/iam_role_policies_exclusive)

## Import evidence

- Saved plan SHA-256: `a94d96ca4d02b3d27e968b3bf5a453e306891ddcb5fc4180c4ba6746338d058d`; six imports, zero adds, zero changes, zero destroys.
- Post-import AWS-backed `terraform plan -lock=false`: no changes. The sensitive plan, state, and input files are stored under ignored `local-data/aws-preflight/2026-11-16/` and must not be committed.
