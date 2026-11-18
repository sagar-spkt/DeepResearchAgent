"""Pure IAM policy generation for expiry operations; never attaches permissions."""

import re

from .core import Refused, inspect_destroy, validate_config


def backup_policy(config):
    """Return session-scoped backup AWS permissions, without deployment or purge rights.

    This is an identity policy fragment. Kubernetes authorization and credentials
    used by ClickHouse itself must be configured and reviewed separately.
    """
    validate_config(config)
    account, region = config["account_id"], config["region"]
    environment, session = config["environment"], config["session_id"]
    if len(session) > 16:
        raise Refused("Backup policy session ID must fit the expiry session limit")
    if not config["user_pool_id"].startswith(region + "_"):
        raise Refused("Cognito pool must belong to the configured region")
    buckets = [
        "arn:aws:s3:::" + config[key]
        for key in ["document_bucket", "trace_bucket", "backup_bucket"]
    ]
    database = f"arn:aws:rds:{region}:{account}:db:{config['database_identifier']}"
    snapshot = f"arn:aws:rds:{region}:{account}:snapshot:{config['database_identifier']}-{session}-backup"
    table = f"arn:aws:dynamodb:{region}:{account}:table/deepresearch-control"
    statements = []

    def allow(sid, actions, resources, condition=None):
        statement = {"Sid": sid, "Effect": "Allow", "Action": actions, "Resource": resources}
        if condition:
            statement["Condition"] = condition
        statements.append(statement)

    allow("RetainedBucketMetadata", ["s3:GetBucketTagging", "s3:GetBucketVersioning", "s3:ListBucketVersions"], buckets)
    allow("VerifyRetainedVersions", ["s3:GetObjectVersion"], [bucket + "/*" for bucket in buckets])
    prefix = f"arn:aws:s3:::{config['backup_bucket']}/sessions/{environment}/{session}/"
    allow("WriteSessionBackups", ["s3:PutObject"], [
        prefix + "valkey/dump.rdb", prefix + "receipts/backup.json",
    ], {"StringEquals": {"s3:x-amz-server-side-encryption": "AES256"}})
    allow("DescribeExactCluster", ["eks:DescribeCluster"], [
        f"arn:aws:eks:{region}:{account}:cluster/{config['cluster_name']}"
    ])
    allow("VerifyRetainedIdentity", ["cognito-idp:DescribeUserPool"], [
        f"arn:aws:cognito-idp:{region}:{account}:userpool/{config['user_pool_id']}"
    ])
    allow("VerifyAlertTopic", ["sns:GetTopicAttributes"], [config["alert_topic_arn"]])
    allow("VerifyAlertKey", ["kms:DescribeKey", "kms:ListResourceTags", "kms:GetKeyRotationStatus"], [config["alert_kms_key_arn"]])
    if config["github_runner_enabled"]:
        allow(
            "VerifyRunnerTokenKey",
            ["kms:DescribeKey", "kms:ListResourceTags"],
            [config["runner_token_kms_key_arn"]],
        )
    allow("DescribeBackupDatabase", ["rds:DescribeDBInstances"], [database])
    allow("VerifySnapshot", ["rds:DescribeDBSnapshots", "rds:ListTagsForResource"], [database, snapshot])
    request_tags = {"aws:RequestTag/" + key: value for key, value in {
        "Project": "deepresearch", "Environment": environment, "Session": session
    }.items()}
    allow("CreateExactSnapshot", ["rds:CreateDBSnapshot"], [database, snapshot], {
        "StringEquals": request_tags,
        "ForAllValues:StringEquals": {"aws:TagKeys": ["Project", "Environment", "Session"]},
    })
    allow("TagExactSnapshot", ["rds:AddTagsToResource"], [snapshot], {
        "StringEquals": request_tags,
        "ForAllValues:StringEquals": {"aws:TagKeys": ["Project", "Environment", "Session"]},
    })
    allow("VerifyControlTable", ["dynamodb:DescribeTable", "dynamodb:ListTagsOfResource"], [table])
    allow("ReadAdmissionAndLease", ["dynamodb:GetItem"], [table], {
        "ForAllValues:StringEquals": {"dynamodb:LeadingKeys": ["active-session", "lifecycle-lock"]},
        "Null": {"dynamodb:LeadingKeys": "false"},
    })
    allow("MaintainLeaseOnly", ["dynamodb:UpdateItem"], [table], {
        "ForAllValues:StringEquals": {"dynamodb:LeadingKeys": ["lifecycle-lock"]},
        "Null": {"dynamodb:LeadingKeys": "false"},
    })
    return {"Version": "2012-10-17", "Statement": statements}


