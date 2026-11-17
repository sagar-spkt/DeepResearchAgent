mock_provider "aws" {
  mock_resource "aws_subnet" {
    defaults = { id = "subnet-executor" }
  }
  mock_resource "aws_security_group" {
    defaults = { id = "sg-executor", ingress = [] }
  }
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"sts:AssumeRole\",\"Principal\":{\"Service\":\"ec2.amazonaws.com\"}}]}"
    }
  }
}

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

run "ephemeral_runner_is_scoped_and_private" {
  command = apply
  variables {
    enable_github_runner                 = true
    github_repository                    = "owner/repo"
    github_runner_ami_id                 = "ami-0123456789abcdef0"
    github_runner_registration_parameter = "/deepresearch/runner/test-1/registration"
  }
  assert {
    condition     = aws_instance.runner[0].associate_public_ip_address == false && aws_instance.runner[0].subnet_id == aws_subnet.executor.id
    error_message = "The runner must use the private management executor subnet without public IP."
  }
  assert {
    condition     = aws_instance.runner[0].metadata_options[0].http_tokens == "required" && aws_instance.runner[0].metadata_options[0].http_put_response_hop_limit == 1
    error_message = "The runner must require IMDSv2 and use hop limit one."
  }
  assert {
    condition     = aws_instance.runner[0].instance_initiated_shutdown_behavior == "terminate"
    error_message = "Bootstrap shutdown fallback must terminate the instance."
  }
  assert {
    condition     = aws_instance.runner[0].root_block_device[0].encrypted && aws_instance.runner[0].root_block_device[0].delete_on_termination
    error_message = "Runner storage must be encrypted and removed with the instance."
  }
  assert {
    condition     = aws_instance.runner[0].tags["Component"] == "github-actions-runner" && aws_instance.runner[0].tags["Session"] == "test-1"
    error_message = "Runner termination scope must include exact session tags."
  }
  assert {
    condition     = strcontains(aws_instance.runner[0].user_data, "baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40") && !strcontains(aws_instance.runner[0].user_data, "token-value")
    error_message = "Bootstrap must pin the runner checksum and contain no registration token value."
  }
  assert {
    condition     = length(aws_security_group.executor.ingress) == 0 && aws_vpc_security_group_egress_rule.https.to_port == 443
    error_message = "Runner networking must have no ingress and retain HTTPS-only egress."
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
