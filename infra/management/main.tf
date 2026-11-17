terraform {
  required_version = ">= 1.10, < 2.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "= 6.20.0" }
  }
  backend "s3" {}
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
  default_tags {
    tags = {
      Project   = "deepresearch", Environment = var.environment, Session = var.session_id
      Component = "management-network", ManagedBy = "terraform"
    }
  }
}

variable "account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Specify the intended twelve-digit AWS account."
  }
}
variable "region" {
  type    = string
  default = "us-east-1"
  validation {
    condition     = var.region == "us-east-1"
    error_message = "The reviewed network currently supports us-east-1 only."
  }
}
variable "environment" {
  type = string
  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "Only staging/production."
  }
}
variable "session_id" {
  type = string
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,15}$", var.session_id))
    error_message = "Session ID must contain at most 16 safe characters."
  }
}
variable "availability_zone" {
  type = string
  validation {
    condition     = can(regex("^${var.region}[a-z]$", var.availability_zone))
    error_message = "Choose a standard Availability Zone in the selected region."
  }
}

variable "enable_github_runner" {
  type        = bool
  default     = false
  description = "Create the session-scoped token KMS key and allow a runner launch."
}
variable "launch_github_runner" {
  type        = bool
  default     = false
  description = "Launch the one-job EC2 runner after its token parameter is staged."
  validation {
    condition     = !var.launch_github_runner || var.enable_github_runner
    error_message = "Enable the runner key before launching the EC2 runner."
  }
}
variable "github_repository" {
  type        = string
  default     = ""
  description = "Exact owner/repository for the ephemeral deployment runner."
  validation {
    condition     = var.github_repository == "" || can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "Use the exact owner/repository name."
  }
}
variable "github_runner_ami_id" {
  type        = string
  default     = ""
  description = "Reviewed immutable x86_64 Linux AMI ID with Docker, AWS CLI, git, curl, tar, sha256sum, systemd and Python 3 installed."
  validation {
    condition     = var.github_runner_ami_id == "" || can(regex("^ami-[0-9a-f]{8,17}$", var.github_runner_ami_id))
    error_message = "Provide an immutable AMI ID."
  }
}
variable "github_runner_registration_parameter" {
  type        = string
  default     = ""
  description = "Pre-created one-time SSM SecureString parameter containing a repo-scoped registration token. Never pass the token to Terraform."
  validation {
    condition     = var.github_runner_registration_parameter == "" || can(regex("^/deepresearch/runner/[a-z0-9-]+/[a-z0-9-]+$", var.github_runner_registration_parameter))
    error_message = "Use a session-scoped parameter under /deepresearch/runner."
  }
}
variable "github_runner_version" {
  type    = string
  default = "2.338.2"
  validation {
    condition     = var.github_runner_version == "2.338.2"
    error_message = "Runner version is pinned to the reviewed release."
  }
}
variable "github_runner_sha256" {
  type    = string
  default = "baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40"
  validation {
    condition     = var.github_runner_sha256 == "baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40"
    error_message = "Runner checksum is pinned to the reviewed Linux x64 archive."
  }
}

locals { name = "deepresearch-${var.environment}-management-${var.session_id}" }

resource "aws_vpc" "management" {
  cidr_block           = "10.70.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
}
resource "aws_internet_gateway" "management" { vpc_id = aws_vpc.management.id }
resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.management.id
  availability_zone       = var.availability_zone
  cidr_block              = "10.70.0.0/24"
  map_public_ip_on_launch = false
}
resource "aws_subnet" "executor" {
  vpc_id                  = aws_vpc.management.id
  availability_zone       = var.availability_zone
  cidr_block              = "10.70.1.0/24"
  map_public_ip_on_launch = false
}
resource "aws_route_table" "public" { vpc_id = aws_vpc.management.id }
resource "aws_route" "internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.management.id
}
resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}
resource "aws_eip" "nat" { domain = "vpc" }
resource "aws_nat_gateway" "management" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public.id
  depends_on    = [aws_internet_gateway.management, aws_route.internet, aws_route_table_association.public]
}
resource "aws_route_table" "executor" { vpc_id = aws_vpc.management.id }
resource "aws_route" "executor_egress" {
  route_table_id         = aws_route_table.executor.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.management.id
}
resource "aws_route_table_association" "executor" {
  subnet_id      = aws_subnet.executor.id
  route_table_id = aws_route_table.executor.id
}
resource "aws_security_group" "executor" {
  name        = local.name
  description = "No inbound; HTTPS egress for the session operations runner."
  vpc_id      = aws_vpc.management.id
}
resource "aws_vpc_security_group_egress_rule" "https" {
  security_group_id = aws_security_group.executor.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.management.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.executor.id]
}