_DESTROY_ACTIONS = {
    "aws_vpc": ["ec2:DeleteVpc"],
    "aws_internet_gateway": ["ec2:DetachInternetGateway", "ec2:DeleteInternetGateway"],
    "aws_subnet": ["ec2:DeleteSubnet"],
    "aws_route_table": ["ec2:DeleteRouteTable"],
    "aws_route_table_association": ["ec2:DisassociateRouteTable"],
    "aws_security_group": ["ec2:DeleteSecurityGroup"],
    "aws_vpc_security_group_ingress_rule": ["ec2:RevokeSecurityGroupIngress"],
    "aws_db_subnet_group": ["rds:DeleteDBSubnetGroup"],
    # The shutdown workflow clears deletion protection immediately before the
    # already-reviewed destroy plan is applied.
    "aws_db_instance": ["rds:ModifyDBInstance", "rds:DeleteDBInstance"],
    "aws_iam_role": ["iam:DeleteRole"],
    "aws_iam_role_policy_attachment": ["iam:DetachRolePolicy"],
    "aws_iam_role_policy": ["iam:DeleteRolePolicy"],
    "aws_eks_cluster": ["eks:DeleteCluster"],
    "aws_cloudwatch_log_group": ["logs:DeleteLogGroup"],
    "aws_eks_access_entry": ["eks:DeleteAccessEntry"],
    "aws_eks_access_policy_association": ["eks:DisassociateAccessPolicy"],
    "aws_iam_policy": ["iam:DeletePolicy", "iam:DeletePolicyVersion"],
    "aws_eks_node_group": ["eks:DeleteNodegroup"],
    "aws_eks_addon": ["eks:DeleteAddon"],
    "aws_eks_pod_identity_association": ["eks:DeletePodIdentityAssociation"],
}


