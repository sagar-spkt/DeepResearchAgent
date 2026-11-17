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
  values = { id = "deepresearch-staging-github-oidc", name = "deepresearch-staging-github-oidc", arn = "arn:aws:iam::123456789012:role/deepresearch-staging-github-oidc" }
}
override_resource {
  target = aws_iam_role.github_actions["production"]
  values = { id = "deepresearch-production-github-oidc", name = "deepresearch-production-github-oidc", arn = "arn:aws:iam::123456789012:role/deepresearch-production-github-oidc" }
}
override_resource {
  target = aws_iam_role.staging_provisioner
  values = { id = "deepresearch-staging-provisioner-github-oidc", name = "deepresearch-staging-provisioner-github-oidc", arn = "arn:aws:iam::123456789012:role/deepresearch-staging-provisioner-github-oidc" }
}
override_resource {
  target = aws_iam_role.staging_provisioner_session["management"]
  values = { id = "deepresearch-staging-management-provisioner", name = "deepresearch-staging-management-provisioner", arn = "arn:aws:iam::123456789012:role/deepresearch-staging-management-provisioner" }
}
override_resource {
  target = aws_iam_role.staging_provisioner_session["runtime"]
  values = { id = "deepresearch-staging-runtime-provisioner", name = "deepresearch-staging-runtime-provisioner", arn = "arn:aws:iam::123456789012:role/deepresearch-staging-runtime-provisioner" }
}
override_resource {
  target = aws_iam_policy.staging_provisioner["common"]
  values = { id = "arn:aws:iam::123456789012:policy/deepresearch-staging-provisioner-common", arn = "arn:aws:iam::123456789012:policy/deepresearch-staging-provisioner-common", name = "deepresearch-staging-provisioner-common" }
}
override_resource {
  target = aws_iam_policy.staging_provisioner["management-network"]
  values = { id = "arn:aws:iam::123456789012:policy/deepresearch-staging-provisioner-management-network", arn = "arn:aws:iam::123456789012:policy/deepresearch-staging-provisioner-management-network", name = "deepresearch-staging-provisioner-management-network" }
}
override_resource {
  target = aws_iam_policy.staging_provisioner["management-runner"]
  values = { id = "arn:aws:iam::123456789012:policy/deepresearch-staging-provisioner-management-runner", arn = "arn:aws:iam::123456789012:policy/deepresearch-staging-provisioner-management-runner", name = "deepresearch-staging-provisioner-management-runner" }
}
override_resource {
  target = aws_iam_policy.staging_provisioner["management-egress"]
  values = { id = "arn:aws:iam::123456789012:policy/deepresearch-staging-provisioner-management-egress", arn = "arn:aws:iam::123456789012:policy/deepresearch-staging-provisioner-management-egress", name = "deepresearch-staging-provisioner-management-egress" }
}
override_resource {
  target = aws_iam_policy.staging_provisioner["runtime-network"]
  values = { id = "arn:aws:iam::123456789012:policy/deepresearch-staging-provisioner-runtime-network", arn = "arn:aws:iam::123456789012:policy/deepresearch-staging-provisioner-runtime-network", name = "deepresearch-staging-provisioner-runtime-network" }
}
override_resource {
  target = aws_iam_policy.staging_provisioner["runtime-eks"]
  values = { id = "arn:aws:iam::123456789012:policy/deepresearch-staging-provisioner-runtime-eks", arn = "arn:aws:iam::123456789012:policy/deepresearch-staging-provisioner-runtime-eks", name = "deepresearch-staging-provisioner-runtime-eks" }
}
override_resource {
  target = aws_iam_policy.staging_provisioner["runtime-service"]
  values = { id = "arn:aws:iam::123456789012:policy/deepresearch-staging-provisioner-runtime-service", arn = "arn:aws:iam::123456789012:policy/deepresearch-staging-provisioner-runtime-service", name = "deepresearch-staging-provisioner-runtime-service" }
}
override_resource { target = aws_iam_role_policy_attachments_exclusive.github_actions["staging"] }
override_resource { target = aws_iam_role_policy_attachments_exclusive.github_actions["production"] }
override_resource { target = aws_iam_role_policies_exclusive.github_actions["staging"] }
override_resource { target = aws_iam_role_policies_exclusive.github_actions["production"] }
override_resource { target = aws_iam_role_policy_attachments_exclusive.staging_provisioner_session["management"] }
override_resource { target = aws_iam_role_policy_attachments_exclusive.staging_provisioner_session["runtime"] }
override_resource { target = aws_iam_role_policies_exclusive.staging_provisioner_session["management"] }
override_resource { target = aws_iam_role_policies_exclusive.staging_provisioner_session["runtime"] }

