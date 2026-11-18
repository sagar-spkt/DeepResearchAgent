# ADR-0010: Keep the expiry runner network separate from runtime

- Status: accepted for source implementation; live integration unverified
- Date: 2026-11-16

## Decision

Create a session-scoped management VPC in separate Terraform state. CodeBuild runs in a private subnet with HTTPS egress through a NAT gateway and an S3 gateway endpoint. It receives no public address or inbound security-group rules. The fixed management CIDR is distinct from the runtime CIDR.

For the initial staging design, use the existing EKS public HTTPS API endpoint with its source allowlist restricted to the management NAT public address as a `/32`, plus explicitly approved operator host addresses. Keep private endpoint access enabled for cluster-local traffic. EKS authentication and scoped Kubernetes RBAC are still required. The executor's current backup path needs only Kubernetes API access and AWS service APIs, so it does not require direct RDS access or VPC peering. Database bootstrap will run as an authorized in-cluster job.

This choice avoids peering and private DNS forwarding dependencies in the first release. It exposes only the restricted Kubernetes control-plane endpoint; inference and database services remain private. Prove executor DNS/TLS reachability and negative access from unauthorized sources before enabling expiry. CodeBuild network-interface IAM and runtime input wiring remain open. The current management root supplies the network only; it does not itself configure EKS or enable expiry. A future private-only control plane would require separately reviewed connectivity and operator access.

The VPC-attached runner cannot destroy its own network while finishing teardown. The GitHub-hosted `Finalize empty staging session` workflow now handles an admission where application writers never started: it requires the deployment marker and EKS/RDS runtime to be absent, checks the runner instance has stopped and requires the dispatching administrator to attest to removal of its exact GitHub registration, validates a session-bound delete-only management plan, removes the one-time runner token, then verifies the management VPC and NAT are gone. The finalizer lease is available only after the four-hour runtime window and remains available for cleanup of an overdue empty admission. Each overdue lease lasts at most two minutes and uses the same ownership, marker and session checks. This permits a fresh reviewed destroy plan after an interrupted finalization. The monthly reservation is never refunded.

This empty-session path does not handle an admission that may have run application writers. Such sessions still require the separate reviewed backup, drain, shutdown, and recovery workflow; the durable deployment marker permanently blocks empty-session finalization. It also does not create or remove expiry resources. Management resources have no deletion protection because they are disposable; retained application data stays in separate roots.

## Consequences

The runner can remain connected after runtime destruction. NAT and public-address charges continue until the external finalizer removes them. Include this session allowance in the reviewed cost plan and never retain an idle NAT as part of the monthly baseline. No AWS resources are created by this decision or offline mocked Terraform tests.

## Reference

[AWS EKS endpoint access](https://docs.aws.amazon.com/eks/latest/userguide/cluster-endpoint.html) documents the public/private endpoint combination and public source CIDR controls. The NAT `/32` design is this project's application of those controls; live connectivity is unverified.
