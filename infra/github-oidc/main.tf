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
  staging_control_table_arn          = "arn:aws:dynamodb:${var.region}:${data.aws_caller_identity.current.account_id}:table/deepresearch-control"
  staging_provisioner_arn            = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/deepresearch-staging-provisioner-github-oidc"
  staging_management_provisioner_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/deepresearch-staging-management-provisioner"
  staging_runtime_provisioner_arn    = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/deepresearch-staging-runtime-provisioner"
  staging_resource_tags = {
    "aws:RequestTag/Project"     = "deepresearch"
    "aws:RequestTag/Environment" = "staging"
    "aws:RequestTag/Session"     = "$${aws:PrincipalTag/Session}"
  }
  staging_session_resource_tags = {
    "aws:ResourceTag/Project"     = "deepresearch"
    "aws:ResourceTag/Environment" = "staging"
    "aws:ResourceTag/Session"     = "$${aws:PrincipalTag/Session}"
  }
}

resource "aws_iam_role" "github_actions" {
  for_each = local.environments

  name                 = "deepresearch-${each.value}-github-oidc"
  description          = each.value == "staging" ? "GitHub Actions OIDC trust for DeepResearchAgent staging deploy and lifecycle lease" : "GitHub Actions OIDC trust for DeepResearchAgent production; no AWS permissions"
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

  role_name = aws_iam_role.github_actions[each.key].name
  policy_names = each.key == "staging" ? [
    aws_iam_role_policy.staging_deploy.name,
    aws_iam_role_policy.staging_lifecycle_lease.name,
    aws_iam_role_policy.staging_deployment_marker.name,
  ] : []

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
    ]
  })

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_iam_role_policy" "staging_lifecycle_lease" {
  name = "deepresearch-staging-lifecycle-lease"
  role = aws_iam_role.github_actions["staging"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadStagingAdmissionAndLifecycleLease"
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

resource "aws_iam_role_policy" "staging_deployment_marker" {
  name = "deepresearch-staging-deployment-marker"
  role = aws_iam_role.github_actions["staging"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadOnlyStagingDeploymentMarkers"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem"]
        Resource = local.staging_control_table_arn
        Condition = {
          "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = ["app-writers#staging#stg-*"] }
          Null                      = { "dynamodb:LeadingKeys" = "false" }
        }
      },
      {
        Sid      = "CheckExactStagingLifecycleLeaseForMarker"
        Effect   = "Allow"
        Action   = ["dynamodb:ConditionCheckItem"]
        Resource = local.staging_control_table_arn
        Condition = {
          "ForAllValues:StringEquals" = {
            "dynamodb:LeadingKeys" = ["lifecycle-lock"]
            "dynamodb:Attributes"  = ["key", "owner_id", "session_id", "expires_at"]
          }
          "ForAnyValue:StringEquals" = { "dynamodb:EnclosingOperation" = ["TransactWriteItems"] }
          Null                       = { "dynamodb:LeadingKeys" = "false" }
        }
      },
      {
        Sid      = "CreateWriteOnceStagingDeploymentMarkers"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = local.staging_control_table_arn
        Condition = {
          "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = ["app-writers#staging#stg-*"] }
          "ForAllValues:StringEquals" = { "dynamodb:Attributes" = [
            "key", "session_id", "environment", "started_at", "commit_sha", "workflow_run_id"
          ] }
          "ForAnyValue:StringEquals" = { "dynamodb:EnclosingOperation" = ["TransactWriteItems"] }
          Null                       = { "dynamodb:LeadingKeys" = "false" }
        }
      },
    ]
  })

  lifecycle {
    prevent_destroy = true
  }
}

# Terraform provisioning is isolated from the application deploy identity. This
# role is assumed only by reviewed workflow_dispatch jobs in the staging
# environment on main; it has no access to retained application objects.
resource "aws_iam_role" "staging_provisioner" {
  name                 = "deepresearch-staging-provisioner-github-oidc"
  description          = "Protected GitHub OIDC identity for session-scoped staging runtime and management Terraform."
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.github_oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = { StringEquals = {
        "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        "token.actions.githubusercontent.com:sub" = "${local.repository_subject}:environment:staging"
        "token.actions.githubusercontent.com:ref" = "refs/heads/main"
      } }
    }]
  })
  lifecycle { prevent_destroy = true }
}

resource "aws_iam_role_policy" "staging_provisioner_assume_session" {
  name = "deepresearch-staging-provisioner-assume-session"
  role = aws_iam_role.staging_provisioner.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["sts:AssumeRole", "sts:TagSession"]
      Resource = [local.staging_management_provisioner_arn, local.staging_runtime_provisioner_arn]
    }]
  })
  lifecycle { prevent_destroy = true }
}

resource "aws_iam_role_policies_exclusive" "staging_provisioner" {
  role_name    = aws_iam_role.staging_provisioner.name
  policy_names = [aws_iam_role_policy.staging_provisioner_assume_session.name]
  lifecycle { prevent_destroy = true }
}