def _runtime_resource_arn(item, config):
    """Map an already-inspected runtime plan record to its exact IAM resource ARN."""
    before = item.get("change", {}).get("before") or {}
    kind = item["type"]
    account, region = config["account_id"], config["region"]
    if kind.startswith("aws_eks_"):
        # EKS access-entry and access-policy APIs authorize against the
        # containing cluster ARN. Other resources use their exact resource ARN.
        if kind in {"aws_eks_access_entry", "aws_eks_access_policy_association"}:
            cluster_name = before.get("cluster_name")
            if cluster_name != config["cluster_name"]:
                raise Refused("Runtime EKS access resource is not bound to the configured cluster")
            return f"arn:aws:eks:{region}:{account}:cluster/{cluster_name}"
        arn = before.get("arn") or before.get("access_entry_arn") or before.get("association_arn")
        if isinstance(arn, str) and arn.startswith(f"arn:aws:eks:{region}:{account}:"):
            return arn
        raise Refused("Runtime EKS resource is missing its exact ARN: " + item["address"])
    if kind == "aws_db_instance":
        arn = before.get("arn")
        if arn == f"arn:aws:rds:{region}:{account}:db:{config['database_identifier']}":
            return arn
        raise Refused("Runtime database ARN does not match lifecycle config")
    if kind == "aws_db_subnet_group":
        name = before.get("name")
        if name:
            return f"arn:aws:rds:{region}:{account}:subgrp:{name}"
        raise Refused("Runtime DB subnet group is missing its name")
    if kind.startswith("aws_iam_"):
        if kind == "aws_iam_role_policy_attachment":
            role = before.get("role")
            policy = before.get("policy_arn")
            if not role or not policy:
                raise Refused("Runtime IAM attachment is missing role/policy identity")
            role_arn = role if role.startswith("arn:aws:iam::") else f"arn:aws:iam::{account}:role/{role}"
            return [role_arn, policy]
        if kind == "aws_iam_role_policy":
            role = before.get("role")
            if not role:
                raise Refused("Runtime inline policy is missing its exact role")
            return role if role.startswith("arn:aws:iam::") else f"arn:aws:iam::{account}:role/{role}"
        arn = before.get("arn")
        if isinstance(arn, str) and arn.startswith(f"arn:aws:iam::{account}:"):
            return arn
        raise Refused("Runtime IAM resource is missing its exact ARN: " + item["address"])
    if kind == "aws_cloudwatch_log_group":
        name = before.get("name")
        if name == f"/aws/eks/deepresearch-{config['environment']}/cluster":
            return f"arn:aws:logs:{region}:{account}:log-group:{name}:*"
        raise Refused("Runtime log group is not the exact EKS cluster log group")
    if kind == "aws_vpc_security_group_ingress_rule":
        security_group_id = before.get("security_group_id")
        if not isinstance(security_group_id, str) or not re.fullmatch(r"sg-[a-f0-9]{8,17}", security_group_id):
            raise Refused("Runtime ingress rule is missing its exact parent security group")
        return f"arn:aws:ec2:{region}:{account}:security-group/{security_group_id}"
    resource_types = {
        "aws_vpc": ("vpc", "id"),
        "aws_internet_gateway": ("internet-gateway", "id"),
        "aws_subnet": ("subnet", "id"),
        "aws_route_table": ("route-table", "id"),
        "aws_route_table_association": ("route-table", "route_table_id"),
        "aws_security_group": ("security-group", "id"),
    }
    if kind in resource_types:
        resource_type, field = resource_types[kind]
        identifier = before.get(field)
        if not identifier or not re.fullmatch(r"[a-z0-9-]+", identifier):
            raise Refused("Runtime EC2 resource is missing its exact identity: " + item["address"])
        return f"arn:aws:ec2:{region}:{account}:{resource_type}/{identifier}"
    raise Refused("No exact IAM ARN mapping for runtime resource: " + item["address"])


def _iam_inventory_resources(changes, config):
    """Return only IAM roles and policies present in this runtime plan plus expiry roles."""
    account, environment = config["account_id"], config["environment"]
    role_prefix = f"arn:aws:iam::{account}:role/deepresearch-{environment}-"
    managed_prefix = f"arn:aws:iam::{account}:policy/deepresearch-{environment}-"
    role_arns = set()
    policy_arns = set()
    for item in changes.values():
        kind = item.get("type", "")
        before = item.get("change", {}).get("before") or {}
        if kind == "aws_iam_role":
            arn = before.get("arn")
            if not isinstance(arn, str) or not arn.startswith(role_prefix):
                raise Refused("Runtime IAM role inventory identity is outside the environment scope")
            role_arns.add(arn)
        elif kind == "aws_iam_role_policy":
            role = before.get("role")
            if not isinstance(role, str) or not re.fullmatch(rf"deepresearch-{re.escape(environment)}-[a-z0-9-]+", role):
                raise Refused("Runtime inline IAM policy is missing its exact environment role")
            role_arns.add(f"arn:aws:iam::{account}:role/{role}")
        elif kind == "aws_iam_role_policy_attachment":
            role, policy = before.get("role"), before.get("policy_arn")
            if not isinstance(role, str) or not re.fullmatch(rf"deepresearch-{re.escape(environment)}-[a-z0-9-]+", role):
                raise Refused("Runtime IAM attachment is missing its exact environment role")
            if not isinstance(policy, str) or not (
                policy.startswith("arn:aws:iam::aws:policy/")
                or policy.startswith(managed_prefix)
            ):
                raise Refused("Runtime IAM attachment policy is outside the reviewed AWS/environment scope")
            role_arns.add(f"arn:aws:iam::{account}:role/{role}")
            policy_arns.add(policy)
        elif kind == "aws_iam_policy":
            arn = before.get("arn")
            if not isinstance(arn, str) or not arn.startswith(managed_prefix):
                raise Refused("Runtime managed IAM policy inventory identity is outside the environment scope")
            policy_arns.add(arn)

    expiry_base = f"arn:aws:iam::{account}:role/deepresearch-{environment}-expiry-{config['session_id']}"
    role_arns.update({expiry_base, expiry_base + "-states", expiry_base + "-scheduler"})
    return sorted(role_arns), sorted(policy_arns)


