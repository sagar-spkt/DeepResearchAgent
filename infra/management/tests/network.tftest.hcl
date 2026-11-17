mock_provider "aws" {
  mock_resource "aws_subnet" {
    defaults = { id = "subnet-executor" }
  }
  mock_resource "aws_security_group" {
    defaults = { id = "sg-executor", ingress = [] }
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
    condition     = length(aws_kms_key.runner_token) == 0 && length(aws_instance.runner) == 0
    error_message = "The runner key and EC2 instance must both be absent when disabled."
  }
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

run "runner_key_can_be_created_before_launch" {
  command = plan
  variables { enable_github_runner = true }
  assert {
    condition     = length(aws_kms_key.runner_token) == 1 && length(aws_instance.runner) == 0
    error_message = "Runner opt-in must create its KMS key without starting an instance before token staging."
  }
}

run "ephemeral_runner_is_scoped_and_private" {
  command = apply
  variables {
    enable_github_runner                 = true
    launch_github_runner                 = true
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
    condition     = strcontains(aws_instance.runner[0].user_data, "chmod 755 /usr/local/sbin/actions-runner-terminate") && strcontains(aws_instance.runner[0].user_data, "trap on_exit EXIT") && strcontains(aws_instance.runner[0].user_data, "OnActiveSec=85min")
    error_message = "Bootstrap must install an executable completion hook and deadline before setup failure."
  }
  assert {
    condition     = strcontains(aws_instance.runner[0].user_data, "set_bootstrap_step") && strcontains(aws_instance.runner[0].user_data, "failed at %s with exit %s") && strcontains(aws_instance.runner[0].user_data, "wait for Docker and Buildx")
    error_message = "Runner bootstrap must emit a safe stage marker and non-secret failure location before fail-closed termination."
  }
  assert {
    condition     = strcontains(aws_instance.runner[0].user_data, "tag_value=\"failed-") && strcontains(aws_iam_role_policy.runner[0].policy, "aws:RequestTag/BootstrapStatus") && strcontains(aws_iam_role_policy.runner[0].policy, "aws:TagKeys") && strcontains(aws_iam_role_policy.runner[0].policy, "failed-*")
    error_message = "Bootstrap failure reporting must write only a fixed stage and exit code through narrowly scoped instance-tag permission."
  }
  assert {
    condition     = strcontains(aws_instance.runner[0].user_data, "baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40") && !strcontains(aws_instance.runner[0].user_data, "token-value")
    error_message = "Bootstrap must pin the runner checksum and contain no registration token value."
  }
  assert {
    condition     = strcontains(aws_instance.runner[0].user_data, "/^URIs:/s#http://#https://#g") && strcontains(aws_instance.runner[0].user_data, "apt-get -o Acquire::Retries=3 update") && strcontains(aws_instance.runner[0].user_data, "apt-get -o Acquire::Retries=3 install") && !strcontains(aws_instance.runner[0].user_data, "apt-get install -y --no-install-recommends awscli")
    error_message = "The private runner must rewrite all Ubuntu source URIs to HTTPS, retry package downloads, and avoid the removed Noble awscli package."
  }
  assert {
    condition     = strcontains(aws_instance.runner[0].user_data, "https://awscli.amazonaws.com/v2/install.sh.sig") && strcontains(aws_instance.runner[0].user_data, "gpg --batch --verify") && strcontains(aws_instance.runner[0].user_data, "FB5DB77FD5C118B80511ADA8A6310ACC4672475C") && strcontains(aws_instance.runner[0].user_data, "aws_cli_version=2.40.3") && strcontains(aws_instance.runner[0].user_data, "--system --version \"$aws_cli_version\"")
    error_message = "AWS CLI installation must verify the official signed installer against its pinned signing key fingerprint."
  }
  assert {
    condition = toset(flatten([
      for statement in jsondecode(aws_iam_role_policy.runner[0].policy).Statement : try(tolist(statement.Action), [statement.Action])
    ])) == toset(["ssm:GetParameter", "ssm:DeleteParameter", "kms:Decrypt", "ec2:TerminateInstances", "ec2:CreateTags"])
    error_message = "Runner instance IAM must contain only exact token handoff, scoped failure reporting and self-termination actions."
  }
  assert {
    condition     = strcontains(aws_iam_role_policy.runner[0].policy, format("%s:%s", jsonencode("ec2:ResourceTag/Project"), jsonencode("deepresearch"))) && strcontains(aws_iam_role_policy.runner[0].policy, format("%s:%s", jsonencode("ec2:ResourceTag/Component"), jsonencode("github-actions-runner"))) && strcontains(aws_iam_role_policy.runner[0].policy, format("%s:%s", jsonencode("ec2:ResourceTag/Session"), jsonencode("test-1"))) && strcontains(aws_iam_role_policy.runner[0].policy, format("%s:%s", jsonencode("ec2:ResourceTag/Environment"), jsonencode("staging")))
    error_message = "Self-termination must require exact project, component, session and environment tags."
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
