terraform {

  required_version = ">= 1.10, < 2.0"
  required_providers {

    aws = {
      source = "hashicorp/aws", version = "= 6.20.0"
    }


  }

  backend "s3" {

  }


}

provider "aws" {

  region              = var.region
  allowed_account_ids = [var.account_id]
  default_tags {
    tags = {
      Project = "deepresearch", Environment = var.environment, ManagedBy = "terraform"
    }

  }


}

variable "account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Specify the intended twelve-digit AWS account ID."
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
    error_message = "Only staging or production."
  }


}

data "aws_caller_identity" "current" {

}

locals {
  name = "deepresearch-${var.environment}"
}

