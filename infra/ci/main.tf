# One-time bootstrap, applied by an AWS administrator from a workstation:
#   terraform -chdir=infra/ci init -backend-config="bucket=<state bucket>"
#   terraform -chdir=infra/ci apply
# It creates one GitHub Actions deploy role per environment. Each role can only
# be assumed by a job running in the matching protected GitHub environment.

terraform {
  required_version = ">= 1.10, < 2.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "= 6.20.0" }
  }
  backend "s3" {
    key          = "deepresearch/shared/ci.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "us-east-1"
  default_tags {
    tags = { Project = "deepresearch", ManagedBy = "terraform" }
  }
}

# This repository uses GitHub's immutable OIDC subject format
# (owner@owner_id/repo@repo_id), see
# GET repos/{owner}/{repo}/actions/oidc/customization/sub.
variable "github_subject_prefix" {
  type    = string
  default = "repo:sagar-spkt@22609549/DeepResearchAgent@1457879530"
}

variable "environments" {
  type    = set(string)
  default = ["staging", "production"]
}

data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

data "aws_iam_policy_document" "trust" {
  for_each = var.environments
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["${var.github_subject_prefix}:environment:${each.key}"]
    }
  }
}

resource "aws_iam_role" "deploy" {
  for_each             = var.environments
  name                 = "deepresearch-${each.key}-deploy"
  description          = "GitHub Actions provisioning, deployment and teardown for ${each.key}."
  assume_role_policy   = data.aws_iam_policy_document.trust[each.key].json
  max_session_duration = 7200
}

# Terraform creates and destroys the whole environment (VPC, EKS, IAM roles,
# RDS, Cognito, ...), so the role needs broad rights. Access is limited by the
# trust policy to the protected GitHub environment.
resource "aws_iam_role_policy_attachment" "deploy" {
  for_each   = var.environments
  role       = aws_iam_role.deploy[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

output "deploy_role_arns" {
  value = { for k, v in aws_iam_role.deploy : k => v.arn }
}
