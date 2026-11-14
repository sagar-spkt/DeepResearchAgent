# Staged lifecycle workflows

Copy these files into .github/workflows only after integrating scripts and reviewing the protection model. They currently fail closed at preflight: they do not provision, back up, destroy or restore AWS. A workflow named Prepare deploy is not a working deployment pipeline.

Before implementing apply jobs, bootstrap a GitHub OIDC provider and environment-specific IAM roles with aud=sts.amazonaws.com and sub=repo:OWNER/REPO:environment:staging or production. Pin configure-aws-credentials by reviewed commit and request id-token:write only on those jobs. Keep production environment human approval enabled. Never use pull_request_target for code execution or expose cloud secrets to fork PRs.

Use one global concurrency group for AWS lifecycle and hold an AWS-side session lock as well (GitHub concurrency alone does not cover console/external changes). Build images once, publish by digest, attest source and staging evidence, and promote the same digests. Validate release manifests; schema checks are not provenance verification. Infrastructure plans can contain secrets and must not be public artifacts/logs.

Implement AWS-side four-hour expiry before permitting deploy, including backup/drain/failure notification and billed-resource inventory. Read infra/README.md for retained/runtime ownership and restoration blockers. Purge requires a separate explicit approval and exact environment inventory, not an input interpolated into shell commands. Current scripts use fixed argparse choices and never eval inputs.
