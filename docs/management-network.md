# Session management network

`infra/management` defines a disposable operations network in separate state. It is source only; no management network is deployed. The fixed `10.70.0.0/16` CIDR does not overlap the current runtime `10.60.0.0/16`. The root binds the provider to the supplied AWS account, `us-east-1`, environment, and safe session identifier.

The network has one public NAT subnet and one private executor subnet in the same Availability Zone. A public NAT gateway has a dedicated EIP; the public route goes to the internet gateway and the private default route goes to NAT. Both subnets disable automatic public addresses. CodeBuild must receive only the private subnet output. The executor security group has no ingress and permits outbound TCP/443. The VPC resolver supplies DNS; DNS to custom external servers is not enabled by this security group. The free S3 gateway endpoint routes S3 traffic from the private route table without NAT processing. Its endpoint policy defaults to AWS's full-access endpoint policy; IAM and bucket policies still control actual S3 permissions. It grants no independent principal permissions.

AWS's [CodeBuild VPC guide](https://docs.aws.amazon.com/codebuild/latest/userguide/vpc-support.html) requires private build subnets and NAT for public API access because CodeBuild network interfaces do not receive public IPs. A public subnet and internet gateway alone would not work. One AZ here is an explicit portfolio cost/capacity choice; it is not highly available.

## Integration gates

The root outputs its VPC ID/CIDR, private subnet list, private route table ID, executor security group list, NAT gateway/allocation IDs and public EIP. For the initial design, include that public EIP as an exact `/32` in the runtime EKS API allowlist, along with approved operator host addresses. EKS authentication and namespace RBAC remain mandatory. The executor uses Kubernetes API calls for pods and AWS APIs for snapshots; it needs no direct RDS connection or runtime peering. CodeBuild ENI permissions, input wiring and live endpoint access are still open. Verify DNS/TLS and TCP/443 reachability from the real runner, plus denial from unauthorized sources, before enabling expiry or application writers. See ADR-0010.

Route tables deliberately contain no inline routes; standalone default routes avoid competing ownership if separately reviewed connectivity routes are needed later. The management root must not also own a route that the runtime root manages. Keep management state separate from runtime so it survives cluster/VPC destruction. The staging backend example uses `deepresearch/staging/management.tfstate`; preserve its encrypted versioned state until cleanup and resource inventory succeed. A state key must never be reused for concurrent sessions.

## Cleanup order

1. Provision this network before the VPC-attached executor. Use a reviewed exact plan and the existing lifecycle admission/lease.
2. Configure the EKS API's NAT `/32` allowlist, executor IAM/RBAC and expiry. Prove endpoint reachability and backup prerequisites before starting application writers.
3. The VPC executor backs up data, destroys runtime, publishes its final receipt and exits while its management subnet/NAT remain available.
4. An external runner without a management-VPC attachment verifies the executor build is terminal, its ENIs are gone, and runtime cleanup evidence is complete. It then removes session expiry infrastructure and the management network, waits for NAT deletion, releases the EIP, and verifies remaining resources before closing admission.

The external finalizer and runtime integration are still required. Never make the VPC-attached executor destroy its own subnet, NAT or VPC: it would lose API access, and its active ENI prevents clean VPC deletion. Disposable resources have no `prevent_destroy` flags. That does not authorize deletion; the operational workflow must still enforce exact scope, data preservation and reviewed-plan controls. Failed backup must preserve retained data; management networking may need to remain temporarily available for recovery, with owner notification about ongoing charges.

## Provisional cost

Using example unit rates of `$0.045/NAT-hour` and `$0.005/public-IPv4-hour`, six network hours cost about `$0.30`, before NAT bytes, traffic and provisioning overruns. These are planning assumptions, not verified `us-east-1` rates; refresh the exact regional offer before launch. A NAT/EIP left for 730 hours would add about `$36.50/month` at those assumed rates, before traffic. Removal must be part of session completion. See [AWS VPC pricing](https://aws.amazon.com/vpc/pricing/). The S3 gateway endpoint has no hourly fee; other S3/request charges remain.

## Offline verification

With the pinned provider installed, use backend-disabled initialization, `terraform -chdir=infra/management validate`, and `terraform -chdir=infra/management test`. `tests/network.tftest.hcl` uses a mocked AWS provider and plan-only runs: it checks network separation, private executor placement, HTTPS-only explicit egress, gateway endpoint choice, and rejects malformed session/account/environment or cross-region AZ inputs. Those commands do not establish live routing, regional capacity, effective IAM or billed-resource cleanup. Independent review and deployment-time validation remain necessary.
