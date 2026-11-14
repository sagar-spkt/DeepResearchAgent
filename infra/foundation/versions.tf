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

  region = var.region
  default_tags {
    tags = {
      Project = "deepresearch", Environment = var.environment, ManagedBy = "terraform"
    }

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

