# ADR-0005: Use a short-lived role for EKS administrator access

## Status

Accepted for staging and production runtime plans; revalidate the trusted operator before apply.

## Context

The runtime Terraform accepted `administrator_role_arn` but wired that value directly into an EKS access entry. The authorized `research-operator` profile currently resolves to an IAM user, and no existing project operator role was found. Using that long-lived user directly for cluster-wide EKS administrator access does not meet the deployment guidance that calls for an assumed role. The existing GitHub OIDC roles are not a substitute: their trust subjects name a different repository and their permissions require separation and review.

## Decision

The runtime stack accepts a same-account `operator_principal_arn` identifying an IAM user or role. It creates a dedicated `${environment}` EKS cluster-administrator role with a one-hour maximum session duration and a trust policy for that principal. The EKS access entry and `AmazonEKSClusterAdminPolicy` association reference the generated role ARN. Terraform exposes that ARN as `cluster_admin_role_arn`; operators use it with `aws eks update-kubeconfig --role-arn`.

The Terraform provisioning identity and the EKS Kubernetes administrator identity remain distinct. Runtime creation is performed with the authorized provisioning profile. Kubernetes access requires a separate STS role session. Production deployment automation continues to require its own corrected OIDC trust, least-privilege policies, and protected GitHub environments.

## Consequences and limits

- The operator principal must be in the same AWS account and must be allowed to call `sts:AssumeRole` for the generated role. The role trust does not itself grant that caller permission. The operator also needs `eks:DescribeCluster` for `aws eks update-kubeconfig`; neither permission was tested in this session.
- The role receives cluster-wide Kubernetes administrator permissions through the EKS access policy; its one-hour AWS role session limits session duration but does not replace protected identity controls or audit review.
- The current authorized IAM user belongs to an `Admins` group with `AdministratorAccess`; effective permissions, MFA requirements, permission boundaries, and organization policies were not simulated. This is not an approval to provision or use cluster-admin access.
- The generated role is runtime-scoped. Recreate and review the runtime plan after the foundation exists and before any authorized apply. Do not apply the earlier plan that bound the IAM user directly.

## Evidence

- Read-only IAM queries on 2026-11-15 found no existing project operator role; `research-operator` is an IAM user in the `Admins` group. Its managed group policy name is `AdministratorAccess`.
- An independent platform review of the first isolated runtime plan found direct user-based EKS cluster administration and recommended an assumed role. The plan was not applied.
- Terraform validation passed and an updated isolated no-lock runtime plan was independently reviewed on 2026-11-15: 39 creates, 0 updates, 0 deletes. Its empty runtime state and assumed foundation identifiers make it review-only; it must be regenerated against actual foundation outputs/state before any separately authorized apply. No AWS resources or IAM policies were changed by this ADR.
