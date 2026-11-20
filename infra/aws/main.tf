# One root per environment. Everything here is disposable: `terraform destroy`
# removes the whole environment including its data.
#   terraform -chdir=infra/aws init -backend-config="bucket=<state bucket>" \
#     -backend-config="key=deepresearch/<env>/aws.tfstate"
#   terraform -chdir=infra/aws apply -var-file=env/<env>.tfvars

terraform {
  required_version = ">= 1.10, < 2.0"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "= 6.20.0" }
    random = { source = "hashicorp/random", version = "= 3.7.2" }
  }
  backend "s3" {
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "deepresearch", Environment = var.environment, ManagedBy = "terraform" }
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "environment" {
  type = string
  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment must be staging or production."
  }
}

variable "app_host" {
  type        = string
  description = "Public HTTPS hostname of the application, inside the Route 53 zone."
}

variable "route53_zone_name" {
  type = string
}

variable "availability_zones" {
  type    = list(string)
  default = ["us-east-1a", "us-east-1b"]
}

variable "kubernetes_version" {
  type    = string
  default = "1.37"
}

variable "node_instance_type" {
  type    = string
  default = "m6i.xlarge"
}

variable "database_instance_class" {
  type    = string
  default = "db.t4g.small"
}

variable "bedrock_model_id" {
  type    = string
  default = "amazon.nova-lite-v1:0"
}

variable "admin_principal_arns" {
  type        = list(string)
  default     = []
  description = "Extra IAM users/roles granted EKS cluster-admin (for debugging with kubectl)."
}

data "aws_caller_identity" "current" {}

data "aws_route53_zone" "main" {
  name         = var.route53_zone_name
  private_zone = false
}

locals {
  name       = "deepresearch-${var.environment}"
  account_id = data.aws_caller_identity.current.account_id
}
