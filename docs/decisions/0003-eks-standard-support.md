# ADR-0003: Use standard EKS support only

## Status

Accepted for the current portfolio baseline; validate again before provisioning.

## Decision

The current target as of 2026-11-14 is EKS Kubernetes 1.37. The Terraform EKS cluster sets `upgrade_policy.support_type` to `STANDARD` and disables bootstrap self-managed network add-ons. VPC CNI, kube-proxy, CoreDNS, Pod Identity Agent, and EBS CSI are explicitly EKS-managed add-ons with versions pinned below. Before any apply, verify that the chosen minor version and add-on versions remain offered in `us-east-1`, that the operator charts support it, and that account quota/capacity are available. The selected AWS Load Balancer Controller baseline is Helm chart 1.14.1 / controller v2.14.1, verified from the official chart package. Its multi-platform image manifest is pinned by digest and the chart's `tag@digest` render passed locally; installation remains gated on reviewed deployment authorization, least-privilege IAM, and cluster preflight. Preserve the version-matched CRD upgrade step.

## Rationale and consequences

AWS recommends creating clusters with its latest available Kubernetes version. EKS extended support costs extra; standard-only support avoids that charge and causes AWS to automatically upgrade the control plane at the end of standard support. Before the selected Kubernetes version reaches the end of standard support, update `kubernetes_version` and every version-pinned EKS add-on to a reviewed, compatible standard-support release, then validate and apply the coordinated upgrade ahead of AWS's automatic transition. Never respond to Terraform drift by downgrading an EKS cluster. Operators must budget for compatibility work and verify node groups, add-ons and application charts before that date. The version is a dated baseline, not a long-term pin or proof of cluster compatibility.

## Evidence and limits

- AWS lists 1.37 among standard-support versions and documents extended-support pricing: [EKS Kubernetes version lifecycle](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions.html).
- AWS documents controller v2.14.1 and manual CRD updates on upgrades: [Install the AWS Load Balancer Controller with Helm](https://docs.aws.amazon.com/eks/latest/userguide/lbc-helm.html). The official [AWS EKS chart repository](https://github.com/aws/eks-charts/tree/master/stable/aws-load-balancer-controller) publishes chart 1.14.1 with appVersion v2.14.1; the chart was locally linted and rendered against Kubernetes 1.37.0.
- Terraform AWS provider 6.20.0 supports `upgrade_policy.support_type` values `STANDARD` and `EXTENDED`: [aws_eks_cluster provider documentation](https://github.com/hashicorp/terraform-provider-aws/blob/v6.20.0/website/docs/r/eks_cluster.html.markdown).
- Read-only `DescribeAddonVersions` calls for Kubernetes 1.37 in `us-east-1` on 2026-11-14 confirmed these pinned versions: VPC CNI `v1.23.3-eksbuild.1`, kube-proxy `v1.37.0-eksbuild.39`, CoreDNS `v1.14.7-eksbuild.35`, Pod Identity Agent `v1.4.1-eksbuild.3`, and EBS CSI `v1.68.0-eksbuild.1`. The VPC CNI `enableNetworkPolicy` setting was checked against `DescribeAddonConfiguration` for its pinned version.
- Read-only Service Quotas reported four On-Demand G and VT vCPUs in `us-east-1`; no quota increase was requested.
- Read-only Service Quotas reported 32 On-Demand Standard vCPUs. EC2 offering queries found `g6e.xlarge` in us-east-1a/b/c/d and `m6i.2xlarge` in us-east-1a/b/c/d/f. Offering by AZ does not prove on-demand capacity or current quota usage.
- AWS Public ECR reported the controller multi-platform manifest digest as `sha256:13abee6e31c2f25ee91f9ae91aabe4f5bcaf6a70eccd372b36835786d2e0813e`; `linux/amd64` and `linux/arm64` manifests were present. The chart rendered `public.ecr.aws/eks/aws-load-balancer-controller:v2.14.1@sha256:…` locally at Kubernetes 1.37.0. Vulnerability scanning and a live install remain outstanding.
- No cluster was created. Actual GPU capacity, controller installation and application acceptance remain unverified.
