# AWS staging preflight observations

**Observed:** 2026-11-15, `us-east-1`, AWS CLI profile `research-operator`. **Access:** read-only API calls. **Outcome:** no resource changes. This is an evidence snapshot, not staging approval, a cost cap, or proof of future capacity.

## Identity and current runtime footprint

- The owner authorized using the `research-operator` profile for this project. `sts get-caller-identity` succeeded for that profile; the account ID is intentionally not repeated in this document. Confirm the account-scoped provider still resolves to this identity before any plan or mutation.
- The owner reports that `spktsagar.com` is registered to them and currently serves their personal website through CloudFront, and explicitly approved using `research.spktsagar.com` for the project even if it is already in use elsewhere. A read-only Route 53 query found a public `research.spktsagar.com` hosted zone. Its records are NS and SOA at the zone root, plus an alias A record for `staging.research.spktsagar.com` targeting `research-assistant-staging-1487266862.us-east-1.elb.amazonaws.com`. The zone root itself has no A/AAAA record. Read-only Classic ELB and ELBv2 inventories in this account/region returned no matching load balancer. Local DNS resolution was blocked, so live target health and whether the alias resolves elsewhere remain unverified. Preserve the existing staging alias and the apex/personal website. No DNS records were changed; any record change needs a reviewed collision/traffic plan and explicit staging authorization.
- On 2026-11-15, `bedrock get-foundation-model-availability` reported model agreement, entitlement, and region status `AVAILABLE` for `amazon.nova-lite-v1:0` in `us-east-1`. This is a metadata-only availability check; it did not invoke the model, confirm inference succeeds, or incur token charges. Recheck immediately before an approved run.
- EC2 `describe-instances` returned no pending or running instances in `us-east-1`.
- EKS `list-clusters` returned an empty list in `us-east-1`.
- RDS `describe-db-instances` returned no DB instances in `us-east-1`.
- These checks cover those service/resource types in this Region only. They do not inventory S3 objects, snapshots, ECR, Secrets Manager, other Regions, or non-EC2 charges.

## Quotas, usage and capacity

- Service Quotas reports 4 On-Demand G/VT vCPUs and 32 Standard On-Demand vCPUs in the account/Region. The desired `g6e.xlarge` consumes all four G/VT vCPUs, so the limit permits one such instance at a time.
- CloudWatch `AWS/Usage` had no G/VT usage datapoints during the queried seven-day window ending 2026-11-15 13:00 UTC. Standard usage datapoints reached 2 vCPUs on 2026-11-12 and 2026-11-13; the current EC2 inventory had no pending/running instances when checked. Absence of a usage datapoint is not proof of capacity.
- A prior EC2 instance-type offerings query showed the selected instance types offered across multiple Availability Zones. A read-only `describe-capacity-reservations` query found no active reservation for `m6i.2xlarge` or `g6e.xlarge` in `us-east-1`; this work requested no reservation. Offerings only show regional/AZ support, and absence of a reservation does not prove that On-Demand capacity is unavailable or available. Capacity must be rechecked immediately before any authorized deployment.

## Existing Terraform-state bucket metadata

- The only bucket returned by the read-only account bucket-name query with the project's documented `ra-<account>-` prefix was `ra-<account>-terraform-state`. The query did not list object keys or read Terraform state.
- Bucket metadata shows versioning enabled, SSE-S3 (`AES256`) default encryption, and all four S3 Block Public Access settings enabled.
- Its lifecycle rules expire current `staging/recovery/` objects after 3 days and current `production/recovery/` objects after 7 days; noncurrent versions under both prefixes expire after 1 day. This is short-lived operational recovery retention, not evidence of durable application-data backups. Confirm the intended retention and backup layout before staging; do not assume those prefixes preserve long-term restore points.
- No application originals/backups buckets with that prefix were returned. This limited name query does not prove there is no application data in buckets with other names or in other Regions.

## Budget snapshot

- The account-wide `research-assistant-gross-usage` AWS Budget has a $100 USD limit and no cost filters. On 2026-11-15, its calculated actual was $6.152 and forecast was $6.226 (last updated 09:10:50 America/New_York). Actual-spend notifications at $50, $75, and $100 all reported `OK`; `describe-budget-actions-for-budget` returned no actions.
- The separate `My Zero-Spend Budget` has a $1 limit; on 2026-11-15, its calculated actual was $0 and forecast was $0.073 (last updated 09:10:56 America/New_York). It has an actual-spend notification at $0.01 with state `OK`; its budget actions list is empty. Neither budget provides an enforced spend stop. Calculated actuals are not a final invoice and may lag usage.
- These snapshots do not prove the portfolio's $200/month objective. The $100 budget is account-wide, and the preliminary project estimate excludes several variable costs and existing account usage. See [the cost estimate](aws-cost-estimate.md).

## Staging foundation plan (2026-11-15)

