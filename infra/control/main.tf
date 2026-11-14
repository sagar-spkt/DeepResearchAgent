terraform {
  required_version = ">= 1.10, < 2.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "= 6.20.0" }
  }
  backend "s3" {}
}
variable "account_id" { type = string }
variable "region" { type = string }
provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
}
resource "aws_dynamodb_table" "control" {
  name                        = "deepresearch-control"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "key"
  deletion_protection_enabled = true
  attribute {
    name = "key"
    type = "S"
  }
  server_side_encryption { enabled = true }
  point_in_time_recovery { enabled = true }
  tags = { Project = "deepresearch", Component = "control" }
  lifecycle { prevent_destroy = true }
}
output "control_table_arn" { value = aws_dynamodb_table.control.arn }
