# AWS cost estimate (staging and production)

**Status:** preliminary, not a quote, budget cap, or deployment authorization. **Price date:** 2026-11-15. **Region:** `us-east-1`. **Currency:** USD. **Pricing basis:** public AWS on-demand rates, no savings plans, private discounts, Free Tier credits, tax, or support plan. EC2 and RDS unit prices were read from the AWS Price List API using `research-operator`; no infrastructure was created or changed.

## Modeled runtime

The Terraform baseline creates one EKS cluster, one `m6i.2xlarge` CPU node, one `g6e.xlarge` GPU node, and one Single-AZ `db.t4g.small` RDS instance per active environment. The GPU node group has `desired_size = 1`. The ingress chart groups the application and admin console on one public ALB; the model assumes one average LCU. The subnets assign public IPv4 addresses to both nodes. The ALB is modeled with two public IPv4 addresses, one in each selected AZ. The price model uses the documented 20 GiB Linux EKS node-group default for the CPU root volume, the configured 100 GiB GPU root volume, and the RDS 20 GiB initial storage allocation. RDS storage autoscaling can grow to 40 GiB, so the 20 GiB baseline is not a storage ceiling; at the modeled $0.115/GiB-month rate, a fully allocated additional 20 GiB adds up to $2.30 per active month. Node volumes are modeled as gp3; the actual EBS volume type should be confirmed from a reviewed plan. RDS gp2/gp3 storage has the same listed price for this estimate.

| Runtime component | Price assumption | Cost per active environment hour |
| --- | --- | ---: |
| EKS standard-support cluster | $0.10 / cluster-hour | $0.1000 |
| CPU node, `m6i.2xlarge` Linux On-Demand | $0.384 / instance-hour | $0.3840 |
| GPU node, `g6e.xlarge` Linux On-Demand | $1.861 / instance-hour | $1.8610 |
| RDS PostgreSQL, `db.t4g.small`, Single-AZ | $0.032 / instance-hour | $0.0320 |
| RDS 20 GiB allocated storage | $0.115 / GiB-month, prorated over 730 hours | $0.0032 |
| EBS node volumes, 120 GiB combined | $0.08 / GiB-month, prorated over 730 hours | $0.0132 |
| One ALB and one assumed average LCU | $0.0225 / ALB-hour + $0.008 / LCU-hour | $0.0305 |
| Four public IPv4 addresses | $0.005 / address-hour | $0.0200 |
| **Modeled runtime subtotal** | Assumes all listed resources run together | **$2.4438 / hour** |

At the specification's limit of **20 combined environment-hours per month**, this runtime subtotal is **$48.88/month**. A four-hour session is about **$9.78**. This assumes the GPU node is active for every hour; a measured GPU-off period would reduce the estimate. The subnet, ALB, node count, average LCU, and disk assumptions must be checked against the final reviewed Terraform/Helm plan.

## Retained resources and usage-dependent charges

The shutdown design retains foundation services and data. Those costs continue after EKS and RDS runtime resources are removed.

| Retained item | Current modeled amount | Notes |
| --- | ---: | --- |
| Foundation Secrets Manager secrets | $2.40/month for two environments | Terraform creates three foundation secrets per environment at $0.40/secret-month; API calls are extra. RDS-managed credentials may add a secret while the DB exists. |
| ClickHouse and Valkey EBS volumes | $3.52/month for two environments | One 20 GiB and one 2 GiB gp3 volume per environment, retained by the Helm StorageClasses. This assumes one pair per environment and no abandoned older volumes. |
| Customer-managed KMS alert keys | $2/month base for two environments; up to $6/month after the first two rotations per key | One dedicated key per environment. KMS request use is unmodeled; AWS's current price page adds $1/month for each of the first two key rotations. |
| SNS alert topic | No fixed topic charge; usage unmodeled | Standard-topic requests and endpoint deliveries are usage-based. AWS's current FAQ lists $0.50 per million requests and $2 per 100,000 email deliveries, with a monthly free allowance; the account's remaining shared allowance and alert volume are unverified. |
| Existing Route 53 hosted zone | $0.50/month account-wide | A public `research.spktsagar.com` zone already exists. Reusing it adds no new hosted-zone fee; this is its current recurring price contribution, excluding queries. Do not create another zone without need. |
| RDS snapshots | $0.095 per charged GiB-month | Snapshot storage has an account/Region free allowance tied to provisioned RDS storage. The account's current allowance and retained snapshot bytes were not inspected. Model conservatively at the full charged rate until measured. |
| S3 documents, ClickHouse/Valkey backups, versions | $0.023 per GB-month at the first S3 Standard tier | There are three versioned buckets per environment. Object/version growth, requests, lifecycle transitions, and retrievals are unbounded by the current plan. |
| ECR images | $0.10 per GB-month for private storage | Image sizes, version retention, and a future private vLLM repository are not fixed in Terraform. In-region transfer to AWS compute is free. |
| DynamoDB control table with PITR | Usage-dependent; PITR is $0.20 per GB-month | Table size and request volumes are not measured. A small table is expected but this is not a cap. |
| Cognito | $0 base MAU charge at the five-user target under Essentials | First 10,000 direct/social MAUs are included; SMS, paid email usage, or other plans can add charges. |
| SQS | $0 at or below 1 million requests/month | AWS's free request allowance; traffic above it is billed. |
| CloudWatch, SNS, DNS queries, data transfer, CI, taxes | Not quantified | Log ingestion, ALB LCU use above one, cross-AZ traffic, SNS publishes/delivery, email, internet egress, GitHub runner charges, taxes, and other account activity may add cost. The source has no NAT Gateway. |