locals {
  runner_enabled          = var.enable_github_runner
  runner_instance_enabled = var.enable_github_runner && var.launch_github_runner
  runner_parameter_arn    = "arn:aws:ssm:${var.region}:${var.account_id}:parameter${var.github_runner_registration_parameter}"
  runner_tags = {
    Project   = "deepresearch", Environment = var.environment, Session = var.session_id,
    Component = "github-actions-runner", ManagedBy = "terraform"
  }
  runner_assume_policy = {
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  }
  runner_policy_document = local.runner_instance_enabled ? {
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:DeleteParameter"]
        Resource = local.runner_parameter_arn
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = aws_kms_key.runner_token[0].arn
        Condition = {
          StringEquals = {
            "kms:ViaService"                      = "ssm.${var.region}.amazonaws.com"
            "kms:EncryptionContext:PARAMETER_ARN" = local.runner_parameter_arn
          }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["ec2:TerminateInstances"]
        Resource = "arn:aws:ec2:${var.region}:${var.account_id}:instance/*"
        Condition = {
          StringEquals = {
            "ec2:ResourceTag/Project"     = "deepresearch"
            "ec2:ResourceTag/Component"   = "github-actions-runner"
            "ec2:ResourceTag/Session"     = var.session_id
            "ec2:ResourceTag/Environment" = var.environment
          }
        }
      }
    ]
  } : null
}

resource "aws_kms_key" "runner_token" {
  count                   = local.runner_enabled ? 1 : 0
  description             = "One-time GitHub runner registration token for ${var.session_id}"
  deletion_window_in_days = 7
  enable_key_rotation     = true
  tags = {
    Component = "github-runner-token-key"
  }
}

resource "aws_iam_role" "runner" {
  count              = local.runner_instance_enabled ? 1 : 0
  name               = "${local.name}-runner"
  assume_role_policy = jsonencode(local.runner_assume_policy)
  tags               = local.runner_tags
}
resource "aws_iam_role_policy" "runner" {
  count  = local.runner_instance_enabled ? 1 : 0
  role   = aws_iam_role.runner[0].id
  policy = jsonencode(local.runner_policy_document)
}
resource "aws_iam_instance_profile" "runner" {
  count = local.runner_instance_enabled ? 1 : 0
  name  = "${local.name}-runner"
  role  = aws_iam_role.runner[0].name
}

resource "aws_instance" "runner" {
  count                                = local.runner_instance_enabled ? 1 : 0
  ami                                  = var.github_runner_ami_id
  instance_type                        = "m6i.xlarge"
  subnet_id                            = aws_subnet.executor.id
  vpc_security_group_ids               = [aws_security_group.executor.id]
  iam_instance_profile                 = aws_iam_instance_profile.runner[0].name
  associate_public_ip_address          = false
  instance_initiated_shutdown_behavior = "terminate"
  user_data_replace_on_change          = true
  user_data                            = local.runner_enabled ? templatefile("${path.module}/runner-user-data.sh.tftpl", { version = var.github_runner_version, sha256 = var.github_runner_sha256, repository = var.github_repository, parameter_name = var.github_runner_registration_parameter, session_id = var.session_id }) : ""
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  root_block_device {
    encrypted             = true
    volume_type           = "gp3"
    volume_size           = 40
    delete_on_termination = true
  }
  tags       = local.runner_tags
  depends_on = [aws_iam_role_policy.runner]
  lifecycle {
    precondition {
      condition     = !local.runner_instance_enabled || (var.github_runner_ami_id != "" && var.github_repository != "" && var.github_runner_registration_parameter != "")
      error_message = "Launching the runner requires a pinned AMI, exact repository, and pre-created token parameter name."
    }
    precondition {
      condition     = !local.runner_instance_enabled || var.github_runner_registration_parameter == "/deepresearch/runner/${var.session_id}/registration"
      error_message = "Runner token parameter must match the current session-scoped name."
    }
  }
}

output "github_runner_instance_id" {
  value       = local.runner_instance_enabled ? try(aws_instance.runner[0].id, null) : null
  description = "Ephemeral runner instance ID, if enabled."
}
output "github_runner_token_kms_key_arn" {
  value       = try(aws_kms_key.runner_token[0].arn, null)
  description = "KMS key ARN required to securely stage the one-time runner token."
}

output "vpc_id" { value = aws_vpc.management.id }
output "vpc_cidr" { value = aws_vpc.management.cidr_block }
output "private_subnet_ids" { value = [aws_subnet.executor.id] }
output "private_route_table_id" { value = aws_route_table.executor.id }
output "security_group_ids" { value = [aws_security_group.executor.id] }
output "nat_public_ip" { value = aws_eip.nat.public_ip }
output "nat_gateway_id" { value = aws_nat_gateway.management.id }
output "nat_allocation_id" { value = aws_eip.nat.id }
