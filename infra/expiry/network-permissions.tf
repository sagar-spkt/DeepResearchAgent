# Network authorization is separate from backup/destruction permissions.
locals {
  network_arn_prefix           = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}"
  executor_subnet_arns         = [for id in var.management_subnet_ids : "${local.network_arn_prefix}:subnet/${id}"]
  executor_security_group_arns = [for id in var.management_security_group_ids : "${local.network_arn_prefix}:security-group/${id}"]
  management_vpc_arn           = "${local.network_arn_prefix}:vpc/${var.management_vpc_id}"
}

resource "aws_iam_role_policy" "executor_network" {
  role = aws_iam_role.executor.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DescribeNetworkMetadata"
        Effect    = "Allow"
        Action    = ["ec2:DescribeDhcpOptions", "ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups", "ec2:DescribeVpcs"]
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = var.region } }
      },
      {
        Sid    = "CreateInterfaceInReviewedNetwork"
        Effect = "Allow"
        Action = ["ec2:CreateNetworkInterface"]
        Resource = concat(
          ["${local.network_arn_prefix}:network-interface/*"],
          local.executor_subnet_arns,
          local.executor_security_group_arns,
        )
      },
      {
        Sid      = "DeleteDetachedManagementInterfaces"
        Effect   = "Allow"
        Action   = ["ec2:DeleteNetworkInterface"]
        Resource = "${local.network_arn_prefix}:network-interface/*"
        Condition = { ArnEquals = {
          "ec2:Subnet" = local.executor_subnet_arns
          "ec2:Vpc"    = local.management_vpc_arn
        } }
      },
      {
        Sid      = "PermitOnlyCodeBuildInReviewedSubnets"
        Effect   = "Allow"
        Action   = ["ec2:CreateNetworkInterfacePermission"]
        Resource = "${local.network_arn_prefix}:network-interface/*"
        Condition = {
          StringEquals = { "ec2:AuthorizedService" = "codebuild.amazonaws.com" }
          ArnEquals = {
            "ec2:Subnet" = local.executor_subnet_arns
            "ec2:Vpc"    = local.management_vpc_arn
          }
        }
      },
    ]
  })

  lifecycle {
    precondition {
      condition     = var.region == "us-east-1"
      error_message = "The reviewed executor network policy currently supports us-east-1 only."
    }
    precondition {
      condition = (
        can(regex("^vpc-([0-9a-f]{8}|[0-9a-f]{17})$", var.management_vpc_id)) &&
        can(regex("^vpc-([0-9a-f]{8}|[0-9a-f]{17})$", var.runtime_vpc_id)) &&
        var.management_vpc_id != var.runtime_vpc_id
      )
      error_message = "Management and runtime must be distinct, valid VPC IDs."
    }
    precondition {
      condition = (
        length(var.management_subnet_ids) > 0 &&
        length(distinct(var.management_subnet_ids)) == length(var.management_subnet_ids) &&
        alltrue([for id in var.management_subnet_ids : can(regex("^subnet-([0-9a-f]{8}|[0-9a-f]{17})$", id))]) &&
        length(var.management_security_group_ids) > 0 &&
        length(distinct(var.management_security_group_ids)) == length(var.management_security_group_ids) &&
        alltrue([for id in var.management_security_group_ids : can(regex("^sg-([0-9a-f]{8}|[0-9a-f]{17})$", id))])
      )
      error_message = "Supply nonempty distinct exact management subnet and security group IDs."
    }
  }
}
