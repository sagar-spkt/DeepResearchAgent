# EKS and Kubernetes upgrade policy

Status: operating proposal for this portfolio environment. It does not authorize a cluster change. The selected Kubernetes, add-on, chart, node AMI, and GPU plugin versions must be rechecked against AWS compatibility data and available capacity immediately before each change.

## Cadence

- Review AWS EKS release notes, supported versions, upgrade insights, add-on compatibility, AWS Load Balancer Controller releases, node AMIs, and GPU device-plugin compatibility monthly.
- Schedule routine patch updates quarterly. Triage critical security or availability fixes immediately and prepare an expedited, reviewed change when needed.
- Evaluate the next Kubernetes minor at least quarterly. Target adoption while the current minor is still in standard support, with a 90-day buffer before its end date. Use one minor-version step at a time. The project specification currently targets standard support only; do not plan to rely on extended support.
- Do not copy an old version number forward as “latest.” At change time, record the AWS Region, current and target versions, compatibility evidence, source URLs, and the EKS support end date. EKS standard support lasts 14 months from that version's EKS release; its later extended-support period is outside the project's target policy. See [EKS Kubernetes version lifecycle](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions.html).

## Change sequence and checks

1. Open a change record and capture the current Terraform state reference, cluster version, add-on versions, Helm releases, node AMI IDs, workload image digests, and recovery point. Confirm staging/production environment and lifecycle lease.
2. Review EKS upgrade insights, deprecated APIs, Kubernetes release notes, AWS add-on compatibility, Helm chart compatibility, and the official AWS Load Balancer Controller support matrix. Check subnet IP headroom, quota, regional instance capacity, PodDisruptionBudgets, and workload placement before scheduling.
3. Update only one Kubernetes minor at a time. Follow the EKS upgrade procedure: ensure nodes are compatible with the current control plane, update the control plane, then bring node Kubernetes versions into alignment and update cluster components. AWS documents add-ons and clients as explicit follow-up work; do not assume add-ons update automatically.
4. Upgrade pinned EKS add-ons (VPC CNI, CoreDNS, kube-proxy, Pod Identity Agent, and EBS CSI) to versions explicitly compatible with the target Kubernetes version. Preserve custom configuration deliberately and review any conflict resolution. EKS does not automatically advance add-ons when new versions are released or when the control plane is upgraded.
5. Upgrade the AWS Load Balancer Controller Helm release using a reviewed chart and image digest. For managed node groups, select a reviewed EKS-optimized AMI compatible with the target Kubernetes version. Validate the NVIDIA device plugin, CUDA/runtime compatibility, and GPU node taints before scheduling vLLM. Recheck all project charts with the pinned Kubernetes schema.
6. Run repository, Terraform format/init/validate, Helm lint/render, image/SBOM/vulnerability checks, and staging smoke/authorization tests. Observe API, worker, ingress, GPU scheduling, document processing, and Langfuse health through a four-hour demo session. Record failures and roll-forward/rollback actions.
7. Promote the same tested immutable artifact digests to production only through the protected release process. Do not perform staging and production cluster changes concurrently in the shared AWS account.

## Rollback and closure

- Before each step, define a measurable abort signal and an operator action. Stop before the next step if the control plane, add-on, ingress, node readiness, or application checks fail.
- Preserve prior application image digests and Helm/Terraform change records. Roll back application artifacts or Helm values where compatible; repair or roll forward incompatible cluster infrastructure. EKS control-plane rollback is available only for an eligible in-place upgrade to the immediately previous minor, initiated within seven days; node groups and add-ons require separate handling. Check rollback readiness and compatibility before relying on it. See [EKS cluster version rollback](https://docs.aws.amazon.com/eks/latest/userguide/rollback-cluster.html).
- Close the change only after the target versions, source evidence, successful checks, observed recovery behavior, and any residual risk are recorded in the infrastructure docs and release record.
- This project initially operates within a four-vCPU G/VT On-Demand quota and a limited demo schedule. A maintenance window must account for GPU capacity, quotas and cost; never use upgrade validation as a reason to provision or benchmark without the separate authorization required by the remaining-work plan.

## Official references

- [Understand the Kubernetes version lifecycle on EKS](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions.html)
- [Update an existing EKS cluster to a new Kubernetes version](https://docs.aws.amazon.com/eks/latest/userguide/update-cluster.html)
- [Update an Amazon EKS add-on](https://docs.aws.amazon.com/eks/latest/userguide/updating-an-add-on.html)
- [Update a managed node group](https://docs.aws.amazon.com/eks/latest/userguide/update-managed-node-group.html)
- [Roll back a cluster to a previous Kubernetes version](https://docs.aws.amazon.com/eks/latest/userguide/rollback-cluster.html)