run "staging_provisioning_roles_are_stack_and_session_bound" {
  command = apply

  assert {
    condition     = jsondecode(aws_iam_role.staging_provisioner.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:sagar-spkt@22609549/DeepResearchAgent@1457879530:environment:staging" && jsondecode(aws_iam_role.staging_provisioner.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:ref"] == "refs/heads/main"
    error_message = "Only this repository's staging environment on main may enter the provisioning bootstrap role."
  }

  assert {
    condition     = toset(jsondecode(aws_iam_role_policy.staging_provisioner_assume_session.policy).Statement[0].Resource) == toset([local.staging_management_provisioner_arn, local.staging_runtime_provisioner_arn])
    error_message = "The bootstrap role may assume only the management/runtime provisioner roles."
  }

  assert {
    condition     = alltrue([for stack, role in aws_iam_role.staging_provisioner_session : jsondecode(role.assume_role_policy).Statement[0].Condition.StringEquals["aws:RequestTag/Stack"] == stack && jsondecode(role.assume_role_policy).Statement[0].Condition.StringLike["aws:RequestTag/Session"] == "stg-*" && contains(jsondecode(role.assume_role_policy).Statement[0].Action, "sts:TagSession")])
    error_message = "Each role must require its exact stack and a staging session tag from the OIDC bootstrap role."
  }

  assert {
    condition     = alltrue([for name, policy in aws_iam_policy.staging_provisioner : length(policy.policy) < 6144])
    error_message = "Every serialized customer managed policy must fit AWS's 6,144 character policy quota."
  }

  assert {
    condition     = strcontains(aws_iam_policy.staging_provisioner["common"].policy, "$${aws:PrincipalTag/Session}") && strcontains(aws_iam_policy.staging_provisioner["runtime-network"].policy, "ec2:CreateAction") && strcontains(aws_iam_policy.staging_provisioner["runtime-eks"].policy, "eks:TagResource")
    error_message = "State access, EC2 creation tags, and EKS create-time tags must be limited to the session."
  }

  assert {
    condition     = one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-runner"].policy).Statement : statement if statement.Sid == "UseExactUbuntuRunnerAMI"]).Resource == "arn:aws:ec2:${var.region}::image/${var.staging_runner_ami_id}"
    error_message = "The management provisioner may launch only the exact public runner AMI pinned for this staging environment."
  }

  assert {
    condition     = one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-runner"].policy).Statement : statement if statement.Sid == "PassOnlyStagingRunnerRoleToEC2"]).Resource == "arn:aws:iam::123456789012:role/deepresearch-staging-management-$${aws:PrincipalTag/Session}-runner" && one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-runner"].policy).Statement : statement if statement.Sid == "PassOnlyStagingRunnerRoleToEC2"]).Condition.StringEquals["iam:PassedToService"] == "ec2.amazonaws.com"
    error_message = "The management provisioner may pass only its exact session's runner role to EC2."
  }

  assert {
    condition     = one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-runner"].policy).Statement : statement if statement.Sid == "SetOnlyTheSessionRunnerShutdownBehavior"]).Action == ["ec2:ModifyInstanceAttribute"] && one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-runner"].policy).Statement : statement if statement.Sid == "SetOnlyTheSessionRunnerShutdownBehavior"]).Resource == "arn:aws:ec2:${var.region}:123456789012:instance/*" && one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-runner"].policy).Statement : statement if statement.Sid == "SetOnlyTheSessionRunnerShutdownBehavior"]).Condition.StringEquals["ec2:Attribute/instanceInitiatedShutdownBehavior"] == "terminate" && one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-runner"].policy).Statement : statement if statement.Sid == "SetOnlyTheSessionRunnerShutdownBehavior"]).Condition.StringEquals["aws:ResourceTag/Project"] == "deepresearch" && one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-runner"].policy).Statement : statement if statement.Sid == "SetOnlyTheSessionRunnerShutdownBehavior"]).Condition.StringEquals["aws:ResourceTag/Environment"] == "staging" && one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-runner"].policy).Statement : statement if statement.Sid == "SetOnlyTheSessionRunnerShutdownBehavior"]).Condition.StringEquals["aws:ResourceTag/Session"] == "$${aws:PrincipalTag/Session}" && one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-runner"].policy).Statement : statement if statement.Sid == "SetOnlyTheSessionRunnerShutdownBehavior"]).Condition.StringEquals["aws:ResourceTag/Component"] == "github-actions-runner" && one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-runner"].policy).Statement : statement if statement.Sid == "SetOnlyTheSessionRunnerShutdownBehavior"]).Condition.StringEquals["aws:RequestedRegion"] == var.region
    error_message = "ModifyInstanceAttribute must only set terminate-on-shutdown on the exact session's private runner instance."
  }

  assert {
    condition     = one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-runner"].policy).Statement : statement if statement.Sid == "TagOnlyTheManagementSessionRunnerVolumeAtLaunch"]).Resource == "arn:aws:ec2:${var.region}:123456789012:volume/*" && one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-runner"].policy).Statement : statement if statement.Sid == "TagOnlyTheManagementSessionRunnerVolumeAtLaunch"]).Condition.StringEquals["ec2:CreateAction"] == "RunInstances" && one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-runner"].policy).Statement : statement if statement.Sid == "TagOnlyTheManagementSessionRunnerVolumeAtLaunch"]).Condition.StringEquals["aws:RequestedRegion"] == var.region && one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-runner"].policy).Statement : statement if statement.Sid == "TagOnlyTheManagementSessionRunnerVolumeAtLaunch"]).Condition.StringEquals["aws:RequestTag/Project"] == "deepresearch" && one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-runner"].policy).Statement : statement if statement.Sid == "TagOnlyTheManagementSessionRunnerVolumeAtLaunch"]).Condition.StringEquals["aws:RequestTag/Environment"] == "staging" && one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-runner"].policy).Statement : statement if statement.Sid == "TagOnlyTheManagementSessionRunnerVolumeAtLaunch"]).Condition.StringEquals["aws:RequestTag/Session"] == "$${aws:PrincipalTag/Session}" && one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-runner"].policy).Statement : statement if statement.Sid == "TagOnlyTheManagementSessionRunnerVolumeAtLaunch"]).Condition.StringEquals["aws:RequestTag/Component"] == "management-network" && one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-runner"].policy).Statement : statement if statement.Sid == "TagOnlyTheManagementSessionRunnerVolumeAtLaunch"]).Condition.StringEquals["aws:RequestTag/ManagedBy"] == "terraform" && one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-runner"].policy).Statement : statement if statement.Sid == "TagOnlyTheManagementSessionRunnerVolumeAtLaunch"]).Condition["ForAllValues:StringEquals"]["aws:TagKeys"] == ["Project", "Environment", "Session", "Component", "ManagedBy"]
    error_message = "The runner volume may be tagged only with the exact management-network, Terraform, and session tags during instance launch."
  }

  assert {
    condition     = one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-egress"].policy).Statement : statement if statement.Sid == "AuthorizeOnlyTaggedManagementEgressRules"]).Resource == "arn:aws:ec2:${var.region}:123456789012:security-group-rule/*" && one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-egress"].policy).Statement : statement if statement.Sid == "AuthorizeOnlyTaggedManagementEgressRules"]).Condition.StringEquals["aws:RequestTag/Session"] == "$${aws:PrincipalTag/Session}" && one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-egress"].policy).Statement : statement if statement.Sid == "AuthorizeOnlyTaggedManagementEgressRules"]).Condition["ForAllValues:StringEquals"]["aws:TagKeys"] == ["Project", "Environment", "Session", "Component", "ManagedBy"]
    error_message = "Management egress rules may be authorized only when the exact session, network component, and Terraform tags are present."
  }

  assert {
    condition     = one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-egress"].policy).Statement : statement if statement.Sid == "TagOnlyNewManagementEgressRules"]).Action == ["ec2:CreateTags"] && one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-egress"].policy).Statement : statement if statement.Sid == "TagOnlyNewManagementEgressRules"]).Condition.StringEquals["ec2:CreateAction"] == "AuthorizeSecurityGroupEgress" && one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-egress"].policy).Statement : statement if statement.Sid == "TagOnlyNewManagementEgressRules"]).Condition.StringEquals["aws:RequestTag/Component"] == "management-network"
    error_message = "EC2 may tag only management egress rules during their creation, with the exact resource component."
  }

  assert {
    condition     = !strcontains(aws_iam_policy.staging_provisioner["runtime-service"].policy, "rds:DeleteDBInstance") && !strcontains(aws_iam_policy.staging_provisioner["runtime-service"].policy, "rds:DeleteDBSnapshot")
    error_message = "Provisioning must preserve the staging database and snapshots."
  }

  assert {
    condition     = one([for statement in jsondecode(aws_iam_policy.staging_provisioner["common"].policy).Statement : statement if statement.Sid == "MaintainOnlyStagingLifecycleLease"]).Action == ["dynamodb:UpdateItem"] && one([for statement in jsondecode(aws_iam_policy.staging_provisioner["common"].policy).Statement : statement if statement.Sid == "MaintainOnlyStagingLifecycleLease"]).Condition["ForAllValues:StringEquals"]["dynamodb:LeadingKeys"] == ["lifecycle-lock"]
    error_message = "Provisioning may update only the lifecycle lock, never delete its persistent application deployment markers or mutate the admission/monthly ledger."
  }

  assert {
    condition     = one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-runner"].policy).Statement : statement if statement.Sid == "ReadOnlyThisStagingDeploymentMarker"]).Action == ["dynamodb:GetItem"] && one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-runner"].policy).Statement : statement if statement.Sid == "ReadOnlyThisStagingDeploymentMarker"]).Condition["ForAllValues:StringEquals"]["dynamodb:LeadingKeys"] == ["app-writers#staging#$${aws:PrincipalTag/Session}"]
    error_message = "The management role may read only its exact session deployment marker for fail-closed finalizer checks."
  }

  assert {
    condition     = one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-network"].policy).Statement : statement if statement.Sid == "ReadOnlyStagingFoundationStateForLifecycleFinalization"]).Action == ["s3:GetObject"] && one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-network"].policy).Statement : statement if statement.Sid == "ReadOnlyStagingFoundationStateForLifecycleFinalization"]).Resource == "arn:aws:s3:::${var.staging_state_bucket}/deepresearch/staging/foundation.tfstate" && one([for statement in jsondecode(aws_iam_policy.staging_provisioner["management-network"].policy).Statement : statement if statement.Sid == "ReadOnlyStagingFoundationStateForLifecycleFinalization"]).Condition.Bool["aws:SecureTransport"] == "true"
    error_message = "The management workflow may read only the private foundation state object needed to build the lifecycle release config."
  }
}