resource "aws_iam_role" "staging_provisioner_session" {
  for_each             = toset(["management", "runtime"])
  name                 = "deepresearch-staging-${each.key}-provisioner"
  description          = "Session-tagged staging ${each.key} Terraform identity."
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = aws_iam_role.staging_provisioner.arn }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
      Condition = {
        StringLike                  = { "aws:RequestTag/Session" = "stg-*" }
        StringEquals                = { "aws:RequestTag/Stack" = each.key }
        "ForAllValues:StringEquals" = { "aws:TagKeys" = ["Session", "Stack"] }
        Null                        = { "aws:RequestTag/Session" = "false", "aws:RequestTag/Stack" = "false" }
      }
    }]
  })
  lifecycle { prevent_destroy = true }
}

locals {
  staging_provisioner_common_statements = [
    {
      Sid       = "ReadStagingMetadata"
      Effect    = "Allow"
      Action    = ["ec2:Describe*", "eks:Describe*", "rds:Describe*", "rds:ListTagsForResource", "iam:GetRole", "iam:GetPolicy", "iam:GetPolicyVersion", "iam:GetRolePolicy", "iam:GetInstanceProfile", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:ListInstanceProfilesForRole", "iam:ListPolicyVersions", "iam:ListRoleTags", "iam:ListInstanceProfileTags", "logs:Describe*", "logs:ListTagsForResource", "sts:GetCallerIdentity"]
      Resource  = "*"
      Condition = { StringEquals = { "aws:RequestedRegion" = var.region } }
    },
    {
      Sid      = "ReadStagingStateBucketLocation"
      Effect   = "Allow"
      Action   = ["s3:GetBucketLocation"]
      Resource = "arn:aws:s3:::${var.staging_state_bucket}"
    },
    {
      Sid      = "VerifyStagingStateBucketVersioning"
      Effect   = "Allow"
      Action   = ["s3:GetBucketVersioning"]
      Resource = "arn:aws:s3:::${var.staging_state_bucket}"
    },
    {
      Sid      = "VerifyPrivateStagingStateBucket"
      Effect   = "Allow"
      Action   = ["s3:GetBucketPublicAccessBlock"]
      Resource = "arn:aws:s3:::${var.staging_state_bucket}"
    },
    {
      Sid      = "ListOnlyStagingTerraformLocksAndPlans"
      Effect   = "Allow"
      Action   = ["s3:ListBucket"]
      Resource = "arn:aws:s3:::${var.staging_state_bucket}"
      Condition = { StringLike = { "s3:prefix" = [
        "deepresearch/staging/$${aws:PrincipalTag/Stack}.tfstate*",
        "deepresearch/staging/management.tfstate",
        "deepresearch/staging/foundation.tfstate",
        "deepresearch/staging/plans/$${aws:PrincipalTag/Session}/*"
      ] } }
    },
    {
      Sid    = "ReadAndWriteStagingState"
      Effect = "Allow"
      Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
      Resource = [
        "arn:aws:s3:::${var.staging_state_bucket}/deepresearch/staging/$${aws:PrincipalTag/Stack}.tfstate",
        "arn:aws:s3:::${var.staging_state_bucket}/deepresearch/staging/$${aws:PrincipalTag/Stack}.tfstate.tflock"
      ]
      Condition = { Bool = { "aws:SecureTransport" = "true" } }
    },
    {
      Sid       = "ReadPrivateStagingPlans"
      Effect    = "Allow"
      Action    = ["s3:GetObject", "s3:GetObjectVersion"]
      Resource  = "arn:aws:s3:::${var.staging_state_bucket}/deepresearch/staging/plans/$${aws:PrincipalTag/Session}/*"
      Condition = { Bool = { "aws:SecureTransport" = "true" } }
    },
    {
      Sid       = "WriteEncryptedPrivateStagingPlans"
      Effect    = "Allow"
      Action    = ["s3:PutObject"]
      Resource  = "arn:aws:s3:::${var.staging_state_bucket}/deepresearch/staging/plans/$${aws:PrincipalTag/Session}/*"
      Condition = { Bool = { "aws:SecureTransport" = "true" }, StringEquals = { "s3:x-amz-server-side-encryption" = "AES256" } }
    },
    {
      Sid       = "ReadStagingAdmissionAndLease"
      Effect    = "Allow"
      Action    = ["dynamodb:GetItem"]
      Resource  = local.staging_control_table_arn
      Condition = { "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = ["active-session", "lifecycle-lock"] }, Null = { "dynamodb:LeadingKeys" = "false" } }
    },
    {
      Sid      = "ReadStagingControlTableMetadata"
      Effect   = "Allow"
      Action   = ["dynamodb:DescribeTable", "dynamodb:ListTagsOfResource"]
      Resource = local.staging_control_table_arn
    },
    {
      Sid       = "MaintainOnlyStagingLifecycleLease"
      Effect    = "Allow"
      Action    = ["dynamodb:UpdateItem"]
      Resource  = local.staging_control_table_arn
      Condition = { "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = ["lifecycle-lock"] }, Null = { "dynamodb:LeadingKeys" = "false" } }
    }
  ]
  staging_runtime_network_statements = [
    {
      Sid       = "ReadOnlyStagingFoundationState"
      Effect    = "Allow"
      Action    = ["s3:GetObject"]
      Resource  = "arn:aws:s3:::${var.staging_state_bucket}/deepresearch/staging/foundation.tfstate"
      Condition = { Bool = { "aws:SecureTransport" = "true" } }
    },
    {
      Sid       = "ReadManagementNetworkOutputForRuntimeAccess"
      Effect    = "Allow"
      Action    = ["s3:GetObject"]
      Resource  = "arn:aws:s3:::${var.staging_state_bucket}/deepresearch/staging/management.tfstate"
      Condition = { Bool = { "aws:SecureTransport" = "true" } }
    },
    {
      Sid       = "CreateSessionTaggedRuntimeVpc"
      Effect    = "Allow"
      Action    = ["ec2:CreateVpc"]
      Resource  = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:vpc/*"
      Condition = { StringEquals = merge(local.staging_resource_tags, { "aws:RequestedRegion" = var.region }) }
    },
    {
      Sid       = "CreateSessionTaggedRuntimeSubnets"
      Effect    = "Allow"
      Action    = ["ec2:CreateSubnet"]
      Resource  = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:subnet/*"
      Condition = { StringEquals = merge(local.staging_resource_tags, { "aws:RequestedRegion" = var.region }) }
    },
    {
      Sid       = "CreateRuntimeSubnetsOnlyInSessionVpc"
      Effect    = "Allow"
      Action    = ["ec2:CreateSubnet"]
      Resource  = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:vpc/*"
      Condition = { StringEquals = merge(local.staging_session_resource_tags, { "aws:RequestedRegion" = var.region }) }
    },
    {
      Sid       = "CreateSessionTaggedRuntimeInternetGateways"
      Effect    = "Allow"
      Action    = ["ec2:CreateInternetGateway"]
      Resource  = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:internet-gateway/*"
      Condition = { StringEquals = merge(local.staging_resource_tags, { "aws:RequestedRegion" = var.region }) }
    },
    {
      Sid       = "CreateSessionTaggedRuntimeRouteTables"
      Effect    = "Allow"
      Action    = ["ec2:CreateRouteTable"]
      Resource  = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:route-table/*"
      Condition = { StringEquals = merge(local.staging_resource_tags, { "aws:RequestedRegion" = var.region }) }
    },
    {
      Sid       = "CreateRuntimeRouteTablesOnlyInSessionVpc"
      Effect    = "Allow"
      Action    = ["ec2:CreateRouteTable"]
      Resource  = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:vpc/*"
      Condition = { StringEquals = merge(local.staging_session_resource_tags, { "aws:RequestedRegion" = var.region }) }
    },
    {
      Sid       = "CreateSessionTaggedRuntimeSecurityGroups"
      Effect    = "Allow"
      Action    = ["ec2:CreateSecurityGroup"]
      Resource  = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:security-group/*"
      Condition = { StringEquals = merge(local.staging_resource_tags, { "aws:RequestedRegion" = var.region }) }
    },
    {
      Sid       = "CreateRuntimeSecurityGroupsOnlyInSessionVpc"
      Effect    = "Allow"
      Action    = ["ec2:CreateSecurityGroup"]
      Resource  = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:vpc/*"
      Condition = { StringEquals = merge(local.staging_session_resource_tags, { "aws:RequestedRegion" = var.region }) }
    },
    {
      Sid       = "TagOnlyNewSessionTaggedRuntimeNetwork"
      Effect    = "Allow"
      Action    = ["ec2:CreateTags"]
      Resource  = "*"
      Condition = { StringEquals = merge(local.staging_resource_tags, { "aws:RequestedRegion" = var.region, "ec2:CreateAction" = ["CreateVpc", "CreateSubnet", "CreateInternetGateway", "CreateRouteTable", "CreateSecurityGroup"] }) }
    },
    {
      Sid       = "ManageSessionTaggedRuntimeNetwork"
      Effect    = "Allow"
      Action    = ["ec2:ModifyVpcAttribute", "ec2:CreateRoute", "ec2:ReplaceRoute", "ec2:DeleteRoute", "ec2:AssociateRouteTable", "ec2:DisassociateRouteTable", "ec2:AttachInternetGateway", "ec2:DetachInternetGateway", "ec2:AuthorizeSecurityGroupIngress", "ec2:RevokeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress", "ec2:RevokeSecurityGroupEgress"]
      Resource  = "*"
      Condition = { StringEquals = merge(local.staging_session_resource_tags, { "aws:RequestedRegion" = var.region }) }
    },
  ]
  staging_runtime_eks_statements = [
    {
      Sid       = "CreateSessionTaggedStagingEKSResources"
      Effect    = "Allow"
      Action    = ["eks:CreateNodegroup", "eks:CreateAddon", "eks:CreateAccessEntry", "eks:CreatePodIdentityAssociation", "eks:TagResource"]
      Resource  = ["arn:aws:eks:${var.region}:${data.aws_caller_identity.current.account_id}:cluster/deepresearch-staging", "arn:aws:eks:${var.region}:${data.aws_caller_identity.current.account_id}:nodegroup/deepresearch-staging/*/*", "arn:aws:eks:${var.region}:${data.aws_caller_identity.current.account_id}:addon/deepresearch-staging/*/*", "arn:aws:eks:${var.region}:${data.aws_caller_identity.current.account_id}:access-entry/deepresearch-staging/*", "arn:aws:eks:${var.region}:${data.aws_caller_identity.current.account_id}:podidentityassociation/deepresearch-staging/*"]
      Condition = { StringEquals = merge(local.staging_resource_tags, { "aws:RequestedRegion" = var.region }) }
    },
    {
      Sid       = "CreateOnlyTaggedStagingEKSCluster"
      Effect    = "Allow"
      Action    = ["eks:CreateCluster"]
      Resource  = "arn:aws:eks:${var.region}:${data.aws_caller_identity.current.account_id}:cluster/deepresearch-staging"
      Condition = { StringEquals = merge(local.staging_resource_tags, { "aws:RequestedRegion" = var.region }) }
    },
    {
      Sid       = "ManageSessionTaggedStagingEKSResources"
      Effect    = "Allow"
      Action    = ["eks:UpdateClusterConfig", "eks:UpdateClusterVersion", "eks:UpdateNodegroupConfig", "eks:UpdateNodegroupVersion", "eks:UpdateAddon", "eks:UpdateAccessEntry", "eks:AssociateAccessPolicy", "eks:DisassociateAccessPolicy", "eks:UpdatePodIdentityAssociation", "eks:ListAssociatedAccessPolicies", "eks:TagResource", "eks:UntagResource"]
      Resource  = ["arn:aws:eks:${var.region}:${data.aws_caller_identity.current.account_id}:cluster/deepresearch-staging", "arn:aws:eks:${var.region}:${data.aws_caller_identity.current.account_id}:nodegroup/deepresearch-staging/*/*", "arn:aws:eks:${var.region}:${data.aws_caller_identity.current.account_id}:addon/deepresearch-staging/*/*", "arn:aws:eks:${var.region}:${data.aws_caller_identity.current.account_id}:access-entry/deepresearch-staging/*", "arn:aws:eks:${var.region}:${data.aws_caller_identity.current.account_id}:podidentityassociation/deepresearch-staging/*"]
      Condition = { StringEquals = merge(local.staging_session_resource_tags, { "aws:RequestedRegion" = var.region }) }
    },
    {
      Sid       = "CreateSessionTaggedStagingDatabase"
      Effect    = "Allow"
      Action    = ["rds:CreateDBInstance", "rds:CreateDBSubnetGroup"]
      Resource  = ["arn:aws:rds:${var.region}:${data.aws_caller_identity.current.account_id}:db:deepresearch-staging", "arn:aws:rds:${var.region}:${data.aws_caller_identity.current.account_id}:subgrp:deepresearch-staging-*"]
      Condition = { StringEquals = merge(local.staging_resource_tags, { "aws:RequestedRegion" = var.region }) }
    },
    {
      Sid       = "ManageSessionTaggedStagingIAM"
      Effect    = "Allow"
      Action    = ["iam:TagRole", "iam:UntagRole", "iam:TagPolicy", "iam:UntagPolicy", "iam:UpdateAssumeRolePolicy", "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:AttachRolePolicy", "iam:DetachRolePolicy"]
      Resource  = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/deepresearch-staging-*", "arn:aws:iam::${data.aws_caller_identity.current.account_id}:policy/deepresearch-staging-*"]
      Condition = { StringEquals = local.staging_session_resource_tags }
    },
    {
      Sid       = "CreateOnlySessionTaggedStagingIAM"
      Effect    = "Allow"
      Action    = ["iam:CreateRole", "iam:CreatePolicy"]
      Resource  = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/deepresearch-staging-*", "arn:aws:iam::${data.aws_caller_identity.current.account_id}:policy/deepresearch-staging-*"]
      Condition = { StringEquals = local.staging_resource_tags }
    },
    {
      Sid       = "PassOnlyStagingServiceRoles"
      Effect    = "Allow"
      Action    = ["iam:PassRole"]
      Resource  = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/deepresearch-staging-*"
      Condition = { StringEquals = merge(local.staging_session_resource_tags, { "iam:PassedToService" = ["eks.amazonaws.com", "ec2.amazonaws.com", "pods.eks.amazonaws.com"] }) }
    },
  ]
  staging_runtime_service_statements = [
    {
      Sid       = "CreateTaggedStagingLogs"
      Effect    = "Allow"
      Action    = ["logs:CreateLogGroup", "logs:TagResource"]
      Resource  = "arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/eks/deepresearch-staging:*"
      Condition = { StringEquals = merge(local.staging_resource_tags, { "aws:RequestedRegion" = var.region }) }
    },
    {
      Sid       = "ManageTaggedStagingLogs"
      Effect    = "Allow"
      Action    = ["logs:PutRetentionPolicy"]
      Resource  = "arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/eks/deepresearch-staging:*"
      Condition = { StringEquals = merge(local.staging_session_resource_tags, { "aws:RequestedRegion" = var.region }) }
    }
  ]
  staging_management_provisioner_statements = [
    {
      Sid       = "CreateSessionTaggedManagementVpc"
      Effect    = "Allow"
      Action    = ["ec2:CreateVpc"]
      Resource  = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:vpc/*"
      Condition = { StringEquals = merge(local.staging_resource_tags, { "aws:RequestedRegion" = var.region }) }
    },
    {
      Sid       = "CreateSessionTaggedManagementSubnet"
      Effect    = "Allow"
      Action    = ["ec2:CreateSubnet"]
      Resource  = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:subnet/*"
      Condition = { StringEquals = merge(local.staging_resource_tags, { "aws:RequestedRegion" = var.region }) }
    },
    {
      Sid       = "CreateManagementSubnetOnlyInSessionVpc"
      Effect    = "Allow"
      Action    = ["ec2:CreateSubnet"]
      Resource  = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:vpc/*"
      Condition = { StringEquals = merge(local.staging_session_resource_tags, { "aws:RequestedRegion" = var.region }) }
    },
    {
      Sid       = "CreateSessionTaggedManagementInternetGateway"
      Effect    = "Allow"
      Action    = ["ec2:CreateInternetGateway"]
      Resource  = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:internet-gateway/*"
      Condition = { StringEquals = merge(local.staging_resource_tags, { "aws:RequestedRegion" = var.region }) }
    },
    {
      Sid       = "CreateSessionTaggedManagementRouteTable"
      Effect    = "Allow"
      Action    = ["ec2:CreateRouteTable"]
      Resource  = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:route-table/*"
      Condition = { StringEquals = merge(local.staging_resource_tags, { "aws:RequestedRegion" = var.region }) }
    },
    {
      Sid       = "CreateManagementRouteTableOnlyInSessionVpc"
      Effect    = "Allow"
      Action    = ["ec2:CreateRouteTable"]
      Resource  = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:vpc/*"
      Condition = { StringEquals = merge(local.staging_session_resource_tags, { "aws:RequestedRegion" = var.region }) }
    },
    {
      Sid       = "CreateSessionTaggedManagementSecurityGroup"
      Effect    = "Allow"
      Action    = ["ec2:CreateSecurityGroup"]
      Resource  = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:security-group/*"
      Condition = { StringEquals = merge(local.staging_resource_tags, { "aws:RequestedRegion" = var.region }) }
    },
    {
      Sid       = "CreateManagementSecurityGroupOnlyInSessionVpc"
      Effect    = "Allow"
      Action    = ["ec2:CreateSecurityGroup"]
      Resource  = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:vpc/*"
      Condition = { StringEquals = merge(local.staging_session_resource_tags, { "aws:RequestedRegion" = var.region }) }
    },
    {
      Sid       = "AllocateSessionTaggedManagementAddress"
      Effect    = "Allow"
      Action    = ["ec2:AllocateAddress"]
      Resource  = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:elastic-ip/*"
      Condition = { StringEquals = merge(local.staging_resource_tags, { "aws:RequestedRegion" = var.region }) }
    },
    {
      Sid       = "CreateSessionTaggedManagementNatGateway"
      Effect    = "Allow"
      Action    = ["ec2:CreateNatGateway"]
      Resource  = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:natgateway/*"
      Condition = { StringEquals = merge(local.staging_resource_tags, { "aws:RequestedRegion" = var.region }) }
    },
    {
      Sid       = "CreateManagementNatOnlyInSessionSubnetAndAddress"
      Effect    = "Allow"
      Action    = ["ec2:CreateNatGateway"]
      Resource  = ["arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:vpc/*", "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:subnet/*", "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:elastic-ip/*"]
      Condition = { StringEquals = merge(local.staging_session_resource_tags, { "aws:RequestedRegion" = var.region }) }
    },
    {
      Sid       = "CreateSessionTaggedManagementEndpoint"
      Effect    = "Allow"
      Action    = ["ec2:CreateVpcEndpoint"]
      Resource  = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:vpc-endpoint/*"
      Condition = { StringEquals = merge(local.staging_resource_tags, { "aws:RequestedRegion" = var.region }) }
    },
    {
      Sid       = "CreateManagementEndpointOnlyOnSessionNetwork"
      Effect    = "Allow"
      Action    = ["ec2:CreateVpcEndpoint"]
      Resource  = ["arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:vpc/*", "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:subnet/*", "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:route-table/*", "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:security-group/*"]
      Condition = { StringEquals = merge(local.staging_session_resource_tags, { "aws:RequestedRegion" = var.region }) }
    },
    {
      Sid       = "TagOnlyNewSessionTaggedManagementNetwork"
      Effect    = "Allow"
      Action    = ["ec2:CreateTags"]
      Resource  = "*"
      Condition = { StringEquals = merge(local.staging_resource_tags, { "aws:RequestedRegion" = var.region, "ec2:CreateAction" = ["CreateVpc", "CreateSubnet", "CreateInternetGateway", "CreateRouteTable", "CreateSecurityGroup", "CreateVpcEndpoint", "AllocateAddress", "CreateNatGateway"] }) }
    },
    {
      Sid       = "ManageSessionTaggedManagementNetwork"
      Effect    = "Allow"
      Action    = ["ec2:DeleteVpc", "ec2:ModifyVpcAttribute", "ec2:DeleteSubnet", "ec2:CreateRoute", "ec2:ReplaceRoute", "ec2:DeleteRoute", "ec2:AssociateRouteTable", "ec2:DisassociateRouteTable", "ec2:AttachInternetGateway", "ec2:DetachInternetGateway", "ec2:DeleteInternetGateway", "ec2:DeleteSecurityGroup", "ec2:AuthorizeSecurityGroupIngress", "ec2:RevokeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress", "ec2:RevokeSecurityGroupEgress", "ec2:DeleteVpcEndpoints", "ec2:ModifyVpcEndpoint", "ec2:DeleteNatGateway", "ec2:ReleaseAddress", "ec2:TerminateInstances", "ec2:DeleteTags"]
      Resource  = "*"
      Condition = { StringEquals = merge(local.staging_session_resource_tags, { "aws:RequestedRegion" = var.region }) }
    },
    {
      Sid      = "AuthorizeOnlyTaggedManagementEgressRules"
      Effect   = "Allow"
      Action   = ["ec2:AuthorizeSecurityGroupEgress"]
      Resource = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:security-group-rule/*"
      Condition = {
        StringEquals = merge(local.staging_resource_tags, {
          "aws:RequestTag/Component" = "management-network"
          "aws:RequestTag/ManagedBy" = "terraform"
          "aws:RequestedRegion"      = var.region
          "ec2:Region"               = var.region
        })
        "ForAllValues:StringEquals" = { "aws:TagKeys" = ["Project", "Environment", "Session", "Component", "ManagedBy"] }
        Null = {
          "aws:RequestTag/Project"     = "false"
          "aws:RequestTag/Environment" = "false"
          "aws:RequestTag/Session"     = "false"
          "aws:RequestTag/Component"   = "false"
          "aws:RequestTag/ManagedBy"   = "false"
        }
      }
    },
    {
      Sid      = "TagOnlyNewManagementEgressRules"
      Effect   = "Allow"
      Action   = ["ec2:CreateTags"]
      Resource = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:security-group-rule/*"
      Condition = {
        StringEquals = merge(local.staging_resource_tags, {
          "aws:RequestTag/Component" = "management-network"
          "aws:RequestTag/ManagedBy" = "terraform"
          "aws:RequestedRegion"      = var.region
          "ec2:CreateAction"         = "AuthorizeSecurityGroupEgress"
          "ec2:Region"               = var.region
        })
        "ForAllValues:StringEquals" = { "aws:TagKeys" = ["Project", "Environment", "Session", "Component", "ManagedBy"] }
        Null = {
          "aws:RequestTag/Project"     = "false"
          "aws:RequestTag/Environment" = "false"
          "aws:RequestTag/Session"     = "false"
          "aws:RequestTag/Component"   = "false"
          "aws:RequestTag/ManagedBy"   = "false"
        }
      }
    },
  ]
  staging_management_runner_statements = [
    {
      Sid      = "UseExactUbuntuRunnerAMI"
      Effect   = "Allow"
      Action   = ["ec2:RunInstances"]
      Resource = "arn:aws:ec2:${var.region}::image/${var.staging_runner_ami_id}"
      Condition = {
        StringEquals = { "aws:RequestedRegion" = var.region }
      }
    },
    {
      Sid       = "RunStagingRunnerOnlyInSessionSubnetAndGroup"
      Effect    = "Allow"
      Action    = ["ec2:RunInstances"]
      Resource  = ["arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:subnet/*", "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:security-group/*"]
      Condition = { StringEquals = merge(local.staging_session_resource_tags, { "aws:RequestedRegion" = var.region }) }
    },
    {
      Sid       = "RunStagingRunnerWithNetworkInterface"
      Effect    = "Allow"
      Action    = ["ec2:RunInstances"]
      Resource  = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:network-interface/*"
      Condition = { StringEquals = { "aws:RequestedRegion" = var.region } }
    },
    {
      Sid       = "RunOnlySessionTaggedStagingRunnerInstance"
      Effect    = "Allow"
      Action    = ["ec2:RunInstances"]
      Resource  = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:instance/*"
      Condition = { StringEquals = merge(local.staging_resource_tags, { "aws:RequestedRegion" = var.region, "ec2:InstanceType" = "m6i.xlarge" }) }
    },
    {
      Sid       = "RunOnlyEncryptedStagingRunnerVolume"
      Effect    = "Allow"
      Action    = ["ec2:RunInstances"]
      Resource  = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:volume/*"
      Condition = { Bool = { "ec2:Encrypted" = "true" }, StringEquals = { "aws:RequestedRegion" = var.region } }
    },
    {
      Sid      = "SetOnlyTheSessionRunnerShutdownBehavior"
      Effect   = "Allow"
      Action   = ["ec2:ModifyInstanceAttribute"]
      Resource = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:instance/*"
      Condition = {
        StringEquals = merge(local.staging_session_resource_tags, {
          "aws:ResourceTag/Component"                       = "github-actions-runner"
          "aws:RequestedRegion"                             = var.region
          "ec2:Region"                                      = var.region
          "ec2:Attribute/instanceInitiatedShutdownBehavior" = "terminate"
        })
      }
    },
    {
      Sid       = "TagOnlyTheSessionRunnerAtLaunch"
      Effect    = "Allow"
      Action    = ["ec2:CreateTags"]
      Resource  = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:instance/*"
      Condition = { StringEquals = merge(local.staging_resource_tags, { "aws:RequestedRegion" = var.region, "ec2:CreateAction" = "RunInstances" }) }
    },
    {
      Sid      = "TagOnlyTheManagementSessionRunnerVolumeAtLaunch"
      Effect   = "Allow"
      Action   = ["ec2:CreateTags"]
      Resource = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:volume/*"
      Condition = {
        StringEquals = merge(local.staging_resource_tags, {
          "aws:RequestTag/Component" = "management-network"
          "aws:RequestTag/ManagedBy" = "terraform"
          "aws:RequestedRegion"      = var.region
          "ec2:CreateAction"         = "RunInstances"
          "ec2:Region"               = var.region
        })
        "ForAllValues:StringEquals" = { "aws:TagKeys" = ["Project", "Environment", "Session", "Component", "ManagedBy"] }
        Null = {
          "aws:RequestTag/Project"     = "false"
          "aws:RequestTag/Environment" = "false"
          "aws:RequestTag/Session"     = "false"
          "aws:RequestTag/Component"   = "false"
          "aws:RequestTag/ManagedBy"   = "false"
        }
      }
    },
    {
      Sid       = "ManageSessionTaggedRunnerIAM"
      Effect    = "Allow"
      Action    = ["iam:DeleteRole", "iam:TagRole", "iam:UntagRole", "iam:UpdateAssumeRolePolicy", "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:DeleteInstanceProfile", "iam:AddRoleToInstanceProfile", "iam:RemoveRoleFromInstanceProfile", "iam:TagInstanceProfile", "iam:UntagInstanceProfile"]
      Resource  = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/deepresearch-staging-*", "arn:aws:iam::${data.aws_caller_identity.current.account_id}:instance-profile/deepresearch-staging-*"]
      Condition = { StringEquals = local.staging_session_resource_tags }
    },
    {
      Sid       = "CreateOnlySessionTaggedRunnerIAM"
      Effect    = "Allow"
      Action    = ["iam:CreateRole", "iam:CreateInstanceProfile"]
      Resource  = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/deepresearch-staging-*", "arn:aws:iam::${data.aws_caller_identity.current.account_id}:instance-profile/deepresearch-staging-*"]
      Condition = { StringEquals = local.staging_resource_tags }
    },
    {
      Sid       = "PassOnlyStagingRunnerRoleToEC2"
      Effect    = "Allow"
      Action    = ["iam:PassRole"]
      Resource  = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/deepresearch-staging-management-$${aws:PrincipalTag/Session}-runner"
      Condition = { StringEquals = { "iam:PassedToService" = "ec2.amazonaws.com" } }
    },
    {
      Sid       = "CreateSessionTaggedRunnerTokenKey"
      Effect    = "Allow"
      Action    = ["kms:CreateKey"]
      Resource  = "*"
      Condition = { StringEquals = merge(local.staging_resource_tags, { "aws:RequestedRegion" = var.region, "aws:RequestTag/Component" = "github-runner-token-key" }) }
    },
    {
      Sid       = "ManageSessionTaggedRunnerTokenKey"
      Effect    = "Allow"
      Action    = ["kms:EnableKeyRotation", "kms:GetKeyRotationStatus", "kms:GetKeyPolicy", "kms:PutKeyPolicy", "kms:ScheduleKeyDeletion", "kms:CancelKeyDeletion", "kms:TagResource", "kms:UntagResource", "kms:DescribeKey", "kms:ListResourceTags", "kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
      Resource  = "*"
      Condition = { StringEquals = merge(local.staging_session_resource_tags, { "aws:RequestedRegion" = var.region, "aws:ResourceTag/Component" = "github-runner-token-key" }) }
    },
    {
      Sid      = "ManageOnlyStagingRunnerTokenParameter"
      Effect   = "Allow"
      Action   = ["ssm:PutParameter", "ssm:GetParameter", "ssm:DeleteParameter"]
      Resource = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/deepresearch/runner/$${aws:PrincipalTag/Session}/registration"
    }
  ]
  staging_provisioner_policy_documents = {
    common               = jsonencode({ Version = "2012-10-17", Statement = local.staging_provisioner_common_statements })
    "management-network" = jsonencode({ Version = "2012-10-17", Statement = slice(local.staging_management_provisioner_statements, 0, 8) })
    "management-egress"  = jsonencode({ Version = "2012-10-17", Statement = slice(local.staging_management_provisioner_statements, 8, length(local.staging_management_provisioner_statements)) })
    "management-runner"  = jsonencode({ Version = "2012-10-17", Statement = local.staging_management_runner_statements })
    "runtime-network"    = jsonencode({ Version = "2012-10-17", Statement = local.staging_runtime_network_statements })
    "runtime-eks"        = jsonencode({ Version = "2012-10-17", Statement = local.staging_runtime_eks_statements })
    "runtime-service"    = jsonencode({ Version = "2012-10-17", Statement = local.staging_runtime_service_statements })
  }
}

resource "aws_iam_policy" "staging_provisioner" {
  for_each = local.staging_provisioner_policy_documents
  name     = "deepresearch-staging-provisioner-${each.key}"
  policy   = each.value
  tags     = { Project = "deepresearch", Environment = "staging", Component = "staging-provisioner-policy" }
  lifecycle { prevent_destroy = true }
}

resource "aws_iam_role_policy_attachment" "staging_provisioner" {
  for_each = {
    "management-common"  = { stack = "management", policy = aws_iam_policy.staging_provisioner["common"].arn }
    "management-network" = { stack = "management", policy = aws_iam_policy.staging_provisioner["management-network"].arn }
    "management-egress"  = { stack = "management", policy = aws_iam_policy.staging_provisioner["management-egress"].arn }
    "management-runner"  = { stack = "management", policy = aws_iam_policy.staging_provisioner["management-runner"].arn }
    "runtime-common"     = { stack = "runtime", policy = aws_iam_policy.staging_provisioner["common"].arn }
    "runtime-network"    = { stack = "runtime", policy = aws_iam_policy.staging_provisioner["runtime-network"].arn }
    "runtime-eks"        = { stack = "runtime", policy = aws_iam_policy.staging_provisioner["runtime-eks"].arn }
    "runtime-service"    = { stack = "runtime", policy = aws_iam_policy.staging_provisioner["runtime-service"].arn }
  }
  role       = aws_iam_role.staging_provisioner_session[each.value.stack].name
  policy_arn = each.value.policy
}

resource "aws_iam_role_policy_attachments_exclusive" "staging_provisioner_session" {
  for_each  = toset(["management", "runtime"])
  role_name = aws_iam_role.staging_provisioner_session[each.key].name
  policy_arns = each.key == "management" ? [
    aws_iam_policy.staging_provisioner["common"].arn,
    aws_iam_policy.staging_provisioner["management-network"].arn,
    aws_iam_policy.staging_provisioner["management-egress"].arn,
    aws_iam_policy.staging_provisioner["management-runner"].arn
    ] : [
    aws_iam_policy.staging_provisioner["common"].arn,
    aws_iam_policy.staging_provisioner["runtime-network"].arn,
    aws_iam_policy.staging_provisioner["runtime-eks"].arn,
    aws_iam_policy.staging_provisioner["runtime-service"].arn
  ]
  lifecycle { prevent_destroy = true }
}

resource "aws_iam_role_policies_exclusive" "staging_provisioner_session" {
  for_each     = toset(["management", "runtime"])
  role_name    = aws_iam_role.staging_provisioner_session[each.key].name
  policy_names = []
  lifecycle { prevent_destroy = true }
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
  description = "GitHub OIDC trust role ARNs. The staging role has only the reviewed deployment and lease policy; production has no permissions."
  value       = { for environment, role in aws_iam_role.github_actions : environment => role.arn }
}

output "staging_provisioner_role_arn" {
  description = "Session-tagged chained roles for protected staging Terraform workflows, keyed by stack."
  value       = { for stack, role in aws_iam_role.staging_provisioner_session : stack => role.arn }
}

output "staging_provisioner_bootstrap_role_arn" {
  description = "OIDC-only role that can only assume the session-tagged staging provisioner role."
  value       = aws_iam_role.staging_provisioner.arn
}
