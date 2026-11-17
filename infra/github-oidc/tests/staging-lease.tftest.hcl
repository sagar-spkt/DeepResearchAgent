mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
}

variables {
  account_id            = "123456789012"
  region                = "us-east-1"
  staging_state_bucket  = "deepresearch-state-123456789012-us-east-1"
  staging_runner_ami_id = "ami-0123456789abcdef0"
}

override_resource {
  target = aws_iam_role.github_actions["staging"]
  values = {
    id   = "deepresearch-staging-github-oidc"
    name = "deepresearch-staging-github-oidc"
    arn  = "arn:aws:iam::123456789012:role/deepresearch-staging-github-oidc"
  }
}
override_resource {
  target = aws_iam_role.github_actions["production"]
  values = {
    id   = "deepresearch-production-github-oidc"
    name = "deepresearch-production-github-oidc"
    arn  = "arn:aws:iam::123456789012:role/deepresearch-production-github-oidc"
  }
}
override_resource {
  target = aws_iam_role_policy_attachments_exclusive.github_actions["staging"]
}
override_resource {
  target = aws_iam_role_policy_attachments_exclusive.github_actions["production"]
}
override_resource {
  target = aws_iam_role_policies_exclusive.github_actions["staging"]
}
override_resource {
  target = aws_iam_role_policies_exclusive.github_actions["production"]
}