## Expiry control-plane sensitivity

`infra/expiry/main.tf` creates a session-named EventBridge Scheduler schedule, Step Functions state machine, CodeBuild project (90-minute timeout), CloudWatch log group and two alarms, and SQS dead-letter queue. The alarms use the environment SNS topic from foundation; alert endpoint and delivery usage are not included in the modeled total. The one-time schedule is configured to delete after it successfully invokes its target; this does not wait for the Step Functions/CodeBuild shutdown workflow to finish. Workflow failures are monitored separately by a Step Functions alarm, while scheduler target-delivery failures are monitored by an alarm on the DLQ's visible-message count. The two-sample DLQ alarm reduces sensitivity to transient approximate SQS metrics. Schedule deletion is not evidence of successful expiry. The expiry execution destroys the runtime stack but does not explicitly destroy the other expiry-stack resources; the lifecycle inventory reports any that still exist as retained for teardown accounting. Because names include the session ID, resources left by failed or incomplete sessions may accumulate. Charges differ by resource: CodeBuild and Step Functions charge for executions, CloudWatch alarms and retained log bytes can continue to incur monthly charges, and queue costs depend on requests/messages. An idle CodeBuild project or state-machine definition does not itself incur build or transition charges. Keep cleanup verification open; do not treat schedule self-deletion as full expiry-controller cleanup.

For a sensitivity case of five four-hour sessions per month, one expiry build per session, and each build consuming its full 90-minute timeout:

| Service | Sensitivity calculation | Monthly amount / treatment |
| --- | --- | ---: |
| CodeBuild | 5 builds × 90 minutes × $0.005/minute for Linux `BUILD_GENERAL1_SMALL` | $2.25 before any available account-wide free minutes; actual build duration is unmeasured, and 90 minutes is the configured timeout. |
| Step Functions Standard | 5 executions × 1 task-state transition × $0.000025/transition | $0.000125 before the 4,000-transition monthly free tier; other account executions may consume that tier. |
| EventBridge Scheduler | 5 one-time schedule invocations | Within the published 14 million monthly free invocations; other account usage and current eligibility still apply. |
| CloudWatch alarms | 2 standard metric alarms per retained session control plane × $0.10/alarm-month | $0.20/month for each session control plane still present; five accumulated pairs would add $1.00/month. |
| CloudWatch logs | Actual build log bytes × $0.50/GiB ingested, plus retained bytes × $0.03/GiB-month | Unmeasured; the configured seven-day log retention limits age, not ingestion. |
| SQS dead-letter queue | Requests and retained messages | Usage-dependent; no message-volume or failure rate is measured. |

