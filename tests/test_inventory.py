import copy
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lifecycle.control import ControlStore, Lease
from lifecycle.core import Refused, digest
from lifecycle.inventory import CLIENTS, InventoryCollector, release_admission, release_plan

CONFIG = {
    "account_id": "123456789012",
    "region": "us-east-1",
    "environment": "staging",
    "session_id": "session-1",
    "cluster_name": "deepresearch-staging",
    "database_identifier": "deepresearch-staging",
    "document_bucket": "deepresearch-staging-documents-123456789012",
    "backup_bucket": "deepresearch-staging-backups-123456789012",
    "trace_bucket": "deepresearch-staging-traces-123456789012",
    "user_pool_id": "us-east-1_pool123",
    "alert_topic_arn": "arn:aws:sns:us-east-1:123456789012:deepresearch-staging-alerts",
    "alert_kms_key_arn": "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-4111-8111-111111111111",
    "github_runner_enabled": False,
}
TAGS = {"Project": "deepresearch", "Environment": "staging", "Session": "session-1"}
KEYS = {
    "describe_vpcs": "Vpcs",
    "describe_subnets": "Subnets",
    "describe_network_interfaces": "NetworkInterfaces",
    "describe_nat_gateways": "NatGateways",
    "describe_instances": "Reservations",
    "describe_auto_scaling_groups": "AutoScalingGroups",
    "list_clusters": "clusters",
    "describe_db_instances": "DBInstances",
    "describe_volumes": "Volumes",
    "describe_db_snapshots": "DBSnapshots",
    "describe_load_balancers": "LoadBalancers",
    "describe_log_groups": "logGroups",
    "list_policies": "Policies",
}


class ProviderError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


def arn(service, resource):
    return f"arn:aws:{service}:{'' if service == 'iam' else CONFIG['region']}:{CONFIG['account_id']}:{resource}"


