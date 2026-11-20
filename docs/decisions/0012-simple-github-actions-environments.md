# ADR-0012: Disposable environments deployed only by GitHub Actions

Status: accepted (2026-11-19). Supersedes ADR-0002 and ADR-0005 through ADR-0011.

## Context

The previous staging design (lifecycle leases, AWS-side expiry executors, a
self-hosted runner in a session management VPC, closed recovery rehearsals and
retained data) never produced a running deployment. The owner asked for a
simple setup: code plus GitHub Actions pipelines that can provision, deploy and
destroy staging and production on demand, with no data retention, and with
production updated only after staging passes.

## Decision

- One Terraform root (`infra/aws`) per environment with fully destroyable
  resources (no `prevent_destroy`, no final snapshots, `force_destroy` buckets).
- A one-time `infra/ci` root creates a GitHub OIDC deploy role per environment.
  Its trust is limited to the matching GitHub environment; it has
  `AdministratorAccess` because Terraform manages IAM, EKS and networking.
- The EKS API endpoint is public (IAM-authenticated) so GitHub-hosted runners
  can deploy; no self-hosted runner or NAT gateway.
- `Deploy` workflow: manual; `staging` or `staging-then-production`. Production
  runs only after the staging job (apply, deploy, smoke tests) succeeds in the
  same run. Each environment builds its images from the same commit.
- `Destroy` workflow: manual, confirmed by retyping the environment name.
- Staging and production use Amazon Bedrock (Nova Lite) instead of private
  vLLM for now. The vLLM chart template remains available behind
  `model.provider=vllm` and `vllm.enabled`.
- Langfuse is self-hosted in-cluster without a public route.

## Consequences

- Much less code and no gates that block deployment; data is lost on destroy.
- The deploy role is broad; protect the GitHub environments (main-only).
- Production images are rebuilt rather than promoted by digest.
- GPU/vLLM benchmarking, backup/restore and expiry automation are out of scope
  until requested again.
