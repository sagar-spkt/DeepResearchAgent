"""Pure IAM policy generation for the backup operation; never attaches permissions."""

from .core import Refused, validate_config


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
    allow("MaintainLeaseOnly", ["dynamodb:UpdateItem", "dynamodb:DeleteItem"], [table], {
        "ForAllValues:StringEquals": {"dynamodb:LeadingKeys": ["lifecycle-lock"]},
        "Null": {"dynamodb:LeadingKeys": "false"},
    })
    return {"Version": "2012-10-17", "Statement": statements}