run "staging_lease_permissions_are_exactly_scoped" {
  command = plan

  assert {
    condition = one([
      for statement in jsondecode(aws_iam_role_policy.staging_lifecycle_lease.policy).Statement : statement
      if statement.Sid == "ReadStagingAdmissionAndLifecycleLease"
    ]).Action == ["dynamodb:GetItem"]
    error_message = "Staging lease read permission must allow only GetItem."
  }
  assert {
    condition = one([
      for statement in jsondecode(aws_iam_role_policy.staging_lifecycle_lease.policy).Statement : statement
      if statement.Sid == "ReadStagingAdmissionAndLifecycleLease"
    ]).Resource == "arn:aws:dynamodb:us-east-1:123456789012:table/deepresearch-control"
    error_message = "Staging lease reads must target only the shared lifecycle table."
  }
  assert {
    condition = toset(one([
      for statement in jsondecode(aws_iam_role_policy.staging_lifecycle_lease.policy).Statement : statement
      if statement.Sid == "ReadStagingAdmissionAndLifecycleLease"
    ]).Condition["ForAllValues:StringEquals"]["dynamodb:LeadingKeys"]) == toset(["active-session", "lifecycle-lock"])
    error_message = "Staging reads must be limited to the admission and lease keys."
  }
  assert {
    condition = one([
      for statement in jsondecode(aws_iam_role_policy.staging_lifecycle_lease.policy).Statement : statement
      if statement.Sid == "MaintainOnlyStagingLifecycleLease"
      ]).Action == ["dynamodb:UpdateItem"] && one([
      for statement in jsondecode(aws_iam_role_policy.staging_lifecycle_lease.policy).Statement : statement
      if statement.Sid == "MaintainOnlyStagingLifecycleLease"
    ]).Resource == "arn:aws:dynamodb:us-east-1:123456789012:table/deepresearch-control"
    error_message = "Staging lease maintenance must allow only UpdateItem on the exact control table."
  }
  assert {
    condition = toset(one([
      for statement in jsondecode(aws_iam_role_policy.staging_lifecycle_lease.policy).Statement : statement
      if statement.Sid == "MaintainOnlyStagingLifecycleLease"
    ]).Condition["ForAllValues:StringEquals"]["dynamodb:LeadingKeys"]) == toset(["lifecycle-lock"])
    error_message = "Staging lease updates must be limited to the lifecycle-lock item."
  }
  assert {
    condition = length(flatten([
      for statement in jsondecode(aws_iam_role_policy.staging_lifecycle_lease.policy).Statement : [
        for action in try(tolist(statement.Action), [statement.Action]) : action
        if startswith(action, "dynamodb:")
      ]
    ])) == 2
    error_message = "The staging deploy role must have no extra DynamoDB actions such as delete, scan, or batch access."
  }
  assert {
    condition = length(flatten([
      for statement in jsondecode(aws_iam_role_policy.staging_lifecycle_lease.policy).Statement : [
        for action in try(tolist(statement.Action), [statement.Action]) : action
        if contains(["ec2:RunInstances", "eks:CreateCluster", "eks:CreateNodegroup", "iam:PassRole"], action)
      ]
    ])) == 0
    error_message = "The lease permission change must not grant runtime provisioning rights."
  }
  assert {
    condition = one([
      for statement in jsondecode(aws_iam_role_policy.staging_deployment_marker.policy).Statement : statement
      if statement.Sid == "CheckExactStagingLifecycleLeaseForMarker"
      ]).Action == ["dynamodb:ConditionCheckItem"] && one([
      for statement in jsondecode(aws_iam_role_policy.staging_deployment_marker.policy).Statement : statement
      if statement.Sid == "CheckExactStagingLifecycleLeaseForMarker"
      ]).Condition["ForAllValues:StringEquals"]["dynamodb:LeadingKeys"] == ["lifecycle-lock"] && one([
      for statement in jsondecode(aws_iam_role_policy.staging_deployment_marker.policy).Statement : statement
      if statement.Sid == "CheckExactStagingLifecycleLeaseForMarker"
      ]).Condition["ForAnyValue:StringEquals"]["dynamodb:EnclosingOperation"] == ["TransactWriteItems"] && toset(one([
        for statement in jsondecode(aws_iam_role_policy.staging_deployment_marker.policy).Statement : statement
        if statement.Sid == "CheckExactStagingLifecycleLeaseForMarker"
    ]).Condition["ForAllValues:StringEquals"]["dynamodb:Attributes"]) == toset(["key", "owner_id", "session_id", "expires_at"])
    error_message = "Marker creation must condition-check only the active owner's exact lifecycle lease."
  }
  assert {
    condition = one([
      for statement in jsondecode(aws_iam_role_policy.staging_deployment_marker.policy).Statement : statement
      if statement.Sid == "CreateWriteOnceStagingDeploymentMarkers"
      ]).Action == ["dynamodb:PutItem"] && one([
      for statement in jsondecode(aws_iam_role_policy.staging_deployment_marker.policy).Statement : statement
      if statement.Sid == "CreateWriteOnceStagingDeploymentMarkers"
    ]).Resource == "arn:aws:dynamodb:us-east-1:123456789012:table/deepresearch-control"
    error_message = "The deploy role may only create staging deployment markers in the exact lifecycle table."
  }
  assert {
    condition = toset(one([
      for statement in jsondecode(aws_iam_role_policy.staging_deployment_marker.policy).Statement : statement
      if statement.Sid == "CreateWriteOnceStagingDeploymentMarkers"
      ]).Condition["ForAllValues:StringLike"]["dynamodb:LeadingKeys"]) == toset(["app-writers#staging#stg-*"]) && toset(one([
      for statement in jsondecode(aws_iam_role_policy.staging_deployment_marker.policy).Statement : statement
      if statement.Sid == "CreateWriteOnceStagingDeploymentMarkers"
      ]).Condition["ForAllValues:StringEquals"]["dynamodb:Attributes"]) == toset(["key", "session_id", "environment", "started_at", "commit_sha", "workflow_run_id"]) && one([
      for statement in jsondecode(aws_iam_role_policy.staging_deployment_marker.policy).Statement : statement
      if statement.Sid == "CreateWriteOnceStagingDeploymentMarkers"
    ]).Condition["ForAnyValue:StringEquals"]["dynamodb:EnclosingOperation"] == ["TransactWriteItems"]
    error_message = "Marker creation must be limited to staging session keys and the six fixed metadata attributes."
  }
  assert {
    condition = one([
      for statement in jsondecode(aws_iam_role_policy.staging_deployment_marker.policy).Statement : statement
      if statement.Sid == "ReadOnlyStagingDeploymentMarkers"
      ]).Action == ["dynamodb:GetItem"] && one([
      for statement in jsondecode(aws_iam_role_policy.staging_deployment_marker.policy).Statement : statement
      if statement.Sid == "ReadOnlyStagingDeploymentMarkers"
    ]).Condition["ForAllValues:StringLike"]["dynamodb:LeadingKeys"] == ["app-writers#staging#stg-*"]
    error_message = "Marker reads must be read-only and confined to staging session marker keys."
  }
  assert {
    condition = length(flatten([
      for statement in jsondecode(aws_iam_role_policy.staging_deploy.policy).Statement : [
        for action in try(tolist(statement.Action), [statement.Action]) : action
        if startswith(action, "dynamodb:")
      ]
    ])) == 0
    error_message = "DynamoDB access must stay isolated in the dedicated lease policy."
  }
  assert {
    condition = toset(aws_iam_role_policies_exclusive.github_actions["staging"].policy_names) == toset([
      "deepresearch-staging-deploy",
      "deepresearch-staging-lifecycle-lease",
      "deepresearch-staging-deployment-marker",
    ])
    error_message = "The exclusive role policy guard must retain exactly the staging deploy and lease policies."
  }
}