The CodeBuild row is a timeout-based sensitivity, not a forecast or hard maximum for the complete architecture. Repeated failures, log growth, account use of free allowances, and accumulated alarms/control-plane resources can change the total. These rates are from the current AWS pages for [CodeBuild](https://aws.amazon.com/codebuild/pricing/), [Step Functions](https://aws.amazon.com/step-functions/pricing/), [EventBridge Scheduler](https://aws.amazon.com/eventbridge/scheduler/), and [CloudWatch](https://aws.amazon.com/cloudwatch/pricing/) as checked on 2026-11-15. The CodeBuild and Step Functions examples are based on the configured `BUILD_GENERAL1_SMALL` Linux compute type and one-task state machine; measure actual invocations and build duration before treating the sensitivity as representative.

RDS backup objects are produced by both the planned explicit backup and the RDS final-snapshot path. For sensitivity only, the maximum five four-hour sessions implied by 20 monthly environment-hours would retain **100–400 GiB** if each session leaves one or two independent full snapshots of 20–40 GiB. At $0.095/GiB-month that is **$9.50–$38/month** before the account-level free allowance. RDS snapshots are incremental and actual consumed storage may be lower; the true retained bytes and snapshot lineage need a real backup receipt and account usage evidence. This sensitivity is not a promised upper bound.

With 20 runtime hours and the simple retained baseline above (two foundation secret sets, two EBS volume pairs, two alert KMS keys, one existing hosted zone, and the snapshot sensitivity), the illustrative subtotal is about **$66.80–$95.30/month** before key-rotation charges. After the first two automatic rotations per key, the comparable range can reach **$70.80–$99.30/month**, before S3, ECR, DynamoDB, logs, SNS/KMS requests, transfers, vLLM image storage, Bedrock, Tavily, and any other account costs. Do not use this subtotal as a budget commitment.

## Cost exposure if runtime cleanup fails

At the modeled rates, leaving one full environment running for a 730-hour month would cost about **$1,783.98** for the listed runtime resources, before retained storage and usage charges. The $200/month target depends on the four-hour session limit, 20 combined monthly environment-hours, and verified runtime teardown. AWS budget alerts are notifications, not hard spending caps. The current deployment and lifecycle workflows fail closed because backup, restore, expiry, and teardown acceptance are incomplete. Do not bypass these gates to save time.

The 20-hour schedule implies at most five complete four-hour sessions per month if all sessions use the maximum length. This is a planning assumption, not an AWS-enforced quota. The four-vCPU G/VT quota also allows only one `g6e.xlarge` at once in the checked region.

## Material exclusions and open inputs

- The AWS subtotal excludes Bedrock and Tavily. One authorized 20-case Nova Lite/Tavily evaluation used 64 model generations and 18 Tavily requests (14 reported credits), with a conservative usage-derived upper estimate of $0.16384666; the owner confirmed more than 200 free Tavily credits and pay-as-you-go disabled. This is one workload sample, not a per-run price or future spend cap. See [the evaluation record](live-evaluation-proposal.md); any further live run needs its own explicit limits and authorization.
- S3 document and backup retention, version counts, ECR image retention/size, and CloudWatch log volume have no agreed caps. The project currently keeps data and versions for restoration, so these costs grow with use.
- The owner selected Apache-2.0, authorized the `research-operator` profile, and approved using `research.spktsagar.com`. These choices do not change the price table; final release review and the exact staging DNS record plan remain open.
- The Route 53 hosted zone is delegated from the Cloudflare-served parent domain. Reuse of the existing zone is approved; no new hosted zone is needed for this estimate. Confirm the exact staging record and its current target/health before any DNS change, preserving the apex and personal website.
- A dated AWS Budget snapshot is recorded in [docs/aws-preflight.md](aws-preflight.md): the unfiltered account-wide $100 monthly budget reported $6.152 actual and $6.226 forecast at 2026-11-15 20:29 UTC. This is not full Cost Explorer/billing-history analysis or a hard cap. Free Tier eligibility and shared account-wide allowances remain unverified; unrelated account usage may change actual charges.
- Public price-list values and service terms can change. Refresh rates, run the AWS Pricing Calculator with the reviewed final deployment plan, and include existing-account usage before staging authorization. This document does not make an AWS Budget a hard cap.

## Sources

- AWS Price List API, `us-east-1`, queried 2026-11-15: [EC2 offers](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/us-east-1/index.json), [RDS offers](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonRDS/current/us-east-1/index.json).
- AWS service pricing: [EKS](https://aws.amazon.com/eks/pricing/), [Elastic Load Balancing](https://aws.amazon.com/elasticloadbalancing/pricing/), [VPC public IPv4](https://aws.amazon.com/vpc/pricing/), [EBS gp3](https://aws.amazon.com/ebs/general-purpose/), [RDS PostgreSQL](https://aws.amazon.com/rds/postgresql/pricing/), [Secrets Manager](https://aws.amazon.com/secrets-manager/pricing/), [S3](https://aws.amazon.com/s3/pricing/), [ECR](https://aws.amazon.com/ecr/pricing/), [Cognito](https://aws.amazon.com/cognito/pricing/), [SQS](https://aws.amazon.com/sqs/pricing/), [DynamoDB](https://aws.amazon.com/dynamodb/pricing/), and [Route 53](https://aws.amazon.com/route53/pricing/).
- [EKS managed node group disk default](https://docs.aws.amazon.com/eks/latest/APIReference/API_CreateNodegroup.html).
- [RDS backup storage allowance and snapshot behavior](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_WorkingWithAutomatedBackups.html).
- [AWS CodeBuild pricing](https://aws.amazon.com/codebuild/pricing/), [AWS Step Functions pricing](https://aws.amazon.com/step-functions/pricing/), [Amazon EventBridge Scheduler pricing](https://aws.amazon.com/eventbridge/scheduler/), and [Amazon CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/), checked 2026-11-15 for the expiry sensitivity above.
- [Amazon SNS pricing](https://aws.amazon.com/sns/pricing/) and [SNS pricing FAQs](https://aws.amazon.com/sns/faqs/), checked 2026-11-15 for the alert-topic note above. Free usage is shared across regions and account usage/eligibility has not been checked; no notification delivery has been measured.
- [AWS KMS pricing](https://aws.amazon.com/kms/pricing/), checked 2026-11-15 for the two-key base and rotation sensitivity above. Request counts and available free usage remain unverified.