class FakeClient:
    def __init__(self, service, fixtures):
        self.service, self.fixtures = service, fixtures
        self.meta = SimpleNamespace(region_name=CONFIG["region"])

    def get_paginator(self, operation):
        def paginate(**kwargs):
            self.fixtures["calls"].append((self.service, operation, kwargs))
            failure = self.fixtures["errors"].get((self.service, operation))
            if failure:
                raise ProviderError(failure)
            values = self.fixtures["pages"].get((self.service, operation), [])
            if (
                operation == "describe_db_snapshots"
                and (self.service, operation) not in self.fixtures["pages"]
            ):
                values = [
                    {
                        "DBSnapshotIdentifier": CONFIG["database_identifier"]
                        + "-session-1-"
                        + suffix,
                        "DBInstanceIdentifier": CONFIG["database_identifier"],
                        "DBSnapshotArn": arn(
                            "rds",
                            "snapshot:" + CONFIG["database_identifier"] + "-session-1-" + suffix,
                        ),
                        "Status": "available",
                        "Encrypted": True,
                    }
                    for suffix in ("backup", "final")
                ]
            midpoint = len(values) // 2
            yield {KEYS[operation]: values[:midpoint]}
            yield {KEYS[operation]: values[midpoint:]}

        return SimpleNamespace(paginate=paginate)

    def __getattr__(self, operation):
        def call(**kwargs):
            f = self.fixtures
            f["calls"].append((self.service, operation, kwargs))
            if (self.service, operation) in f["errors"]:
                raise ProviderError(f["errors"][(self.service, operation)])
            if (self.service, operation) in f["direct"]:
                value = f["direct"][(self.service, operation)]
                return value(kwargs) if callable(value) else copy.deepcopy(value)
            if operation == "get_caller_identity":
                return {"Account": CONFIG["account_id"]}
            if operation == "describe_alarms":
                return {"MetricAlarms": []}
            if operation == "describe_table":
                return {
                    "Table": {
                        "TableArn": arn("dynamodb", "table/deepresearch-control"),
                        "TableStatus": "ACTIVE",
                    }
                }
            if operation == "list_tags_of_resource":
                return {
                    "Tags": [
                        {"Key": "Project", "Value": "deepresearch"},
                        {"Key": "Component", "Value": "control"},
                    ]
                }
            if operation == "list_tags_for_resource":
                return {"Tags": [{"Key": k, "Value": v} for k, v in TAGS.items()]}
            if operation == "get_item":
                return {}
            if operation == "describe_cluster":
                raise ProviderError("ResourceNotFoundException")
            if operation == "describe_db_instances":
                raise ProviderError("DBInstanceNotFound")
            if operation == "get_role":
                raise ProviderError("NoSuchEntity")
            if operation == "describe_addresses":
                return {"Addresses": []}
            if operation == "get_bucket_tagging":
                return {"TagSet": [{"Key": k, "Value": v} for k, v in TAGS.items()]}
            if operation == "get_bucket_versioning":
                return {"Status": "Enabled"}
            if operation == "get_bucket_encryption":
                return {
                    "ServerSideEncryptionConfiguration": {
                        "Rules": [
                            {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                        ]
                    }
                }
            if operation == "get_public_access_block":
                return {
                    "PublicAccessBlockConfiguration": {
                        key: True
                        for key in (
                            "BlockPublicAcls",
                            "IgnorePublicAcls",
                            "BlockPublicPolicy",
                            "RestrictPublicBuckets",
                        )
                    }
                }
            if operation == "describe_user_pool":
                return {
                    "UserPool": {
                        "Arn": arn("cognito-idp", "userpool/" + CONFIG["user_pool_id"]),
                        "UserPoolTags": TAGS,
                    }
                }
            if operation == "describe_secret":
                return {
                    "ARN": arn("secretsmanager", "secret:" + kwargs["SecretId"] + "-random"),
                    "Name": kwargs["SecretId"],
                    "Tags": [{"Key": k, "Value": v} for k, v in TAGS.items()],
                }
            if operation == "describe_repositories":
                return {
                    "repositories": [
                        {"repositoryArn": arn("ecr", "repository/" + kwargs["repositoryNames"][0])}
                    ]
                }
            if operation == "get_queue_url":
                return {
                    "QueueUrl": "https://sqs.us-east-1.amazonaws.com/123456789012/"
                    + kwargs["QueueName"]
                }
            if operation == "get_queue_attributes":
                return {
                    "Attributes": {"QueueArn": arn("sqs", kwargs["QueueUrl"].rsplit("/", 1)[1])}
                }
            if operation == "get_topic_attributes":
                return {
                    "Attributes": {
                        "Owner": CONFIG["account_id"],
                        "KmsMasterKeyId": CONFIG["alert_kms_key_arn"],
                    }
                }
            if operation == "describe_key":
                return {
                    "KeyMetadata": {
                        "Arn": CONFIG["alert_kms_key_arn"],
                        "KeyId": CONFIG["alert_kms_key_arn"].rsplit("/", 1)[1],
                        "KeyState": "Enabled",
                        "KeyManager": "CUSTOMER",
                        "KeyUsage": "ENCRYPT_DECRYPT",
                        "KeySpec": "SYMMETRIC_DEFAULT",
                        "Description": "Encryption key for deepresearch-staging expiry alert topic",
                    }
                }
            if operation == "get_key_rotation_status":
                return {"KeyRotationEnabled": True}
            if operation == "list_resource_tags":
                return {
                    "Tags": [
                        {"TagKey": "Project", "TagValue": "deepresearch"},
                        {"TagKey": "Environment", "TagValue": "staging"},
                        {"TagKey": "Component", "TagValue": "expiry-alerts"},
                    ]
                }
            if operation == "batch_get_projects":
                return {"projects": [], "projectsNotFound": kwargs["names"]}
            if operation == "describe_state_machine":
                raise ProviderError("StateMachineDoesNotExist")
            if operation == "get_schedule":
                raise ProviderError("ResourceNotFoundException")
            raise AssertionError("Unexpected provider operation " + self.service + ":" + operation)

        return call


def fixture_clients():
    fixtures = {"pages": {}, "errors": {}, "direct": {}, "calls": []}
    return {service: FakeClient(service, fixtures) for service in CLIENTS}, fixtures


class FakeControlDB:
    def __init__(self):
        self.items = {
            "active-session": {
                "key": {"S": "active-session"},
                **{k: {"S": CONFIG[k]} for k in ("account_id", "environment", "session_id")},
            },
            "lifecycle-lock": {
                "key": {"S": "lifecycle-lock"},
                "owner_id": {"S": "holder"},
                "session_id": {"S": "session-1"},
                "expires_at": {"N": "100120"},
            },
            "month#2026-11": {"reserved_seconds": {"N": "21600"}},
        }
        self.lock = threading.Lock()
        self.transactions = []

    def describe_table(self, **_):
        return {
            "Table": {
                "TableArn": arn("dynamodb", "table/deepresearch-control"),
                "TableStatus": "ACTIVE",
            }
        }

    def list_tags_of_resource(self, **_):
        return {
            "Tags": [
                {"Key": "Project", "Value": "deepresearch"},
                {"Key": "Component", "Value": "control"},
            ]
        }

    def get_item(self, **kwargs):
        with self.lock:
            return {"Item": copy.deepcopy(self.items.get(kwargs["Key"]["key"]["S"], {}))}

    def transact_write_items(self, **kwargs):
        with self.lock:
            pending = copy.deepcopy(self.items)
            for action in kwargs["TransactItems"]:
                if "ConditionCheck" in action:
                    values = action["ConditionCheck"]["ExpressionAttributeValues"]
                    item = pending.get("lifecycle-lock", {})
                    if (
                        item.get("owner_id") != values[":owner"]
                        or item.get("session_id") != values[":session"]
                        or int(item.get("expires_at", {}).get("N", "0")) <= int(values[":now"]["N"])
                    ):
                        raise ProviderError("TransactionCanceledException")
                elif "Put" in action:
                    item = action["Put"]["Item"]
                    key = item["key"]["S"]
                    if key in pending:
                        raise ProviderError("TransactionCanceledException")
                    pending[key] = copy.deepcopy(item)
                elif "Delete" in action:
                    values = action["Delete"]["ExpressionAttributeValues"]
                    item = pending.get("active-session", {})
                    if any(
                        item.get(k) != values[v]
                        for k, v in (
                            ("account_id", ":account"),
                            ("environment", ":environment"),
                            ("session_id", ":session"),
                        )
                    ):
                        raise ProviderError("TransactionCanceledException")
                    del pending["active-session"]
            self.items = pending
            self.transactions.append(copy.deepcopy(kwargs))


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.clients, self.fixtures = fixture_clients()

    def collect(self, config=CONFIG):
        return InventoryCollector(config, self.clients, clock=lambda: 100000).collect()

    def test_leftover_administrator_or_backup_role_blocks_runtime_completion(self):
        for suffix in ("cluster-admin", "clickhouse-backup"):
            with self.subTest(role=suffix):
                name = "deepresearch-staging-" + suffix

                def role(kwargs):
                    if kwargs["RoleName"] != name:
                        raise ProviderError("NoSuchEntity")
                    return {
                        "Role": {
                            "RoleName": name,
                            "Arn": arn("iam", "role/" + name),
                            "Tags": [{"Key": k, "Value": v} for k, v in TAGS.items()],
                        }
                    }

                self.fixtures["direct"][("iam", "get_role")] = role
                result = self.collect()
                self.assertFalse(result["runtime_gone"])
                self.assertIn({"kind": "iam-role", "id": name}, result["runtime_remaining"])

    def test_clean_provider_inventory_accounts_retention_without_zero_cost_claim(self):
        result = self.collect()
        self.assertTrue(result["runtime_gone"], result["errors"])
        self.assertTrue(result["inventory_complete"])
        self.assertTrue(result["charges_may_continue"])
        self.assertEqual(
            {
                "rds-snapshot",
                "s3-bucket",
                "cognito-user-pool",
                "secret-container",
                "ecr-repository",
                "sqs-queue",
                "control-table",
                "expiry-dead-letter-queue",
                "sns-topic",
                "kms-key",
            },
            {r["kind"] for r in result["retained_resources"]},
        )
        s3 = [kwargs for service, op, kwargs in self.fixtures["calls"] if service == "s3"]
        self.assertTrue(
            s3 and all(value["ExpectedBucketOwner"] == CONFIG["account_id"] for value in s3)
        )

    def test_foundation_inventory_accounts_encrypted_environment_alert_resources(self):
        result = self.collect()

        self.assertTrue(result["inventory_complete"], result["errors"])
        self.assertIn(
            {
                "kind": "sns-topic",
                "id": arn("sns", "deepresearch-staging-alerts"),
                "billed": True,
            },
            result["retained_resources"],
        )
        self.assertIn(
            {"kind": "kms-key", "id": CONFIG["alert_kms_key_arn"], "billed": True},
            result["retained_resources"],
        )
        self.assertIn(
            (
                "sns",
                "get_topic_attributes",
                {"TopicArn": CONFIG["alert_topic_arn"]},
            ),
            self.fixtures["calls"],
        )
        self.assertIn(
            (
                "sns",
                "list_tags_for_resource",
                {"ResourceArn": CONFIG["alert_topic_arn"]},
            ),
            self.fixtures["calls"],
        )
        self.assertNotIn("list_topics", {op for _service, op, _kwargs in self.fixtures["calls"]})

    def test_runner_token_key_blocks_closeout_until_scheduled_for_deletion(self):
        runner_key = "arn:aws:kms:us-east-1:123456789012:key/22222222-2222-4222-8222-222222222222"
        config = {**CONFIG, "github_runner_enabled": True, "runner_token_kms_key_arn": runner_key}
        runner_state = ["PendingDeletion"]

        def describe_key(kwargs):
            if kwargs["KeyId"] == runner_key:
                return {
                    "KeyMetadata": {
                        "Arn": runner_key,
                        "KeyId": runner_key.rsplit("/", 1)[1],
                        "KeyState": runner_state[0],
                        "KeyManager": "CUSTOMER",
                        "KeyUsage": "ENCRYPT_DECRYPT",
                        "Description": "One-time GitHub runner registration token for session-1",
                        "DeletionDate": datetime(2030, 1, 1, tzinfo=timezone.utc),
                    }
                }
            return {
                "KeyMetadata": {
                    "Arn": CONFIG["alert_kms_key_arn"],
                    "KeyId": CONFIG["alert_kms_key_arn"].rsplit("/", 1)[1],
                    "KeyState": "Enabled",
                    "KeyManager": "CUSTOMER",
                    "KeyUsage": "ENCRYPT_DECRYPT",
                    "KeySpec": "SYMMETRIC_DEFAULT",
                    "Description": "Encryption key for deepresearch-staging expiry alert topic",
                }
            }

        self.fixtures["direct"][("kms", "describe_key")] = describe_key
        self.fixtures["direct"][("kms", "list_resource_tags")] = lambda kwargs: {
            "Tags": [
                {"TagKey": key, "TagValue": value}
                for key, value in (
                    {**TAGS, "Component": "github-runner-token-key"}
                    if kwargs["KeyId"] == runner_key
                    else {**TAGS, "Component": "expiry-alerts"}
                ).items()
            ]
        }
        pending = self.collect(config)
        self.assertTrue(pending["inventory_complete"], pending["errors"])
        self.assertTrue(pending["management_gone"], pending["management_remaining"])
        self.assertIn(
            {
                "kind": "kms-key-pending-deletion",
                "id": runner_key,
                "billed": True,
                "deletion_date": "2030-01-01T00:00:00+00:00",
            },
            pending["retained_resources"],
        )

        runner_state[0] = "Enabled"
        active = self.collect(config)
        self.assertFalse(active["management_gone"])
        self.assertIn(
            {"kind": "runner-token-kms-key", "id": runner_key, "state": "Enabled"},
            active["management_remaining"],
        )

    def test_unencrypted_or_invalid_alert_key_blocks_inventory_completion(self):
        self.fixtures["direct"][("sns", "get_topic_attributes")] = {
            "Attributes": {"Owner": CONFIG["account_id"]}
        }

        result = self.collect()

        self.assertFalse(result["inventory_complete"])
        self.assertIn("retained-sns-kms-binding", {item["check"] for item in result["errors"]})

    def test_alert_key_alias_binding_blocks_inventory_completion(self):
        key_metadata = {
            "Arn": CONFIG["alert_kms_key_arn"],
            "KeyId": CONFIG["alert_kms_key_arn"].rsplit("/", 1)[1],
            "KeyState": "Enabled",
            "KeyManager": "CUSTOMER",
            "KeyUsage": "ENCRYPT_DECRYPT",
            "KeySpec": "SYMMETRIC_DEFAULT",
            "Description": "Encryption key for deepresearch-staging expiry alert topic",
        }
        self.fixtures["direct"][("sns", "get_topic_attributes")] = {
            "Attributes": {
                "Owner": CONFIG["account_id"],
                "KmsMasterKeyId": "alias/deepresearch-staging-alerts",
            }
        }
        self.fixtures["direct"][("kms", "describe_key")] = lambda kwargs: {
            "KeyMetadata": (
                {
                    **key_metadata,
                    "Arn": "arn:aws:kms:us-east-1:123456789012:key/22222222-2222-4222-8222-222222222222",
                    "KeyId": "22222222-2222-4222-8222-222222222222",
                }
                if kwargs["KeyId"] == "alias/deepresearch-staging-alerts"
                else key_metadata
            )
        }

        result = self.collect()

        self.assertFalse(result["inventory_complete"])
        self.assertIn("retained-sns-kms-binding", {item["check"] for item in result["errors"]})

    def test_expiry_inventory_accounts_scheduler_dead_letter_alarm(self):
        name = "deepresearch-staging-expiry-session-1"
        expected_names = [name + "-failed", name + "-scheduler-dlq-visible"]
        requested_names = []
        self.fixtures["direct"][("cloudwatch", "describe_alarms")] = lambda kwargs: (
            requested_names.append(kwargs["AlarmNames"])
            or {
                "MetricAlarms": [
                    {
                        "AlarmName": alarm_name,
                        "AlarmArn": arn("cloudwatch", "alarm:" + alarm_name),
                    }
                    for alarm_name in expected_names
                ]
            }
        )

        result = self.collect()

        self.assertTrue(result["inventory_complete"], result["errors"])
        self.assertEqual([expected_names], requested_names)
        retained = {
            row["id"] for row in result["retained_resources"] if row["kind"] == "expiry-alarm"
        }
        self.assertEqual(
            {arn("cloudwatch", "alarm:" + alarm_name) for alarm_name in expected_names}, retained
        )

    def test_access_denied_is_not_absence(self):
        for service, operation in (
            ("eks", "describe_cluster"),
            ("rds", "describe_db_instances"),
            ("ec2", "describe_instances"),
        ):
            with self.subTest(service=service):
                self.fixtures["errors"][(service, operation)] = "AccessDeniedException"
                result = self.collect()
                self.assertFalse(result["inventory_complete"])
                self.assertFalse(result["runtime_gone"])
                del self.fixtures["errors"][(service, operation)]

    def test_active_instance_found_without_project_tags_by_cluster_membership(self):
        self.fixtures["pages"][("ec2", "describe_instances")] = [
            {
                "Instances": [
                    {
                        "InstanceId": "i-123",
                        "State": {"Name": "running"},
                        "Tags": [{"Key": "eks:cluster-name", "Value": CONFIG["cluster_name"]}],
                    }
                ]
            }
        ]
        result = self.collect()
        self.assertFalse(result["runtime_gone"])
        self.assertIn("ec2-instance", {row["kind"] for row in result["runtime_remaining"]})

    def test_cross_account_and_region_fail_before_scope_claim(self):
        self.fixtures["direct"][("sts", "get_caller_identity")] = {"Account": "999999999999"}
        self.assertFalse(self.collect()["inventory_complete"])
        del self.fixtures["direct"][("sts", "get_caller_identity")]
        self.clients["ec2"].meta.region_name = "us-west-2"
        self.assertFalse(self.collect()["inventory_complete"])

    def test_other_environment_project_vpc_is_not_ignored_or_deleted(self):
        self.fixtures["pages"][("ec2", "describe_vpcs")] = [
            {
                "VpcId": "vpc-other",
                "OwnerId": CONFIG["account_id"],
                "Tags": [
                    {"Key": k, "Value": v} for k, v in {**TAGS, "Environment": "production"}.items()
                ],
            }
        ]
        result = self.collect()
        self.assertFalse(result["runtime_gone"])
        self.assertFalse(result["inventory_complete"])
        self.assertTrue(any("vpc:" in error["check"] for error in result["errors"]))

    def test_unknown_csi_and_elastic_ip_block_completion(self):
        self.fixtures["pages"][("ec2", "describe_volumes")] = [
            {
                "VolumeId": "vol-unknown",
                "Tags": [{"Key": "ebs.csi.aws.com/cluster", "Value": "true"}],
            }
        ]
        self.fixtures["direct"][("ec2", "describe_addresses")] = {
            "Addresses": [{"AllocationId": "eipalloc-unknown"}]
        }
        result = self.collect()
        self.assertFalse(result["inventory_complete"])
        self.assertEqual(
            {"UnscopedCSIVolume", "UnscopedUnassociatedAddress"},
            {e["error"] for e in result["errors"]},
        )

    def test_intentionally_retained_available_csi_is_accounted(self):
        self.fixtures["pages"][("ec2", "describe_volumes")] = [
            {
                "VolumeId": "vol-data",
                "State": "available",
                "Attachments": [],
                "Tags": [
                    {"Key": k, "Value": v}
                    for k, v in {**TAGS, "Retention": "data", "Component": "clickhouse"}.items()
                ],
            }
        ]
        result = self.collect()
        self.assertTrue(result["runtime_gone"], result["errors"])
        self.assertIn("ebs-data-volume", {row["kind"] for row in result["retained_resources"]})

    def test_unassociated_eip_with_unrelated_tags_is_still_unknown(self):
        self.fixtures["direct"][("ec2", "describe_addresses")] = {
            "Addresses": [
                {
                    "AllocationId": "eipalloc-unknown",
                    "Tags": [{"Key": "Name", "Value": "unknown-resource"}],
                }
            ]
        }
        result = self.collect()
        self.assertFalse(result["runtime_gone"])
        self.assertIn("UnscopedUnassociatedAddress", {e["error"] for e in result["errors"]})

    def test_retained_volume_requires_explicit_empty_attachment_list(self):
        volume = {
            "VolumeId": "vol-data",
            "State": "available",
            "Tags": [
                {"Key": k, "Value": v}
                for k, v in {**TAGS, "Retention": "data", "Component": "clickhouse"}.items()
            ],
        }
        for missing in (True, False):
            with self.subTest(missing=missing):
                if not missing:
                    volume["Attachments"] = None
                self.fixtures["pages"][("ec2", "describe_volumes")] = [volume]
                result = self.collect()
                self.assertFalse(result["runtime_gone"])
                self.assertNotIn(
                    "ebs-data-volume", {row["kind"] for row in result["retained_resources"]}
                )

    def test_missing_final_snapshot_blocks_release_evidence(self):
        self.fixtures["pages"][("rds", "describe_db_snapshots")] = []
        self.assertFalse(self.collect()["retained_accounted"])

    def test_historical_volume_requires_retained_completion_not_only_tags(self):
        value = {**TAGS, "Session": "previous", "Retention": "data", "Component": "valkey"}
        self.fixtures["pages"][("ec2", "describe_volumes")] = [
            {
                "VolumeId": "vol-old",
                "State": "available",
                "Attachments": [],
                "Tags": [{"Key": k, "Value": v} for k, v in value.items()],
            }
        ]
        self.assertFalse(self.collect()["runtime_gone"])
        import json

        receipt = {
            "operation": "session-completion",
            "config": {**CONFIG, "session_id": "previous"},
            "time_refunded": False,
            "retained_resources": [{"kind": "ebs-data-volume", "id": "vol-old"}],
        }
        self.fixtures["direct"][("dynamodb", "get_item")] = {
            "Item": {"receipt_json": {"S": json.dumps(receipt)}}
        }
        result = self.collect()
        self.assertTrue(result["runtime_gone"], result["errors"])
        self.assertEqual(
            "previous",
            next(v for v in result["retained_resources"] if v["id"] == "vol-old")["session_id"],
        )

    def test_other_environment_controlplane_without_vpc_blocks_release(self):
        self.fixtures["pages"][("eks", "list_clusters")] = ["deepresearch-production"]

        def describe(kwargs):
            if kwargs["name"] == CONFIG["cluster_name"]:
                raise ProviderError("ResourceNotFoundException")
            return {
                "cluster": {
                    "arn": arn("eks", "cluster/deepresearch-production"),
                    "tags": {"Project": "deepresearch", "Environment": "production"},
                    "status": "ACTIVE",
                }
            }

        self.fixtures["direct"][("eks", "describe_cluster")] = describe
        result = self.collect()
        self.assertFalse(result["runtime_gone"])
        self.assertIn("OtherProjectRuntimePresent", {e["error"] for e in result["errors"]})

    def test_other_environment_rds_without_vpc_blocks_release(self):
        self.fixtures["pages"][("rds", "describe_db_instances")] = [
            {
                "DBInstanceIdentifier": "deepresearch-production",
                "DBInstanceArn": arn("rds", "db:deepresearch-production"),
                "DBInstanceStatus": "available",
            }
        ]
        self.fixtures["direct"][("rds", "list_tags_for_resource")] = {
            "TagList": [
                {"Key": "Project", "Value": "deepresearch"},
                {"Key": "Environment", "Value": "production"},
            ]
        }
        self.assertFalse(self.collect()["runtime_gone"])

    def test_unsupported_api_is_unknown_not_absence(self):
        self.fixtures["errors"][("logs", "describe_log_groups")] = "OperationNotPageableError"
        result = self.collect()
        self.assertFalse(result["inventory_complete"])
        self.assertIn("OperationNotPageableError", {e["error"] for e in result["errors"]})

    def test_malformed_optional_responses_never_mean_resource_absence(self):
        for service, operation in (
            ("codebuild", "batch_get_projects"),
            ("stepfunctions", "describe_state_machine"),
            ("scheduler", "get_schedule"),
            ("sqs", "get_queue_url"),
            ("cloudwatch", "describe_alarms"),
            ("sns", "list_tags_for_resource"),
            ("sns", "get_topic_attributes"),
            ("kms", "describe_key"),
            ("kms", "get_key_rotation_status"),
            ("kms", "list_resource_tags"),
        ):
            with self.subTest(service=service):
                self.fixtures["direct"][(service, operation)] = {}
                self.assertFalse(self.collect()["inventory_complete"])
                del self.fixtures["direct"][(service, operation)]
        self.fixtures["direct"][("codebuild", "batch_get_projects")] = {"projects": []}
        self.assertFalse(self.collect()["runtime_gone"])

    def test_missing_foundation_alert_topic_blocks_completion(self):
        self.fixtures["errors"][("sns", "get_topic_attributes")] = "NotFoundException"

        result = self.collect()

        self.assertFalse(result["inventory_complete"])
        self.assertFalse(result["retained_accounted"])
        self.assertIn("sns:topic-attributes", {item["check"] for item in result["errors"]})


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.clients, self.fixtures = fixture_clients()
        self.db = FakeControlDB()
        self.clock = [100000]
        self.store = ControlStore(self.db, clock=lambda: self.clock[0])
        self.lease = Lease(self.store, CONFIG, "holder", 120)

    def release(self):
        plan = release_plan(CONFIG, datetime.fromtimestamp(self.clock[0], timezone.utc))
        return release_admission(self.store, CONFIG, self.lease, self.clients, plan, digest(plan))

    def test_release_collects_fresh_provider_truth_archives_and_never_refunds(self):
        result = self.release()
        self.assertTrue(result["admission_released"])
        self.assertNotIn("active-session", self.db.items)
        self.assertIn("completion#staging#session-1", self.db.items)
        self.assertEqual("21600", self.db.items["month#2026-11"]["reserved_seconds"]["N"])
        self.assertTrue(self.fixtures["calls"])

    def bootstrap_evidence(self, directory):
        import hashlib
        from pathlib import Path

        saved_plan = Path(directory) / "bootstrap.tfplan"
        saved_plan.write_bytes(b"reviewed bootstrap cleanup plan")
        cleanup_plan = {
            "operation": "bootstrap-cleanup",
            "config": CONFIG,
            "saved_plan": str(saved_plan),
            "saved_plan_sha256": hashlib.sha256(saved_plan.read_bytes()).hexdigest(),
            "deletions": ["aws_eks_cluster.main", "aws_db_instance.main"],
            "preconditions": [
                "first staging provision only; application release has not been installed",
                "research namespace must be absent",
                "RDS final snapshot is retained; foundation and management network are outside this plan",
            ],
        }
        receipt = {
            "operation": "bootstrap-cleanup-receipt",
            "config": CONFIG,
            "plan_hash": digest(cleanup_plan),
            "executed": True,
            "remaining_resources": [],
            "retained_data": True,
        }
        return cleanup_plan, receipt

    def test_bootstrap_release_uses_final_snapshot_without_claiming_prebackup(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            cleanup_plan, receipt = self.bootstrap_evidence(directory)
            final_id = CONFIG["database_identifier"] + "-" + CONFIG["session_id"] + "-final"
            self.fixtures["pages"][("rds", "describe_db_snapshots")] = [
                {
                    "DBSnapshotIdentifier": final_id,
                    "DBInstanceIdentifier": CONFIG["database_identifier"],
                    "DBSnapshotArn": arn("rds", "snapshot:" + final_id),
                    "Status": "available",
                    "Encrypted": True,
                }
            ]
            plan = release_plan(
                CONFIG,
                datetime.fromtimestamp(self.clock[0], timezone.utc),
                cleanup_plan,
                receipt,
            )
            result = release_admission(
                self.store,
                CONFIG,
                self.lease,
                self.clients,
                plan,
                digest(plan),
                cleanup_plan=cleanup_plan,
                cleanup_receipt=receipt,
            )

        self.assertEqual("bootstrap-release", plan["operation"])
        self.assertTrue(result["admission_released"])
        self.assertEqual("first-provision-bootstrap-cleanup", result["closeout_mode"])
        self.assertNotIn("active-session", self.db.items)
        self.assertEqual(
            [final_id],
            [item["id"].rsplit(":", 1)[-1] for item in result["inventory"]["retained_resources"] if item["kind"] == "rds-snapshot"],
        )

    def test_bootstrap_release_still_requires_management_resources_gone(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            cleanup_plan, receipt = self.bootstrap_evidence(directory)
            plan = release_plan(
                CONFIG,
                datetime.fromtimestamp(self.clock[0], timezone.utc),
                cleanup_plan,
                receipt,
            )
            self.fixtures["pages"][("ec2", "describe_vpcs")] = [
                {
                    "VpcId": "vpc-management",
                    "State": "available",
                    "CidrBlock": "10.61.0.0/16",
                    "Tags": [
                        {"Key": "Project", "Value": "deepresearch"},
                        {"Key": "Environment", "Value": "staging"},
                        {"Key": "Session", "Value": "session-1"},
                        {"Key": "ManagedBy", "Value": "terraform"},
                    ],
                }
            ]
            with self.assertRaises(Refused):
                release_admission(
                    self.store,
                    CONFIG,
                    self.lease,
                    self.clients,
                    plan,
                    digest(plan),
                    cleanup_plan=cleanup_plan,
                    cleanup_receipt=receipt,
                )
        self.assertIn("active-session", self.db.items)

    def test_bootstrap_release_rejects_receipt_for_different_cleanup_plan(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            cleanup_plan, receipt = self.bootstrap_evidence(directory)
            receipt["plan_hash"] = "0" * 64
            with self.assertRaises(Refused):
                release_plan(
                    CONFIG,
                    datetime.fromtimestamp(self.clock[0], timezone.utc),
                    cleanup_plan,
                    receipt,
                )

    def test_bootstrap_release_rejects_cleanup_plan_without_no_writer_precondition(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            cleanup_plan, receipt = self.bootstrap_evidence(directory)
            cleanup_plan["preconditions"].remove(
                "first staging provision only; application release has not been installed"
            )
            receipt["plan_hash"] = digest(cleanup_plan)
            with self.assertRaises(Refused):
                release_plan(
                    CONFIG,
                    datetime.fromtimestamp(self.clock[0], timezone.utc),
                    cleanup_plan,
                    receipt,
                )

    def test_active_or_unavailable_provider_never_releases(self):
        self.fixtures["errors"][("eks", "describe_cluster")] = "AccessDeniedException"
        with self.assertRaises(Refused):
            self.release()
        self.assertIn("active-session", self.db.items)
        self.assertEqual([], self.db.transactions)

    def test_lost_lease_and_wrong_admission_fail_closed(self):
        self.db.items["lifecycle-lock"]["owner_id"] = {"S": "replacement"}
        with self.assertRaises(Refused):
            self.release()
        self.assertEqual([], self.db.transactions)
        self.db.items["lifecycle-lock"]["owner_id"] = {"S": "holder"}
        self.db.items["active-session"]["environment"] = {"S": "production"}
        with self.assertRaises(Refused):
            self.release()
        self.assertIn("active-session", self.db.items)

    def test_unapproved_release_fails_before_provider_collection(self):
        plan = release_plan(CONFIG, datetime.fromtimestamp(self.clock[0], timezone.utc))
        with self.assertRaises(Refused):
            release_admission(self.store, CONFIG, self.lease, self.clients, plan, "wrong")
        self.assertEqual([], self.fixtures["calls"])
        self.assertEqual([], self.db.transactions)

    def test_lease_lost_during_provider_collection_retains_admission(self):
        def lose_lease(_):
            self.db.items["lifecycle-lock"]["owner_id"] = {"S": "replacement"}
            return {"Account": CONFIG["account_id"]}

        self.fixtures["direct"][("sts", "get_caller_identity")] = lose_lease
        with self.assertRaises(Refused):
            self.release()
        self.assertIn("active-session", self.db.items)
        self.assertEqual([], self.db.transactions)

    def test_atomic_condition_blocks_lease_replacement_after_collection(self):
        transact = self.db.transact_write_items

        def lose_lease(**kwargs):
            self.db.items["lifecycle-lock"]["owner_id"] = {"S": "replacement"}
            return transact(**kwargs)

        self.db.transact_write_items = lose_lease
        with self.assertRaises(Refused):
            self.release()
        self.assertIn("active-session", self.db.items)
        self.assertNotIn("completion#staging#session-1", self.db.items)
        self.assertEqual("21600", self.db.items["month#2026-11"]["reserved_seconds"]["N"])

    def test_simultaneous_release_has_one_completion(self):
        def attempt(_):
            try:
                return self.release()["admission_released"]
            except Refused:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(1, sum(results))
        self.assertEqual(1, len(self.db.transactions))


if __name__ == "__main__":
    unittest.main()
