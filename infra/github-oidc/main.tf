terraform {
  required_version = ">= 1.10, < 2.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.20.0"
    }
  }

  backend "s3" {}
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
}

data "aws_caller_identity" "current" {}

locals {
  github_oidc_provider_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"
  # Keep the OIDC identity pinned to this repository; changing it requires a reviewed source change.
  repository_subject = "repo:sagar-spkt@22609549/DeepResearchAgent@1457879530"
  environments       = toset(["staging", "production"])
  staging_repository_arns = [
    "arn:aws:ecr:${var.region}:${data.aws_caller_identity.current.account_id}:repository/deepresearch-staging/backend",
    "arn:aws:ecr:${var.region}:${data.aws_caller_identity.current.account_id}:repository/deepresearch-staging/web",
  ]
  staging_control_table_arn = "arn:aws:dynamodb:${var.region}:${data.aws_caller_identity.current.account_id}:table/deepresearch-control"
}

resource "aws_iam_role" "github_actions" {
  for_each = local.environments

  name                 = "deepresearch-${each.value}-github-oidc"
  description          = "GitHub Actions OIDC trust for DeepResearchAgent ${each.value}; permissions intentionally unset"
  max_session_duration = 3600

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = local.github_oidc_provider_arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = "${local.repository_subject}:environment:${each.value}"
          "token.actions.githubusercontent.com:ref" = "refs/heads/main"
        }
      }
    }]
  })

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_iam_role_policy_attachments_exclusive" "github_actions" {
  for_each = local.environments

  role_name   = aws_iam_role.github_actions[each.key].name
  policy_arns = []

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_iam_role_policies_exclusive" "github_actions" {
  for_each = local.environments

  role_name    = aws_iam_role.github_actions[each.key].name
  policy_names = each.key == "staging" ? [aws_iam_role_policy.staging_deploy.name] : []

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_iam_role_policy" "staging_deploy" {
  name = "deepresearch-staging-deploy"
  role = aws_iam_role.github_actions["staging"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AuthenticateToECR"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Sid    = "PublishOnlyStagingApplicationImages"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:CompleteLayerUpload",
          "ecr:InitiateLayerUpload",
          "ecr:PutImage",
          "ecr:UploadLayerPart",
        ]
        Resource = local.staging_repository_arns
      },
      {
        Sid      = "DescribeOnlyStagingCluster"
        Effect   = "Allow"
        Action   = ["eks:DescribeCluster"]
        Resource = "arn:aws:eks:${var.region}:${data.aws_caller_identity.current.account_id}:cluster/deepresearch-staging"
      },
      {
        Sid    = "ReadOnlyStagingRuntimeSecrets"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = [
          "arn:aws:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:deepresearch-staging/application-*",
          "arn:aws:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:deepresearch-staging/vllm-*",
          "arn:aws:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:deepresearch-staging/web-*",
          var.staging_rds_master_secret_arn,
        ]
      },
      {
        Sid      = "VerifyStagingIngressLoadBalancer"
        Effect   = "Allow"
        Action   = ["elasticloadbalancing:DescribeLoadBalancers", "elasticloadbalancing:DescribeTags"]
        Resource = "*"
      },
      {
        Sid      = "ReadStagingDatabaseEndpoint"
        Effect   = "Allow"
        Action   = ["rds:DescribeDBInstances"]
        Resource = "*"
      },
      {
        Sid      = "UpdateOnlyTheStagingARecord"
        Effect   = "Allow"
        Action   = ["route53:ChangeResourceRecordSets"]
        Resource = "arn:aws:route53:::hostedzone/Z07904421IUDAJDY0WXZ8"
        Condition = {
          "ForAllValues:StringEquals" = {
            "route53:ChangeResourceRecordSetsNormalizedRecordNames" = ["staging.research.spktsagar.com"]
            "route53:ChangeResourceRecordSetsRecordTypes"           = ["A"]
            "route53:ChangeResourceRecordSetsActions"               = ["UPSERT"]
          }
        }
      },
      {
        Sid      = "WaitForRoute53ChangeToPropagate"
        Effect   = "Allow"
        Action   = ["route53:GetChange"]
        Resource = "arn:aws:route53:::change/*"
      },
      {
        Sid      = "ReadActiveStagingAdmissionAndLease"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem"]
        Resource = local.staging_control_table_arn
        Condition = {
          "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = ["active-session", "lifecycle-lock"] }
          Null                        = { "dynamodb:LeadingKeys" = "false" }
        }
      },
      {
        Sid      = "MaintainOnlyStagingLifecycleLease"
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem"]
        Resource = local.staging_control_table_arn
        Condition = {
          "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = ["lifecycle-lock"] }
          Null                        = { "dynamodb:LeadingKeys" = "false" }
        }
      },
    ]
  })

  lifecycle {
    prevent_destroy = true
  }
}

import {
  for_each = local.environments

  to = aws_iam_role.github_actions[each.key]
  id = "deepresearch-${each.key}-github-oidc"
}

import {
  for_each = local.environments

  to = aws_iam_role_policy_attachments_exclusive.github_actions[each.key]
  id = "deepresearch-${each.key}-github-oidc"
}

import {
  for_each = local.environments

  to = aws_iam_role_policies_exclusive.github_actions[each.key]
  id = "deepresearch-${each.key}-github-oidc"
}

output "role_arns" {
  description = "GitHub OIDC trust roles, with no AWS permissions attached."
  value       = { for environment, role in aws_iam_role.github_actions : environment => role.arn }
}