def expiry_executor_policy(config, runtime_plan, runtime_state_bucket):
    """Compose backup, scoped runtime deletion, and post-shutdown inventory permissions.

    Deletion actions are emitted only for the exact addresses accepted by
    ``inspect_destroy``. Provider reads needed by saved-plan apply and the final
    inventory are read-only and region/resource scoped where supported.
    """
    validate_config(config)
    if not isinstance(runtime_state_bucket, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", runtime_state_bucket
    ):
        raise Refused("Expiry runtime state bucket is invalid")
    deletions = set(inspect_destroy(runtime_plan, config))
    changes = {
        item["address"]: item
        for item in runtime_plan.get("resource_changes", [])
        if item.get("mode") == "managed"
    }
    if not deletions or not deletions.issubset(changes):
        raise Refused("Expiry IAM policy requires the exact inspected runtime plan")

    policy = backup_policy(config)
    statements = policy["Statement"]
    account, region = config["account_id"], config["region"]
    state_object = f"arn:aws:s3:::{runtime_state_bucket}/deepresearch/{config['environment']}/runtime.tfstate"

    def allow(sid, actions, resources, condition=None):
        statement = {"Sid": sid, "Effect": "Allow", "Action": actions, "Resource": resources}
        if condition:
            statement["Condition"] = condition
        statements.append(statement)

    # The executor locks the exact runtime state while applying the saved plan.
    allow("MaintainExactRuntimeState", ["s3:GetObject", "s3:PutObject"], [state_object],
          {"Bool": {"aws:SecureTransport": "true"}})
    allow("MaintainExactRuntimeStateLock", ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
          [state_object + ".tflock"], {"Bool": {"aws:SecureTransport": "true"}})
    allow(
        "ListExactRuntimeStateAndLock",
        ["s3:ListBucket"],
        [f"arn:aws:s3:::{runtime_state_bucket}"],
        {"StringLike": {"s3:prefix": [
            f"deepresearch/{config['environment']}/runtime.tfstate",
            f"deepresearch/{config['environment']}/runtime.tfstate.tflock",
        ]}},
    )
    allow("VerifyExecutorAccount", ["sts:GetCallerIdentity"], ["*"])

    # Read-only provider refresh, retained-resource inventory, and ALB verification.
    regional_reads = {
        "ec2": ["ec2:DescribeAddresses", "ec2:DescribeInstances", "ec2:DescribeInternetGateways", "ec2:DescribeNatGateways", "ec2:DescribeNetworkInterfaces", "ec2:DescribeRouteTables", "ec2:DescribeSecurityGroupRules", "ec2:DescribeSecurityGroups", "ec2:DescribeSubnets", "ec2:DescribeVolumes", "ec2:DescribeVpcs"],
        "eks": ["eks:DescribeAddon", "eks:DescribeAccessEntry", "eks:DescribeCluster", "eks:DescribeNodegroup", "eks:DescribePodIdentityAssociation", "eks:ListClusters", "eks:ListTagsForResource"],
        "rds": ["rds:DescribeDBInstances", "rds:DescribeDBSubnetGroups", "rds:DescribeDBSnapshots", "rds:ListTagsForResource"],
        "logs": ["logs:DescribeLogGroups"],
        "autoscaling": ["autoscaling:DescribeAutoScalingGroups"],
        "elasticloadbalancing": ["elasticloadbalancing:DescribeLoadBalancers", "elasticloadbalancing:DescribeTags"],
        "ecr": ["ecr:DescribeRepositories"],
        "secretsmanager": ["secretsmanager:DescribeSecret"],
        "cognito-idp": ["cognito-idp:DescribeUserPool"],
        "sns": ["sns:GetTopicAttributes", "sns:ListTagsForResource"],
        "kms": ["kms:DescribeKey", "kms:GetKeyRotationStatus", "kms:ListResourceTags"],
        "sqs": ["sqs:GetQueueUrl", "sqs:GetQueueAttributes"],
        "dynamodb": ["dynamodb:DescribeTable", "dynamodb:ListTagsOfResource"],
        "codebuild": ["codebuild:BatchGetProjects"],
        "states": ["states:DescribeStateMachine"],
        "scheduler": ["scheduler:GetSchedule"],
        "cloudwatch": ["cloudwatch:DescribeAlarms"],
    }
    for service, actions in regional_reads.items():
        allow(
            "Read" + service.title().replace("-", ""),
            actions,
            ["*"],
            {"StringEquals": {"aws:RequestedRegion": region}},
        )
    role_arns, policy_arns = _iam_inventory_resources(changes, config)
    allow("ReadRuntimeIAMRoles", [
        "iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies",
        "iam:ListAttachedRolePolicies", "iam:ListRoleTags",
    ], role_arns)
    if policy_arns:
        allow("ReadRuntimeIAMPolicies", [
            "iam:GetPolicy", "iam:GetPolicyVersion", "iam:ListPolicyVersions",
        ], policy_arns)
    # IAM ListPolicies has no resource-level authorization. Inventory uses its
    # Local scope to detect any leftover project-owned policy after teardown.
    allow("ListLocalIAMPoliciesForResidualInventory", ["iam:ListPolicies"], ["*"])
    allow(
        "ReadHistoricalStagingVolumeReceipts",
        ["dynamodb:GetItem"],
        [f"arn:aws:dynamodb:{region}:{account}:table/deepresearch-control"],
        {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["completion#staging#*"]}, "Null": {"dynamodb:LeadingKeys": "false"}},
    )

    # A successful expiry writes its shutdown receipt in addition to the backup receipt.
    prefix = f"arn:aws:s3:::{config['backup_bucket']}/sessions/{config['environment']}/{config['session_id']}/receipts/shutdown.json"
    allow("WriteShutdownReceipt", ["s3:PutObject"], [prefix], {"StringEquals": {"s3:x-amz-server-side-encryption": "AES256"}})

    # RDS deletion protection is disabled after fresh backup/receipt checks and
    # immediately before applying the approved saved plan. Keep that mutation
    # bound to the exact configured database ARN.
    allow("DisableExactDatabaseDeletionProtection", ["rds:ModifyDBInstance"], [f"arn:aws:rds:{region}:{account}:db:{config['database_identifier']}"])

    for address in sorted(deletions):
        item = changes[address]
        kind = item["type"]
        actions = _DESTROY_ACTIONS.get(kind)
        if not actions:
            raise Refused("No expiry delete permission mapping for runtime resource: " + address)
        resource = _runtime_resource_arn(item, config)
        resources = resource if isinstance(resource, list) else [resource]
        allow("Delete" + str(len(statements)), actions, resources)

    return {"Version": "2012-10-17", "Statement": statements}
