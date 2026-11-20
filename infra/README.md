# AWS environments

Staging and production are each one disposable Terraform root (`infra/aws`)
plus three Helm releases, created and torn down only by GitHub Actions. See
[ADR-0012](../docs/decisions/0012-simple-github-actions-environments.md).

| Path | Purpose |
| --- | --- |
| `infra/ci` | One-time bootstrap: GitHub OIDC deploy roles `deepresearch-<env>-deploy`. |
| `infra/aws` | Everything for one environment: VPC, EKS, RDS, S3, SQS, ECR, Cognito, ACM, IAM, generated credentials. |
| `infra/aws/env/<env>.tfvars` | Per-environment hostname and settings. |
| `scripts/deploy.sh` | Terraform apply, image build/push, Helm installs, DNS, smoke tests. |
| `scripts/destroy.sh` | Removes Helm releases (ALB, EBS volumes), DNS record, then `terraform destroy`. |

State lives in the existing versioned bucket `ra-088759500358-terraform-state`
under `deepresearch/<env>/aws.tfstate`. Generated passwords exist only in that
encrypted state and in Kubernetes Secrets.

## One-time setup

1. Apply the CI bootstrap as an AWS administrator:

   ```sh
   terraform -chdir=infra/ci init -backend-config="bucket=ra-088759500358-terraform-state"
   terraform -chdir=infra/ci apply
   ```

   Each role trusts only `repo:sagar-spkt/DeepResearchAgent:environment:<env>`
   and has `AdministratorAccess`, because Terraform creates and deletes IAM
   roles, EKS, RDS and networking.
2. In the GitHub `staging` and `production` environments set variable
   `BOOTSTRAP_OWNER_EMAIL` (the first owner account) and, for web search,
   secret `TAVILY_API_KEY`. Keep both environments restricted to `main`.

## Operations

- **Deploy**: run the *Deploy* workflow on `main`. `staging` provisions or
  updates staging and runs smoke tests. `staging-then-production` does the same
  and then deploys production only if staging succeeded in that run.
- **Destroy**: run the *Destroy* workflow, choose the environment and type its
  name again. All of its data is deleted; nothing is retained.
- **Debug**: users listed in `admin_principal_arns` can run
  `aws eks update-kubeconfig --name deepresearch-<env>`. Langfuse has no public
  route: `kubectl -n research port-forward svc/langfuse 3000:3000`. The Langfuse
  admin password is `LANGFUSE_INIT_USER_PASSWORD` in the
  `research-observability` Secret.

## Cost

While an environment exists it runs one EKS control plane, one `m6i.xlarge`
node, one `db.t4g.small` RDS instance and one ALB (roughly $0.35/hour in
`us-east-1`, before data transfer and Bedrock usage). Destroy environments you
are not using.
