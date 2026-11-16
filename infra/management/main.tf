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

output "vpc_id" { value = aws_vpc.management.id }
output "vpc_cidr" { value = aws_vpc.management.cidr_block }
output "private_subnet_ids" { value = [aws_subnet.executor.id] }
output "private_route_table_id" { value = aws_route_table.executor.id }
output "security_group_ids" { value = [aws_security_group.executor.id] }
output "nat_public_ip" { value = aws_eip.nat.public_ip }
output "nat_gateway_id" { value = aws_nat_gateway.management.id }
output "nat_allocation_id" { value = aws_eip.nat.id }
