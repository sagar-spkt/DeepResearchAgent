mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
}

variables {
  account_id            = "123456789012"
  staging_state_bucket  = "deepresearch-state-123456789012-us-east-1"
  staging_runner_ami_id = "ami-0123456789abcdef0"
}

override_resource {
  target = aws_iam_role.github_actions["staging"]
  values = { id = "deepresearch-staging-github-oidc", name = "deepresearch-staging-github-oidc", arn = "arn:aws:iam::123456789012:role/deepresearch-staging-github-oidc" }
}
override_resource {
  target = aws_iam_role.github_actions["production"]
  values = { id = "deepresearch-production-github-oidc", name = "deepresearch-production-github-oidc", arn = "arn:aws:iam::123456789012:role/deepresearch-production-github-oidc" }
}
override_resource { target = aws_iam_role_policy_attachments_exclusive.github_actions["staging"] }
override_resource { target = aws_iam_role_policy_attachments_exclusive.github_actions["production"] }
override_resource { target = aws_iam_role_policies_exclusive.github_actions["staging"] }
override_resource { target = aws_iam_role_policies_exclusive.github_actions["production"] }

run "expiry_image_publisher_is_repository_and_image_scoped" {
  command = plan

  assert {
    condition     = jsondecode(aws_iam_role.staging_expiry_image_publisher.assume_role_policy).Statement[0].Principal.Federated == local.github_oidc_provider_arn && jsondecode(aws_iam_role.staging_expiry_image_publisher.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com" && jsondecode(aws_iam_role.staging_expiry_image_publisher.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "${local.repository_subject}:environment:staging" && jsondecode(aws_iam_role.staging_expiry_image_publisher.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:ref"] == "refs/heads/main"
    error_message = "Only this repository's staging environment on main may assume the expiry image publisher role."
  }

  assert {
    condition     = toset(jsondecode(aws_iam_role_policy.staging_expiry_image_publisher.policy).Statement[0].Action) == toset(["ecr:GetAuthorizationToken"]) && jsondecode(aws_iam_role_policy.staging_expiry_image_publisher.policy).Statement[0].Resource == "*" && toset(jsondecode(aws_iam_role_policy.staging_expiry_image_publisher.policy).Statement[1].Action) == toset(["ecr:BatchCheckLayerAvailability", "ecr:CompleteLayerUpload", "ecr:InitiateLayerUpload", "ecr:PutImage", "ecr:UploadLayerPart"]) && jsondecode(aws_iam_role_policy.staging_expiry_image_publisher.policy).Statement[1].Resource == "arn:aws:ecr:${var.region}:123456789012:repository/deepresearch-staging/expiry-runner"
    error_message = "The publisher may authenticate to ECR and push layers/images only to the staging expiry-runner repository."
  }

  assert {
    condition     = toset(aws_iam_role_policies_exclusive.staging_expiry_image_publisher.policy_names) == toset([aws_iam_role_policy.staging_expiry_image_publisher.name])
    error_message = "The expiry image publisher role must have exactly its scoped inline ECR policy."
  }

  assert {
    condition     = length(aws_iam_role_policy_attachments_exclusive.staging_expiry_image_publisher.policy_arns) == 0
    error_message = "The expiry image publisher role must not have attached managed policies."
  }
}
