# Workflows

- **Repository / Application / Infrastructure checks** run on every push and
  pull request. The `main` ruleset requires them.
- **Deploy** (manual, `main` only): `staging` provisions or updates staging with
  Terraform, builds and pushes images, installs the Helm charts, points DNS at
  the ALB and runs smoke tests. `staging-then-production` then repeats this for
  production, only if the staging job succeeded in the same run.
- **Destroy** (manual): deletes one environment and all of its data. Type the
  environment name in `confirm`.

Deploy and Destroy share one concurrency group and assume
`deepresearch-<env>-deploy` through GitHub OIDC; the role trusts only the
matching GitHub environment. See `infra/README.md`.
