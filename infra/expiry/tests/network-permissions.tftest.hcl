mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
}

override_resource {
  target = aws_cloudwatch_log_group.executor
  values = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:/aws/codebuild/deepresearch-staging-expiry-test-1" }
}

variables {
  region                        = "us-east-1"
  environment                   = "staging"
  session_id                    = "test-1"
  expire_at_utc                 = "2030-01-01T00:00:00"
  source_bucket                 = "synthetic-source"
  source_key                    = "source.zip"
  source_version_id             = "version-1"
  source_kms_key_arn            = "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-4111-8111-111111111111"
  runner_image_digest           = "example.invalid/runner@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  runner_repository_arn         = "arn:aws:ecr:us-east-1:123456789012:repository/runner"
  approved_intent_sha256        = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  executor_policy_documents     = ["{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"sts:GetCallerIdentity\",\"Resource\":\"*\"}]}"]
  management_vpc_id             = "vpc-11111111"
  runtime_vpc_id                = "vpc-22222222"
  management_subnet_ids         = ["subnet-11111111"]
  management_security_group_ids = ["sg-11111111"]
  alert_topic_arn               = "arn:aws:sns:us-east-1:123456789012:deepresearch-staging-alerts"
}

run "network_permissions_are_scoped" {
  command = plan
  assert {
    condition = (
      length(aws_iam_policy.executor_permissions) == length(var.executor_policy_documents) &&
      length(aws_iam_role_policy_attachment.executor_permissions) == length(var.executor_policy_documents) &&
      length(aws_iam_policy.executor_permissions) <= 10 &&
      alltrue([for policy in values(aws_iam_policy.executor_permissions) : length(policy.policy) <= 6144 && length(policy.name) <= 128]) &&
      length(aws_iam_role_policy.artifact_and_logs.policy) +
      length(aws_iam_role_policy.describe_cluster.policy) +
      length(aws_iam_role_policy.executor_network.policy) <= 10240
    )
    error_message = "Every executor policy must be attached once and stay below the IAM managed-policy document limit."
  }
  assert {
    condition = toset(jsondecode(aws_iam_role_policy.executor_network.policy).Statement[1].Resource) == toset([
      "arn:aws:ec2:us-east-1:123456789012:network-interface/*",
      "arn:aws:ec2:us-east-1:123456789012:subnet/subnet-11111111",
      "arn:aws:ec2:us-east-1:123456789012:security-group/sg-11111111",
    ])
    error_message = "CreateNI must use only reviewed subnet/security group resources in this account/Region."
  }
  assert {
    condition = (
      jsondecode(aws_iam_role_policy.executor_network.policy).Statement[0].Resource == "*" &&
      jsondecode(aws_iam_role_policy.executor_network.policy).Statement[0].Condition.StringEquals["aws:RequestedRegion"] == "us-east-1" &&
      alltrue([for action in jsondecode(aws_iam_role_policy.executor_network.policy).Statement[0].Action : startswith(action, "ec2:Describe")])
    )
    error_message = "Only describe actions may use the Region-constrained global resource."
  }
  assert {
    condition = (
      jsondecode(aws_iam_role_policy.executor_network.policy).Statement[2].Condition.ArnEquals["ec2:Vpc"] == "arn:aws:ec2:us-east-1:123456789012:vpc/vpc-11111111" &&
      jsondecode(aws_iam_role_policy.executor_network.policy).Statement[2].Condition.ArnEquals["ec2:Subnet"] == ["arn:aws:ec2:us-east-1:123456789012:subnet/subnet-11111111"] &&
      jsondecode(aws_iam_role_policy.executor_network.policy).Statement[3].Condition.StringEquals["ec2:AuthorizedService"] == "codebuild.amazonaws.com"
    )
    error_message = "DeleteNI and permission grants must remain bound to the management network and CodeBuild."
  }
}

run "reject_too_many_executor_managed_policies" {
  command = plan
  variables {
    executor_policy_documents = [for index in range(11) : jsonencode({
      Version   = "2012-10-17"
      Statement = [{ Sid = "p${index}", Effect = "Allow", Action = "sts:GetCallerIdentity", Resource = "*" }]
    })]
  }
  expect_failures = [var.executor_policy_documents]
}

run "reject_unscoped_subnet" {
  command = plan
  variables { management_subnet_ids = ["*"] }
  expect_failures = [aws_iam_role_policy.executor_network]
}
run "reject_empty_security_groups" {
  command = plan
  variables { management_security_group_ids = [] }
  expect_failures = [aws_iam_role_policy.executor_network]
}
run "reject_duplicate_subnets" {
  command = plan
  variables { management_subnet_ids = ["subnet-11111111", "subnet-11111111"] }
  expect_failures = [aws_iam_role_policy.executor_network]
}
run "reject_malformed_vpc" {
  command = plan
  variables { management_vpc_id = "vpc-*" }
  expect_failures = [aws_iam_role_policy.executor_network]
}
run "reject_wildcard_region" {
  command = plan
  variables { region = "*" }
  expect_failures = [aws_iam_role_policy.executor_network]
}