- The authorized `research-operator` identity was revalidated with STS; the account identifier is not repeated here. The documented Terraform state bucket exists, but a read-only `HeadObject` for `deepresearch/staging/foundation.tfstate` returned 404. The state object contents were not read.
- A saved plan was generated from an isolated copy of the current foundation Terraform source, using the staging backend configuration and `AWS_PROFILE=research-operator`. Planning used `-lock=false`; the operation did not acquire or write a state lock and did not apply resources. Exact planned callback/logout host: `staging.research.spktsagar.com`.
- The plan proposes 25 creates, 0 changes, and 0 deletes: 15 S3 bucket/security/versioning/policy resources, 2 ECR repositories, 3 Secrets Manager containers, 2 SQS queues, and 3 Cognito resources. Exact-name read-only checks found no planned staging S3 bucket, ECR repository, secret container, queue, Cognito pool, or Cognito hosted-domain collision in the inspected account/region.
- Plan SHA-256 is `d489315fbe57191298331aadd6f2ced5dc257d6c3a5e3c30c794ee8ec2e6d145`; source hashes and the restricted local artifact path `local-data/aws-preflight/2026-11-15/README.md` are recorded with the ignored local artifact. An independent platform review confirmed the plan/source scope and identified two open concerns: the existing `staging.research.spktsagar.com` Route 53 alias still has no verified live target/health, and retained versioned S3/ECR storage has no hard cost bound. No DNS or AWS resource was changed.
- This is a foundation-only planning artifact; it does not include runtime VPC/EKS/RDS, ingress, workload IAM, DNS, backup/expiry implementation, or a full staging cost estimate. It is not apply authorization. Recreate and review fresh complete foundation/runtime plans after remaining preflight inputs are resolved.

## GitHub Actions OIDC and IAM roles

- The AWS account has the GitHub Actions OIDC identity provider and two roles named for staging and production GitHub use. The current GitHub repository is `sagar-spkt/DeepResearchAgent`; both role trust policies instead name `sagar-spkt/ResearchAgentOnAWS` (with repository-ID subject components). Those trust subjects do not match the current repository, so this project's workflows cannot assume these roles as configured. No OIDC token exchange was attempted.
- Both roles have environment-scoped permissions, including `environment-maintenance` permissions capable of deleting tagged environment resources and retained objects (including S3 object versions), deleting the environment ECR repository and Cognito pool, and scheduling tagged KMS keys for deletion. These destructive permissions are attached to the same staging/production GitHub roles; this audit did not establish whether a separate purge identity exists. Treat the current roles as destructive and do not use them for this project until the role split and protections are reviewed.
- The attached `deployment-network` policies restrict `ec2:RunInstances` to `t3.large` and `g4dn.xlarge`; the project specification calls for `m6i.2xlarge` and `g6e.xlarge`. Their launch authorization therefore does not establish permission for the planned node types. Reconcile the exact IAM condition/resource behavior against the final Terraform plan before deployment.
- `environment-operations` grants deletion of the environment's S3 lifecycle/cluster-access lock objects. Review this against the shared lease and state-lock guarantees before any workflow is allowed to use the role.
- The 2026-11-15 authenticated GitHub CLI check succeeded with `repo` and `workflow` token scopes; workflow and run listings worked, and the account API reports `admin`/`push` permission. The remote `main` is `497519f4bc55a8775a6bbc68f789d2d92d07b12d`; the local checkout is two commits ahead at `bb5f888fbda77edac0409bb3c84c54312a4e096a` and has 60 uncommitted paths. No workflow was dispatched. Read-only repository settings still show zero environments, zero rulesets, no protection on `main`, Actions allowing all actions, and SHA pinning not required. Local workflow source remains unpublished and has only `contents: read` with no AWS credentials step; no hosted deployment can use these local changes. Do not change trust, policies, or repository settings without the specific authorization and protected change plan.
- This is a preliminary review of the observed trust and attached/inline policy documents, not an effective-permissions sign-off. No IAM policy simulation, permissions-boundary analysis, organization SCP evaluation, or OIDC token exchange was performed.

## Still needed before staging

- Before staging, bind the complete foundation/runtime plan to the confirmed `research-operator` identity and owner-approved `research.spktsagar.com` hostname. Preserve the apex and personal website. The reviewed foundation plan uses `staging.research.spktsagar.com` for Cognito callback/logout, but the existing alias target remains unresolved; public DNS resolution/delegation and alias health could not be independently checked from this environment. Any DNS mutation remains gated on reviewing the exact record plan plus explicit staging authorization.
- A current final-plan cost review that includes account history, retained data, logs, ECR, transfers, Bedrock and Tavily. The user must set explicit per-run and total spend ceilings before any live model/search evaluation.
- A deployment-time capacity check for G/VT and Standard instances; no capacity reservation was observed or created.
- Approved vLLM/model artifacts, secret references (never values), backup/snapshot retention, and an implemented and rehearsed shared lifecycle lease with expiry/backup/restore gates.
- Hosted GitHub protections and required checks; a trust subject matching this repository; role separation for deployment, operations and purge; reviewed permissions matching the current Terraform plan; and hosted evidence. Current local workflows remain preparation-only; do not treat them as deploy authorization.
- A reviewed, exact Terraform plan and rollback/data-retention procedure, followed by explicit task-specific staging authorization.

