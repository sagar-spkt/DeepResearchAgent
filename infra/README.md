# AWS deployment foundation

These configurations are reviewable infrastructure source, not a verified deployed environment. No AWS changes have been applied.

## State and order

Bootstrap an encrypted versioned S3 Terraform-state bucket separately. Supply per-environment backend configuration using the examples. Foundation contains retained documents/backups/traces buckets, identity, secret containers, image repositories and queues. Runtime creates the VPC, EKS, node groups and private RDS. Feed the foundation output into runtime variables explicitly. Never share state keys across environments.

Initialize each layer with `terraform init -backend-config=...`, then `terraform plan -out=...`; inspect the exact saved plan before apply. Pin provider checksums with `terraform providers lock` in a network-enabled, authorized environment. Provider binaries/checksums are not vendored here.

## Deliberate deployment prerequisites

Set region/AZs, operator CIDR ranges, Kubernetes version in standard support, administrator role, unique session ID, callback URLs/domain, and foundation outputs. Do not pass 0.0.0.0/0 as EKS administrator access. Use a self-hosted ephemeral GitHub runner within the approved network or add a narrowly scoped runner egress CIDR per deployment. GPU capacity/quota and the opposite environment's shutdown must be verified first.

Create application database credentials and vLLM key in Secrets Manager. Bootstrap separate database roles/databases with database/bootstrap.sql from an in-VPC job. Run backend migrations before opening ingress. App SQL cannot use the RDS master identity. RDS extension availability must be verified on the selected engine version.

Install a pinned NVIDIA device-plugin chart, AWS Load Balancer Controller with reviewed least-privilege IAM, and DNS/certificate resources before enabling ingress or persistent observability. ALB/NVIDIA controller installations are operator prerequisites; Terraform defines the EBS CSI add-on with Pod Identity (provider schema validation pending). no cluster-admin credentials or wildcard controller policy is fabricated here. ALB and certificates are intentionally not created by Terraform because ingress reconciliation owns the ALB.

Pod Identity grants application service account access only to the environment's documents, research queue and secret containers. Terraform defines a separate observability role scoped to the traces bucket and service account research-observability. It must not inherit the application service account.

## Retention and lifecycle

Retained resources have prevent_destroy. RDS has deletion protection and mandatory final snapshots: ordinary terraform destroy will fail until a reviewed lifecycle procedure verifies durable database, ClickHouse and identity backups, drains work and intentionally changes protection. This is deliberate fail-closed behavior, not an automatic shutdown implementation.

A complete four-hour AWS-side expiry controller, backups/restore rehearsal, safe draining, cost admission and teardown inventory are blockers before any paid demo deployment. The lifecycle script reports these blockers and does not destroy anything. Billing continues if resources are created before these prerequisites are implemented. Foundation purge must explicitly inventory versions, secrets, ECR and Cognito; it cannot be achieved by a generic destroy command.

## Limits

One CPU node/single-AZ database is not HA. CPU capacity must be measured with Langfuse dependencies. Provider validation, deployment, model compatibility, load/cost testing and backup restoration remain unverified. No $200 spending guarantee is implied.
