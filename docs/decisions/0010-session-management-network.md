# ADR-0010: Keep the expiry runner network separate from runtime

- Status: accepted for source implementation; live integration unverified
- Date: 2026-11-16

## Decision

Create a session-scoped management VPC in separate Terraform state. CodeBuild runs in a private subnet with HTTPS egress through a NAT gateway and an S3 gateway endpoint. It receives no public address or inbound security-group rules. The fixed management CIDR is distinct from the runtime CIDR.

Runtime will own its peering connection and specific routes to this network. Private EKS resolution/reachability and CodeBuild network-interface IAM must pass review before use. The current management root supplies the network only; it does not connect to EKS or enable expiry.

The VPC-attached runner cannot destroy its own network while finishing teardown. An external finalizer must wait for the runner to stop, verify runtime cleanup, remove the session expiry resources and management network, and verify NAT deletion and address release before closing admission. That finalizer is still unimplemented. Management resources have no deletion protection because they are disposable; retained application data stays in separate roots.

## Consequences

The runner can remain connected after runtime destruction. NAT and public-address charges continue until the external finalizer removes them. Include this session allowance in the reviewed cost plan and never retain an idle NAT as part of the monthly baseline. No AWS resources are created by this decision or offline mocked Terraform tests.
