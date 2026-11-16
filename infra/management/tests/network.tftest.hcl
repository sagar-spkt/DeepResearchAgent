mock_provider "aws" {}

variables {
  account_id        = "123456789012"
  environment       = "staging"
  session_id        = "test-1"
  availability_zone = "us-east-1a"
}

run "private_executor_network" {
  command = plan
  assert {
    condition     = aws_vpc.management.cidr_block == "10.70.0.0/16" && aws_subnet.executor.cidr_block == "10.70.1.0/24"
    error_message = "Management must remain separate from runtime 10.60.0.0/16."
  }
  assert {
    condition     = !aws_subnet.executor.map_public_ip_on_launch && aws_subnet.public.availability_zone == aws_subnet.executor.availability_zone
    error_message = "CodeBuild uses a private subnet and a same-AZ NAT."
  }
  assert {
    condition     = aws_route.executor_egress.destination_cidr_block == "0.0.0.0/0" && aws_route.internet.destination_cidr_block == "0.0.0.0/0"
    error_message = "Both NAT and public internet routes are required."
  }
  assert {
    condition     = aws_vpc_security_group_egress_rule.https.from_port == 443 && aws_vpc_security_group_egress_rule.https.to_port == 443 && aws_vpc_security_group_egress_rule.https.ip_protocol == "tcp"
    error_message = "Runner egress is HTTPS only."
  }
  assert {
    condition     = aws_vpc_endpoint.s3.vpc_endpoint_type == "Gateway" && aws_vpc_endpoint.s3.service_name == "com.amazonaws.us-east-1.s3"
    error_message = "Use the S3 gateway endpoint rather than paid interface endpoints."
  }
}

run "reject_bad_session" {
  command = plan
  variables { session_id = "other/*" }
  expect_failures = [var.session_id]
}
run "reject_cross_region_zone" {
  command = plan
  variables { availability_zone = "us-west-2a" }
  expect_failures = [var.availability_zone]
}
run "reject_account" {
  command = plan
  variables { account_id = "not-an-account" }
  expect_failures = [var.account_id]
}
run "reject_environment" {
  command = plan
  variables { environment = "dev" }
  expect_failures = [var.environment]
}
