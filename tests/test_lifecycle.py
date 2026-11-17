import hashlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lifecycle.aws_adapter import AWSAdapter
from lifecycle.core import (
    Refused,
    approve,
    backup_execute,
    backup_plan,
    digest,
    execute_expiry,
    inspect_destroy,
    prepare_expiry,
    prepare_shutdown,
    restore_prepare,
    shutdown_execute,
)

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
    "operator_principal_arn": "arn:aws:iam::123456789012:user/research-operator",
}
RUNTIME_STATE_BUCKET = "deepresearch-terraform-state-123456789012-us-east-1"


def runtime_backend_file(root, bucket=RUNTIME_STATE_BUCKET):
    path = root / "artifacts/runtime.backend.hcl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'bucket = "{bucket}"\n'
        'key = "deepresearch/staging/runtime.tfstate"\n'
        'region = "us-east-1"\n'
        "encrypt = true\n"
        "use_lockfile = true\n"
    )
    return "artifacts/runtime.backend.hcl"


class FakeAdapter:
    def __init__(self):
        self.calls = []

    def verify_scope(self, c):
        self.calls.append("scope")

    def quiesce(self):
        self.calls.append("quiesce")
        return {"verified": True}

    def backup_clickhouse(self, p):
        self.calls.append("clickhouse")
        return {"url": "s3://backup", "id": "1"}

    def backup_valkey(self, p):
        self.calls.append("valkey")
        return {"sha256": "a" * 64}

    def snapshot_database(self, i):
        self.calls.append("snapshot")
        return {"arn": "arn:snapshot", "identifier": i}

    def inventory(self):
        self.calls.append("inventory")
        return []

    def verify_receipt(self, r, **kw):
        self.calls.append("receipt")

    def verify_quiesced(self, expected=None):
        self.calls.append("still-quiesced")

    def remove_ingress(self):
        self.calls.append("ingress")

    def disable_database_protection(self):
        self.calls.append("unprotect")

    def publish_receipt(self, kind, value):
        self.calls.append("publish-" + kind)
        return {"version_id": "v1"}

    def remaining_inventory(self):
        self.calls.append("inventory-after")
        return {"inventory_complete": False}


def tfplan(kind="aws_vpc"):
    return {
        "variables": {
            k: {"value": CONFIG[k]} for k in ["region", "environment", "session_id", "account_id"]
        },
        "configuration": {
            "provider_config": {
                "aws": {
                    "name": "aws",
                    "full_name": "registry.terraform.io/hashicorp/aws",
                    "expressions": {
                        "region": {"references": ["var.region"]},
                        "allowed_account_ids": {"references": ["var.account_id"]},
                    },
                }
            },
            "root_module": {
                "resources": [
                    {
                        "address": kind + ".main",
                        "type": kind,
                        "mode": "managed",
                        "provider_config_key": "aws",
                    }
                ]
            },
        },
        "resource_changes": [
            {
                "address": kind + ".main",
                "type": kind,
                "mode": "managed",
                "provider_name": "registry.terraform.io/hashicorp/aws",
                "change": {
                    "actions": ["delete"],
                    "before": {
                        "id": "vpc-0123456789abcdef0",
                        "owner_id": CONFIG["account_id"],
                        "cidr_block": "10.60.0.0/16",
                        "tags_all": {
                            "Project": "deepresearch",
                            "Environment": "staging",
                            "Session": CONFIG["session_id"],
                        },
                    },
                },
            }
        ],
    }


class CoreTests(unittest.TestCase):
    def test_wrong_approval_blocks_before_adapter(self):
        adapter = FakeAdapter()
        plan = backup_plan(CONFIG)
        with self.assertRaises(Refused):
            backup_execute(plan, "wrong", adapter)
        self.assertEqual([], adapter.calls)

    def test_expired_plan_rejected(self):
        plan = backup_plan(CONFIG, datetime(2020, 1, 1, tzinfo=timezone.utc))
        with self.assertRaisesRegex(Refused, "expired"):
            approve(plan, digest(plan))

    def test_backup_order_and_receipt(self):
        adapter = FakeAdapter()
        plan = backup_plan(CONFIG)
        result = backup_execute(plan, digest(plan), adapter)
        self.assertEqual(
            [
                "scope",
                "quiesce",
                "clickhouse",
                "valkey",
                "snapshot",
                "inventory",
                "receipt",
                "publish-backup",
                "receipt",
            ],
            adapter.calls,
        )
        self.assertEqual(CONFIG, result["config"])
        self.assertFalse(result["restore_rehearsed"])

    def test_backup_failure_never_reports_success(self):
        adapter = FakeAdapter()

        def fail(_):
            raise Refused("snapshot unavailable")

        adapter.snapshot_database = fail
        plan = backup_plan(CONFIG)
        with self.assertRaisesRegex(Refused, "snapshot unavailable"):
            backup_execute(plan, digest(plan), adapter)
        self.assertNotIn("receipt", adapter.calls)

    def test_destroy_rejects_foundation_and_wrong_scope(self):
        for kind in [
            "aws_s3_bucket",
            "aws_cognito_user_pool",
            "aws_secretsmanager_secret",
            "aws_sqs_queue",
        ]:
            with self.assertRaises(Refused):
                inspect_destroy(tfplan(kind), CONFIG)
        value = tfplan()
        value["variables"]["environment"]["value"] = "production"
        with self.assertRaises(Refused):
            inspect_destroy(value, CONFIG)
        value = tfplan()
        value["configuration"]["provider_config"]["aws"]["expressions"]["assume_role"] = {}
        with self.assertRaises(Refused):
            inspect_destroy(value, CONFIG)

    def test_destroy_rejects_updates_and_replacements(self):
        for actions in [["update"], ["delete", "create"]]:
            value = tfplan()
            value["resource_changes"][0]["change"]["actions"] = actions
            with self.assertRaises(Refused):
                inspect_destroy(value, CONFIG)

    def test_gpu_subnets_and_route_associations_are_exactly_network_bound(self):
        tags = {
            "Project": "deepresearch",
            "Environment": "staging",
            "Session": "session-1",
        }
        for name, number in [("gpu", 2), ("gpu_fallback", 3)]:
            with self.subTest(subnet=name):
                plan = tfplan()
                subnet = f"aws_subnet.{name}"
                association = f"aws_route_table_association.{name}"
                resources = [
                    ("aws_vpc.main", "aws_vpc"),
                    ("aws_route_table.public", "aws_route_table"),
                    (subnet, "aws_subnet"),
                    (association, "aws_route_table_association"),
                ]
                plan["configuration"]["root_module"]["resources"] = [
                    {
                        "address": address,
                        "type": kind,
                        "mode": "managed",
                        "provider_config_key": "aws",
                    }
                    for address, kind in resources
                ]
                subnet_id = f"subnet-{number:08x}"
                route_table_id = "rtb-0123456789abcdef0"
                before = {
                    "aws_vpc.main": {
                        "id": "vpc-0123456789abcdef0",
                        "owner_id": CONFIG["account_id"],
                        "cidr_block": "10.60.0.0/16",
                        "tags_all": tags,
                    },
                    "aws_route_table.public": {
                        "id": route_table_id,
                        "vpc_id": "vpc-0123456789abcdef0",
                        "tags_all": tags,
                    },
                    subnet: {
                        "id": subnet_id,
                        "vpc_id": "vpc-0123456789abcdef0",
                        "cidr_block": f"10.60.{number}.0/24",
                        "map_public_ip_on_launch": True,
                        "tags_all": tags,
                    },
                    association: {
                        "subnet_id": subnet_id,
                        "route_table_id": route_table_id,
                    },
                }
                plan["resource_changes"] = [
                    {
                        "address": address,
                        "type": kind,
                        "mode": "managed",
                        "provider_name": "registry.terraform.io/hashicorp/aws",
                        "change": {"actions": ["delete"], "before": before[address]},
                    }
                    for address, kind in resources
                ]
                self.assertEqual(
                    [address for address, _ in resources], inspect_destroy(plan, CONFIG)
                )

                plan["resource_changes"][2]["change"]["before"]["cidr_block"] = (
                    "10.60.250.0/24"
                )
                with self.assertRaisesRegex(Refused, "Subnet CIDR/public mapping mismatch"):
                    inspect_destroy(plan, CONFIG)

                plan["resource_changes"][2]["change"]["before"]["cidr_block"] = (
                    f"10.60.{number}.0/24"
                )
                plan["resource_changes"][2]["change"]["before"][
                    "map_public_ip_on_launch"
                ] = False
                with self.assertRaisesRegex(Refused, "Subnet CIDR/public mapping mismatch"):
                    inspect_destroy(plan, CONFIG)
                plan["resource_changes"][2]["change"]["before"][
                    "map_public_ip_on_launch"
                ] = True

                plan["resource_changes"][3]["change"]["before"]["subnet_id"] = (
                    "subnet-ffffffff"
                )
                with self.assertRaisesRegex(Refused, "Route association identity mismatch"):
                    inspect_destroy(plan, CONFIG)
                plan["resource_changes"][3]["change"]["before"]["subnet_id"] = subnet_id
                plan["resource_changes"][3]["change"]["before"]["route_table_id"] = (
                    "rtb-ffffffff"
                )
                with self.assertRaisesRegex(Refused, "Route association identity mismatch"):
                    inspect_destroy(plan, CONFIG)

    def test_runtime_destroy_binds_exact_vpc_cidr(self):
        plan = tfplan()
        plan["resource_changes"][0]["change"]["before"]["cidr_block"] = "0.0.0.0/0"
        with self.assertRaisesRegex(Refused, "Runtime VPC CIDR mismatch"):
            inspect_destroy(plan, CONFIG)

    def test_gpu_node_group_identity_matches_gpu_flex_and_all_runtime_subnets(self):
        tags = {
            "Project": "deepresearch",
            "Environment": "staging",
            "Session": "session-1",
        }
        vpc_id = "vpc-0123456789abcdef0"
        subnet_values = {
            "aws_subnet.public[0]": ("subnet-00000001", "10.60.0.0/24"),
            "aws_subnet.public[1]": ("subnet-00000002", "10.60.1.0/24"),
            "aws_subnet.gpu": ("subnet-00000003", "10.60.2.0/24"),
            "aws_subnet.gpu_fallback": ("subnet-00000004", "10.60.3.0/24"),
        }
        addresses = ["aws_vpc.main", *subnet_values, "aws_eks_node_group.gpu"]
        kinds = {
            "aws_vpc.main": "aws_vpc",
            **{address: "aws_subnet" for address in subnet_values},
            "aws_eks_node_group.gpu": "aws_eks_node_group",
        }
        plan = tfplan()
        declarations = list(dict.fromkeys(address.split("[")[0] for address in addresses))
        plan["configuration"]["root_module"]["resources"] = [
            {
                "address": address,
                "type": kinds.get(address, kinds.get(address + "[0]")),
                "mode": "managed",
                "provider_config_key": "aws",
            }
            for address in declarations
        ]
        before = {
            "aws_vpc.main": {
                "id": vpc_id,
                "owner_id": CONFIG["account_id"],
                "cidr_block": "10.60.0.0/16",
                "tags_all": tags,
            },
            "aws_eks_node_group.gpu": {
                "cluster_name": "deepresearch-staging",
                "node_group_name": "gpu-flex",
                "node_role_arn": "arn:aws:iam::123456789012:role/deepresearch-staging-node",
                "subnet_ids": [value[0] for value in subnet_values.values()],
                "tags_all": tags,
            },
        }
        for address, (subnet_id, cidr) in subnet_values.items():
            before[address] = {
                "id": subnet_id,
                "vpc_id": vpc_id,
                "cidr_block": cidr,
                "map_public_ip_on_launch": True,
                "tags_all": tags,
            }
        plan["resource_changes"] = [
            {
                "address": address,
                "type": kinds[address],
                "mode": "managed",
                "provider_name": "registry.terraform.io/hashicorp/aws",
                "change": {"actions": ["delete"], "before": before[address]},
            }
            for address in addresses
        ]
        self.assertEqual(addresses, inspect_destroy(plan, CONFIG))

        plan["resource_changes"][-1]["change"]["before"]["node_group_name"] = "gpu"
        with self.assertRaisesRegex(Refused, "Node group identity mismatch"):
            inspect_destroy(plan, CONFIG)

        plan["resource_changes"][-1]["change"]["before"]["node_group_name"] = "gpu-flex"
        plan["resource_changes"][-1]["change"]["before"]["subnet_ids"].pop()
        with self.assertRaisesRegex(Refused, "Node group subnet mismatch"):
            inspect_destroy(plan, CONFIG)

    def test_saved_plan_hash_and_receipt_bound_before_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "infra/runtime"
            runtime.mkdir(parents=True)
            receipt = root / "receipt.json"
            receipt.write_text(json.dumps({"config": CONFIG, "drain": {"writers": []}}))
            saved = root / "destroy.tfplan"
            calls = []

            def runner(argv, **kw):
                calls.append(argv)
                if "plan" in argv:
                    saved.write_bytes(b"immutable binary")
                if "show" in argv:
                    return json.dumps(tfplan())
                return ""

            candidate = prepare_shutdown(
                CONFIG, receipt, runtime, root / "vars.json", saved, runner
            )
            adapter = FakeAdapter()
            shutdown_execute(candidate, digest(candidate), adapter, runner)
            self.assertIn("unprotect", adapter.calls)
            self.assertEqual(
                [
                    "terraform",
                    f"-chdir={runtime.resolve()}",
                    "apply",
                    "-input=false",
                    str(saved.resolve()),
                ],
                calls[-1],
            )
            saved.write_bytes(b"tampered")
            other = FakeAdapter()
            with self.assertRaises(Refused):
                shutdown_execute(candidate, digest(candidate), other, runner)
            self.assertEqual([], other.calls)

    def test_expiry_refuses_early_without_mutations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "infra/runtime").mkdir(parents=True)
            saved = root / "artifacts/expiry.tfplan"

            def runner(argv, **kw):
                if "plan" in argv:
                    saved.write_bytes(b"approved")
                if "show" in argv:
                    return json.dumps(tfplan())
                return ""

            intent = prepare_expiry(
                CONFIG, root, "artifacts/vars.json", runner=runner,
                runtime_state_bucket=RUNTIME_STATE_BUCKET,
                runtime_backend_file=runtime_backend_file(root),
            )
            adapter = FakeAdapter()
            with self.assertRaisesRegex(Refused, "not reached"):
                execute_expiry(intent, digest(intent), root, adapter, runner)
            self.assertEqual([], adapter.calls)

    def test_expiry_persists_backup_before_destructive_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "infra/runtime").mkdir(parents=True)
            saved = root / "artifacts/expiry.tfplan"

            def runner(argv, **kw):
                if "plan" in argv:
                    saved.write_bytes(b"approved")
                if "show" in argv:
                    return json.dumps(tfplan())
                return ""

            intent = prepare_expiry(
                CONFIG, root, "artifacts/vars.json", runner=runner,
                runtime_state_bucket=RUNTIME_STATE_BUCKET,
                runtime_backend_file=runtime_backend_file(root),
            )
            intent["not_before"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            adapter = FakeAdapter()
            result = execute_expiry(intent, digest(intent), root, adapter, runner)
            self.assertTrue(result["executed"])
            self.assertLess(adapter.calls.index("publish-backup"), adapter.calls.index("unprotect"))
            self.assertTrue((root / "artifacts/expiry-backup-receipt.json").is_file())

    def test_restore_does_not_claim_full_rehearsal(self):
        receipt = {
            "config": CONFIG,
            "database_snapshot": {"arn": "arn:snapshot"},
            "clickhouse": {"url": "s3://backup"},
            "valkey": {},
        }
        result = restore_prepare(receipt, FakeAdapter())
        self.assertFalse(result["application_restore_rehearsed"])
        self.assertFalse(result["database_restore_verified"])
        self.assertTrue(result["snapshot_provider_verified"])


class AdapterTests(unittest.TestCase):
    def test_bootstrap_cleanup_refuses_after_deployment_even_if_namespace_is_gone(self):
        calls = []
        marker_key = f"app-writers#staging#{CONFIG['session_id']}"
        dynamodb = SimpleNamespace(
            get_item=lambda **kwargs: calls.append(kwargs)
            or {
                "Item": {
                    "key": {"S": marker_key},
                    "session_id": {"S": CONFIG["session_id"]},
                    "environment": {"S": "staging"},
                    "started_at": {"N": "1000"},
                    "commit_sha": {"S": "a" * 40},
                    "workflow_run_id": {"N": "12345"},
                }
            }
        )
        adapter = AWSAdapter(CONFIG, {"dynamodb": dynamodb}, runner=lambda argv: "")
        adapter.require_lease = lambda: None
        adapter.identity = lambda: None
        adapter.verify_retained = lambda: None

        with self.assertRaisesRegex(Refused, "Application deployment was started"):
            adapter.verify_bootstrap_empty()

        self.assertEqual(1, len(calls))
        self.assertEqual(marker_key, calls[0]["Key"]["key"]["S"])
        self.assertTrue(calls[0]["ConsistentRead"])

    def test_bootstrap_cleanup_fails_closed_on_malformed_deployment_marker(self):
        marker_key = f"app-writers#staging#{CONFIG['session_id']}"
        dynamodb = SimpleNamespace(
            get_item=lambda **_: {
                "Item": {
                    "key": {"S": marker_key},
                    "session_id": {"S": CONFIG["session_id"]},
                    "environment": {"S": "staging"},
                }
            }
        )
        adapter = AWSAdapter(CONFIG, {"dynamodb": dynamodb}, runner=lambda argv: "")
        adapter.require_lease = lambda: None
        adapter.identity = lambda: None
        adapter.verify_retained = lambda: None

        with self.assertRaisesRegex(Refused, "Could not verify durable application deployment history"):
            adapter.verify_bootstrap_empty()

    def test_wrong_account_rejected(self):
        adapter = AWSAdapter(
            CONFIG,
            {"sts": SimpleNamespace(get_caller_identity=lambda: {"Account": "000000000000"})},
        )
        with self.assertRaises(Refused):
            adapter.identity()

    def test_alert_channel_requires_environment_customer_key_and_rotation(self):
        key_id = CONFIG["alert_kms_key_arn"].rsplit("/", 1)[1]
        sns = SimpleNamespace(
            get_topic_attributes=lambda **_: {
                "Attributes": {
                    "Owner": CONFIG["account_id"],
                    "KmsMasterKeyId": CONFIG["alert_kms_key_arn"],
                }
            }
        )
        kms = SimpleNamespace(
            describe_key=lambda **_: {
                "KeyMetadata": {
                    "Arn": CONFIG["alert_kms_key_arn"],
                    "KeyId": key_id,
                    "KeyState": "Enabled",
                    "KeyManager": "CUSTOMER",
                    "KeyUsage": "ENCRYPT_DECRYPT",
                    "KeySpec": "SYMMETRIC_DEFAULT",
                    "Description": "Encryption key for deepresearch-staging expiry alert topic",
                }
            },
            list_resource_tags=lambda **_: {
                "Tags": [
                    {"TagKey": "Project", "TagValue": "deepresearch"},
                    {"TagKey": "Environment", "TagValue": "staging"},
                    {"TagKey": "Component", "TagValue": "expiry-alerts"},
                ]
            },
            get_key_rotation_status=lambda **_: {"KeyRotationEnabled": True},
        )
        adapter = AWSAdapter(CONFIG, {"sns": sns, "kms": kms})

        adapter.verify_alert_channel()

        sns.get_topic_attributes = lambda **_: {
            "Attributes": {
                "Owner": CONFIG["account_id"],
                "KmsMasterKeyId": "alias/deepresearch-staging-alerts",
            }
        }
        adapter.verify_alert_channel()
        original_describe_key = kms.describe_key
        kms.describe_key = lambda **kwargs: (
            {
                "KeyMetadata": {
                    "Arn": "arn:aws:kms:us-east-1:123456789012:key/22222222-2222-4222-8222-222222222222",
                    "KeyId": "22222222-2222-4222-8222-222222222222",
                }
            }
            if kwargs["KeyId"] == "alias/deepresearch-staging-alerts"
            else original_describe_key(**kwargs)
        )
        with self.assertRaises(Refused):
            adapter.verify_alert_channel()
        kms.describe_key = original_describe_key
        sns.get_topic_attributes = lambda **_: {
            "Attributes": {
                "Owner": CONFIG["account_id"],
                "KmsMasterKeyId": CONFIG["alert_kms_key_arn"],
            }
        }

        kms.get_key_rotation_status = lambda **_: {"KeyRotationEnabled": False}
        with self.assertRaises(Refused):
            adapter.verify_alert_channel()

    def test_version_inventory_paginates_and_keeps_delete_markers(self):
        pages = [
            {
                "Versions": [
                    {"Key": "a", "VersionId": "v1", "ETag": "e", "Size": 5, "IsLatest": False}
                ]
            },
            {"DeleteMarkers": [{"Key": "a", "VersionId": "v2", "IsLatest": True}]},
        ]
        client = SimpleNamespace(
            get_paginator=lambda name: SimpleNamespace(paginate=lambda **kw: pages)
        )
        result = AWSAdapter(CONFIG, {"s3": client}).versions("research-documents")
        self.assertEqual(1, len(result["versions"]))
        self.assertEqual(1, len(result["delete_markers"]))

    def test_version_inventory_rejects_null_versions(self):
        client = SimpleNamespace(
            get_paginator=lambda name: SimpleNamespace(
                paginate=lambda **kw: [{"Versions": [{"VersionId": "null"}]}]
            )
        )
        with self.assertRaises(Refused):
            AWSAdapter(CONFIG, {"s3": client}).versions("research-documents")

    def test_snapshot_waits_for_provider_availability(self):
        calls = []
        snapshot = {
            "Status": "available",
            "Encrypted": True,
            "DBSnapshotArn": "arn:snapshot",
            "SnapshotCreateTime": datetime.now(timezone.utc),
        }
        rds = SimpleNamespace(
            create_db_snapshot=lambda **kw: calls.append("create") or {"DBSnapshot": snapshot},
            get_waiter=lambda name: SimpleNamespace(wait=lambda **kw: calls.append("wait")),
            describe_db_snapshots=lambda **kw: {"DBSnapshots": [snapshot]},
        )
        adapter = AWSAdapter(CONFIG, {"rds": rds})
        adapter.exclusive_guard = lambda: calls.append("lease")
        result = adapter.snapshot_database("snapshot-1")
        self.assertEqual(["lease", "create", "wait"], calls)
        self.assertEqual("arn:snapshot", result["arn"])

    def test_changed_backup_object_rejected(self):
        client = SimpleNamespace(head_object=lambda **kw: {"ETag": "changed", "ContentLength": 3})
        with self.assertRaises(Refused):
            AWSAdapter(CONFIG, {"s3": client}).verify_objects(
                {
                    "bucket": "research-backups",
                    "versions": [{"Key": "a", "VersionId": "v1", "ETag": "original", "Size": 3}],
                }
            )


class ScopeRegressionTests(unittest.TestCase):
    def addon_destroy_plan(self, component, addon_name):
        plan = tfplan()
        address = "aws_eks_addon." + component
        plan["configuration"]["root_module"]["resources"] = [
            {
                "address": address,
                "type": "aws_eks_addon",
                "mode": "managed",
                "provider_config_key": "aws",
            }
        ]
        plan["resource_changes"] = [
            {
                "address": address,
                "type": "aws_eks_addon",
                "mode": "managed",
                "provider_name": "registry.terraform.io/hashicorp/aws",
                "change": {
                    "actions": ["delete"],
                    "before": {
                        "cluster_name": CONFIG["cluster_name"],
                        "addon_name": addon_name,
                        "tags_all": {
                            "Project": "deepresearch",
                            "Environment": "staging",
                            "Session": "session-1",
                        },
                    },
                },
            }
        ]
        return plan

    def test_network_addons_are_destroyable_only_for_the_expected_cluster_and_name(self):
        for component, addon_name in [("kube_proxy", "kube-proxy"), ("coredns", "coredns")]:
            with self.subTest(addon=component):
                plan = self.addon_destroy_plan(component, addon_name)
                self.assertEqual(["aws_eks_addon." + component], inspect_destroy(plan, CONFIG))
                for field, invalid in [
                    ("cluster_name", "deepresearch-production"),
                    ("addon_name", "other"),
                ]:
                    plan = self.addon_destroy_plan(component, addon_name)
                    plan["resource_changes"][0]["change"]["before"][field] = invalid
                    with self.assertRaisesRegex(Refused, "EKS addon identity mismatch"):
                        inspect_destroy(plan, CONFIG)

    def test_clickhouse_backup_identity_teardown_rejects_other_service_accounts(self):
        plan = tfplan()
        address = "aws_eks_pod_identity_association.clickhouse_backup"
        plan["configuration"]["root_module"]["resources"] = [
            {
                "address": address,
                "type": "aws_eks_pod_identity_association",
                "mode": "managed",
                "provider_config_key": "aws",
            }
        ]
        before = {
            "cluster_name": CONFIG["cluster_name"],
            "namespace": "research",
            "service_account": "research-clickhouse-backup",
            "role_arn": "arn:aws:iam::123456789012:role/deepresearch-staging-clickhouse-backup",
            "tags_all": {
                "Project": "deepresearch",
                "Environment": "staging",
                "Session": "session-1",
            },
        }
        plan["resource_changes"] = [
            {
                "address": address,
                "type": "aws_eks_pod_identity_association",
                "mode": "managed",
                "provider_name": "registry.terraform.io/hashicorp/aws",
                "change": {"actions": ["delete"], "before": before},
            }
        ]
        self.assertEqual([address], inspect_destroy(plan, CONFIG))
        before["service_account"] = "research-observability"
        with self.assertRaisesRegex(Refused, "Pod Identity binding mismatch"):
            inspect_destroy(plan, CONFIG)

        plan = tfplan()
        address = "aws_eks_pod_identity_association.load_balancer_controller"
        plan["configuration"]["root_module"]["resources"] = [
            {
                "address": address,
                "type": "aws_eks_pod_identity_association",
                "mode": "managed",
                "provider_config_key": "aws",
            }
        ]
        plan["resource_changes"] = [
            {
                "address": address,
                "type": "aws_eks_pod_identity_association",
                "mode": "managed",
                "provider_name": "registry.terraform.io/hashicorp/aws",
                "change": {
                    "actions": ["delete"],
                    "before": {
                        "cluster_name": CONFIG["cluster_name"],
                        "namespace": "kube-system",
                        "service_account": "aws-load-balancer-controller",
                        "role_arn": "arn:aws:iam::123456789012:role/deepresearch-staging-load-balancer-controller",
                        "tags_all": {
                            "Project": "deepresearch",
                            "Environment": "staging",
                            "Session": "session-1",
                        },
                    },
                },
            }
        ]
        self.assertEqual([address], inspect_destroy(plan, CONFIG))

    def test_github_eks_access_teardown_is_limited_to_research_namespace(self):
        plan = tfplan()
        entry = "aws_eks_access_entry.github_actions[0]"
        association = "aws_eks_access_policy_association.github_actions[0]"
        plan["configuration"]["root_module"]["resources"] = [
            {
                "address": "aws_eks_access_entry.github_actions",
                "type": "aws_eks_access_entry",
                "mode": "managed",
                "provider_config_key": "aws",
            },
            {
                "address": "aws_eks_access_policy_association.github_actions",
                "type": "aws_eks_access_policy_association",
                "mode": "managed",
                "provider_config_key": "aws",
            },
        ]
        principal = "arn:aws:iam::123456789012:role/deepresearch-staging-github-oidc"
        plan["resource_changes"] = [
            {
                "address": entry,
                "type": "aws_eks_access_entry",
                "mode": "managed",
                "provider_name": "registry.terraform.io/hashicorp/aws",
                "change": {
                    "actions": ["delete"],
                    "before": {
                        "principal_arn": principal,
                        "cluster_name": CONFIG["cluster_name"],
                        "type": "STANDARD",
                    },
                },
            },
            {
                "address": association,
                "type": "aws_eks_access_policy_association",
                "mode": "managed",
                "provider_name": "registry.terraform.io/hashicorp/aws",
                "change": {
                    "actions": ["delete"],
                    "before": {
                        "principal_arn": principal,
                        "cluster_name": CONFIG["cluster_name"],
                        "policy_arn": "arn:aws:eks::aws:cluster-access-policy/AmazonEKSEditPolicy",
                        "access_scope": [{"type": "namespace", "namespaces": ["research"]}],
                    },
                },
            },
        ]
        self.assertEqual([entry, association], inspect_destroy(plan, CONFIG))
        plan["resource_changes"][1]["change"]["before"]["access_scope"][0]["namespaces"] = [
            "default"
        ]
        with self.assertRaisesRegex(Refused, "namespace-scoped edit"):
            inspect_destroy(plan, CONFIG)

    def test_load_balancer_controller_policy_is_exact_and_destroyable(self):
        plan = tfplan()
        address = "aws_iam_policy.load_balancer_controller"
        plan["configuration"]["root_module"]["resources"] = [
            {
                "address": address,
                "type": "aws_iam_policy",
                "mode": "managed",
                "provider_config_key": "aws",
            }
        ]
        plan["resource_changes"] = [
            {
                "address": address,
                "type": "aws_iam_policy",
                "mode": "managed",
                "provider_name": "registry.terraform.io/hashicorp/aws",
                "change": {
                    "actions": ["delete"],
                    "before": {
                        "name": "deepresearch-staging-load-balancer-controller",
                        "arn": "arn:aws:iam::123456789012:policy/deepresearch-staging-load-balancer-controller",
                        "tags_all": {
                            "Project": "deepresearch",
                            "Environment": "staging",
                            "Session": "session-1",
                        },
                    },
                },
            }
        ]
        self.assertEqual([address], inspect_destroy(plan, CONFIG))

    def admin_destroy_plan(self):
        plan = tfplan()
        plan["variables"]["operator_principal_arn"] = {"value": CONFIG["operator_principal_arn"]}
        tags = {"Project": "deepresearch", "Environment": "staging", "Session": "session-1"}
        role_arn = "arn:aws:iam::123456789012:role/deepresearch-staging-cluster-admin"
        trust = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "sts:AssumeRole",
                    "Principal": {"AWS": CONFIG["operator_principal_arn"]},
                }
            ],
        }
        role = {
            "address": "aws_iam_role.cluster_admin",
            "type": "aws_iam_role",
            "mode": "managed",
            "provider_config_key": "aws",
        }
        access = {
            "address": "aws_eks_access_entry.administrator",
            "type": "aws_eks_access_entry",
            "mode": "managed",
            "provider_config_key": "aws",
        }
        plan["configuration"]["root_module"]["resources"] = [role, access]
        plan["resource_changes"] = [
            {
                "address": role["address"],
                "type": role["type"],
                "mode": "managed",
                "provider_name": "registry.terraform.io/hashicorp/aws",
                "change": {
                    "actions": ["delete"],
                    "before": {
                        "name": "deepresearch-staging-cluster-admin",
                        "arn": role_arn,
                        "assume_role_policy": json.dumps(trust),
                        "tags_all": tags,
                    },
                },
            },
            {
                "address": access["address"],
                "type": access["type"],
                "mode": "managed",
                "provider_name": "registry.terraform.io/hashicorp/aws",
                "change": {
                    "actions": ["delete"],
                    "before": {
                        "principal_arn": role_arn,
                        "cluster_name": "deepresearch-staging",
                    },
                },
            },
        ]
        return plan

    def test_short_lived_eks_admin_role_and_access_binding_are_destroyable(self):
        plan = self.admin_destroy_plan()
        self.assertEqual(
            ["aws_iam_role.cluster_admin", "aws_eks_access_entry.administrator"],
            inspect_destroy(plan, CONFIG),
        )

    def test_short_lived_eks_admin_destroy_rejects_wrong_trust_and_access_role(self):
        plan = self.admin_destroy_plan()
        trust = json.loads(plan["resource_changes"][0]["change"]["before"]["assume_role_policy"])
        trust["Statement"][0]["Principal"]["AWS"] = "arn:aws:iam::123456789012:role/other"
        plan["resource_changes"][0]["change"]["before"]["assume_role_policy"] = json.dumps(trust)
        with self.assertRaisesRegex(Refused, "trust must match"):
            inspect_destroy(plan, CONFIG)

        plan = self.admin_destroy_plan()
        plan["resource_changes"][1]["change"]["before"]["principal_arn"] = (
            "arn:aws:iam::123456789012:role/other"
        )
        with self.assertRaisesRegex(Refused, "administrator identity mismatch"):
            inspect_destroy(plan, CONFIG)

    def test_provider_region_source_and_account_expressions_must_match(self):
        attacks = [
            ("region", {"constant_value": "us-west-2"}),
            ("region", {"references": ["var.region"], "constant_value": "us-west-2"}),
            ("allowed_account_ids", {"constant_value": ["000000000000"]}),
        ]
        for key, expression in attacks:
            plan = tfplan()
            plan["configuration"]["provider_config"]["aws"]["expressions"][key] = expression
            with self.assertRaises(Refused):
                inspect_destroy(plan, CONFIG)
        plan = tfplan()
        plan["configuration"]["provider_config"]["aws"]["full_name"] = (
            "registry.terraform.io/attacker/aws"
        )
        with self.assertRaises(Refused):
            inspect_destroy(plan, CONFIG)
        plan = tfplan()
        plan["resource_changes"][0]["provider_name"] = "registry.terraform.io/attacker/aws"
        with self.assertRaises(Refused):
            inspect_destroy(plan, CONFIG)

    def test_untagged_or_wrong_session_runtime_refused(self):
        for tags in [
            None,
            {},
            {"Project": "deepresearch", "Environment": "staging"},
            {"Project": "deepresearch", "Environment": "staging", "Session": "other"},
        ]:
            plan = tfplan()
            plan["resource_changes"][0]["change"]["before"]["tags_all"] = tags
            with self.assertRaises(Refused):
                inspect_destroy(plan, CONFIG)

    def test_arbitrary_root_and_borrowed_iam_role_refused(self):
        plan = tfplan("aws_iam_role")
        plan["resource_changes"][0]["change"]["before"].update(
            name="production-admin", arn="arn:aws:iam::123456789012:role/production-admin"
        )
        with self.assertRaises(Refused):
            inspect_destroy(plan, CONFIG)
        plan = tfplan("aws_iam_role")
        for obj in [
            plan["configuration"]["root_module"]["resources"][0],
            plan["resource_changes"][0],
        ]:
            obj["address"] = "aws_iam_role.application"
        plan["resource_changes"][0]["change"]["before"].update(
            name="production-admin", arn="arn:aws:iam::123456789012:role/production-admin"
        )
        with self.assertRaises(Refused):
            inspect_destroy(plan, CONFIG)

    def test_account_and_vpc_owner_bound(self):
        plan = tfplan()
        plan["variables"]["account_id"]["value"] = "000000000000"
        with self.assertRaises(Refused):
            inspect_destroy(plan, CONFIG)
        plan = tfplan()
        plan["resource_changes"][0]["change"]["before"]["owner_id"] = "000000000000"
        with self.assertRaises(Refused):
            inspect_destroy(plan, CONFIG)

    def test_retained_bucket_names_are_exact_not_just_tagged(self):
        from lifecycle.core import validate_config

        for key in ["document_bucket", "backup_bucket", "trace_bucket"]:
            changed = dict(CONFIG)
            changed[key] = "attacker-controlled-bucket"
            with self.assertRaises(Refused):
                validate_config(changed)

    def test_config_binds_alert_topic_and_kms_key_to_environment(self):
        from lifecycle.core import validate_config

        for key, value in (
            ("alert_topic_arn", "arn:aws:sns:us-east-1:123456789012:another-topic"),
            (
                "alert_kms_key_arn",
                "arn:aws:kms:us-west-2:123456789012:key/11111111-1111-4111-8111-111111111111",
            ),
        ):
            with self.subTest(key=key):
                changed = {**CONFIG, key: value}
                with self.assertRaises(Refused):
                    validate_config(changed)

    def test_runner_lifecycle_config_requires_explicit_key_scope(self):
        from lifecycle.core import validate_config

        without_runner_state = {key: value for key, value in CONFIG.items() if key != "github_runner_enabled"}
        with self.assertRaises(Refused):
            validate_config(without_runner_state)
        with self.assertRaises(Refused):
            validate_config({**CONFIG, "github_runner_enabled": True})
        with self.assertRaises(Refused):
            validate_config({
                **CONFIG,
                "github_runner_enabled": True,
                "runner_token_kms_key_arn": "arn:aws:kms:us-west-2:123456789012:key/22222222-2222-4222-8222-222222222222",
            })
        with self.assertRaises(Refused):
            validate_config({
                **CONFIG,
                "runner_token_kms_key_arn": "arn:aws:kms:us-east-1:123456789012:key/22222222-2222-4222-8222-222222222222",
            })

    def test_generation_change_or_replacement_requires_new_backup(self):
        generation = 5
        uid = "writer-uid"

        def runner(argv, **kwargs):
            if "deployment" in argv:
                return json.dumps(
                    {"metadata": {"uid": uid, "generation": generation}, "spec": {"replicas": 0}}
                )
            if "pods" in argv:
                return json.dumps({"items": []})
            raise AssertionError(argv)

        adapter = AWSAdapter(CONFIG, {}, runner=runner)
        initial = adapter.verify_quiesced()
        expected = {"writers": initial}
        self.assertEqual(initial, adapter.verify_quiesced(expected))
        generation = 7
        with self.assertRaisesRegex(Refused, "fresh backup"):
            adapter.verify_quiesced(expected)
        generation = 5
        uid = "replacement-uid"
        with self.assertRaisesRegex(Refused, "fresh backup"):
            adapter.verify_quiesced(expected)

    def test_old_snapshot_cannot_prove_new_quiesce(self):
        adapter = AWSAdapter(CONFIG, {})
        created = datetime.now(timezone.utc) - timedelta(minutes=10)
        receipt = {
            "drain": {"verified_at": (created + timedelta(minutes=5)).isoformat()},
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database_snapshot": {"created_at": created.isoformat()},
        }
        with self.assertRaisesRegex(Refused, "not created after"):
            adapter.verify_snapshot_timing({"SnapshotCreateTime": created}, receipt)

    def test_version_calls_pass_expected_bucket_owner(self):
        observed = []

        def pages(**kwargs):
            observed.append(kwargs)
            return []

        client = SimpleNamespace(get_paginator=lambda _: SimpleNamespace(paginate=pages))
        AWSAdapter(CONFIG, {"s3": client}).versions(CONFIG["backup_bucket"])
        self.assertEqual(CONFIG["account_id"], observed[0]["ExpectedBucketOwner"])

    def test_durable_receipt_is_provider_verified_and_content_bound(self):
        import hashlib

        from lifecycle.core import canonical

        receipt = {"operation": "backup-receipt", "config": CONFIG, "created_at": "synthetic"}
        data = canonical(receipt)
        observed = []

        def fetch(**kwargs):
            observed.append(kwargs)
            return {"Body": io.BytesIO(data)}

        pointer = {
            "bucket": CONFIG["backup_bucket"],
            "key": "sessions/staging/session-1/receipts/backup.json",
            "version_id": "immutable-v1",
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        local = {**receipt, "durable_backup_receipt": pointer}
        adapter = AWSAdapter(CONFIG, {"s3": SimpleNamespace(get_object=fetch)})
        adapter.verify_durable_receipt(local)
        self.assertEqual(CONFIG["account_id"], observed[0]["ExpectedBucketOwner"])
        local["created_at"] = "changed-after-publication"
        with self.assertRaisesRegex(Refused, "differs"):
            adapter.verify_durable_receipt(local)


class BoundaryRegressionTests(unittest.TestCase):
    def test_provisioners_are_rejected_even_on_noop_resource(self):
        for value in [
            [],
            [
                {
                    "type": "local-exec",
                    "when": "destroy",
                    "expressions": {"command": {"constant_value": "unsafe"}},
                }
            ],
        ]:
            plan = tfplan()
            plan["resource_changes"][0]["change"]["actions"] = ["no-op"]
            plan["configuration"]["root_module"]["resources"][0]["provisioners"] = value
            with self.assertRaisesRegex(Refused, "provisioners"):
                inspect_destroy(plan, CONFIG)

    def test_writers_resuming_during_ingress_wait_prevent_unprotect(self):
        self.run_resume_case("ingress")

    def test_writers_resuming_during_database_wait_prevent_apply(self):
        self.run_resume_case("unprotect")

    def run_resume_case(self, phase):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "infra/runtime"
            runtime.mkdir(parents=True)
            receipt = root / "receipt.json"
            receipt.write_text(json.dumps({"config": CONFIG, "drain": {"writers": []}}))
            saved = root / "destroy.tfplan"
            commands = []

            def runner(argv, **kwargs):
                commands.append(argv)
                if "plan" in argv:
                    saved.write_bytes(b"approved")
                if "show" in argv:
                    return json.dumps(tfplan())
                return ""

            candidate = prepare_shutdown(
                CONFIG, receipt, runtime, root / "vars.json", saved, runner
            )

            class ResumeAdapter(FakeAdapter):
                def verify_receipt(self, receipt, **kwargs):
                    super().verify_receipt(receipt, **kwargs)
                    if phase in self.calls:
                        raise Refused("Writer generation changed")

            adapter = ResumeAdapter()
            with self.assertRaisesRegex(Refused, "generation changed"):
                shutdown_execute(candidate, digest(candidate), adapter, runner)
            self.assertFalse(any("apply" in cmd for cmd in commands))
            if phase == "ingress":
                self.assertNotIn("unprotect", adapter.calls)
            else:
                self.assertIn("unprotect", adapter.calls)


class ConsoleIngressRegressionTests(unittest.TestCase):
    def test_shared_alb_ingresses_are_both_removed_before_waiting(self):
        calls = []
        adapter = AWSAdapter(CONFIG, {}, runner=lambda argv: calls.append(argv) or "")
        adapter.exclusive_guard = lambda: None
        adapter.cluster_albs = lambda: []
        adapter.remove_ingress()
        self.assertEqual(1, len(calls))
        self.assertIn("research", calls[0])
        self.assertIn("research-console", calls[0])
        self.assertIn("--ignore-not-found=true", calls[0])


class ExpiryApprovalWindowTests(unittest.TestCase):
    def prepare(self, root):
        (root / "infra/runtime").mkdir(parents=True)
        saved = root / "artifacts/expiry.tfplan"

        def runner(argv, **kwargs):
            if "plan" in argv:
                saved.write_bytes(b"approved")
            return json.dumps(tfplan()) if "show" in argv else ""

        return (
            prepare_expiry(
                CONFIG, root, "artifacts/vars.json", runner=runner,
                runtime_state_bucket=RUNTIME_STATE_BUCKET,
                runtime_backend_file=runtime_backend_file(root),
            ),
            runner,
        )

    def test_expiry_intent_hash_binds_runtime_state_location(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            intent, _ = self.prepare(root)
            self.assertEqual(RUNTIME_STATE_BUCKET, intent["runtime_state_bucket"])
            self.assertEqual("deepresearch/staging/runtime.tfstate", intent["runtime_state_key"])
            self.assertEqual("artifacts/runtime.backend.hcl", intent["runtime_backend_file"])
            self.assertEqual(
                hashlib.sha256((root / intent["runtime_backend_file"]).read_bytes()).hexdigest(),
                intent["runtime_backend_sha256"],
            )
            changed = dict(intent, runtime_state_bucket="different-state-bucket")
            self.assertNotEqual(digest(intent), digest(changed))

    def test_expiry_refuses_missing_state_bucket_before_planning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            with self.assertRaisesRegex(Refused, "runtime state bucket"):
                prepare_expiry(
                    CONFIG,
                    root,
                    "artifacts/vars.json",
                    runner=lambda *args, **kwargs: calls.append(args),
                )
            self.assertEqual([], calls)

    def test_expiry_rejects_backend_mismatch_before_terraform(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = runtime_backend_file(root, bucket="another-state-bucket")
            calls = []
            with self.assertRaisesRegex(Refused, "mismatches the approved state bucket"):
                prepare_expiry(
                    CONFIG,
                    root,
                    "artifacts/vars.json",
                    runner=lambda *args, **kwargs: calls.append(args),
                    runtime_state_bucket=RUNTIME_STATE_BUCKET,
                    runtime_backend_file=backend,
                )
            self.assertEqual([], calls)

    def test_expiry_refuses_backend_changed_after_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            intent, runner = self.prepare(root)
            backend_path = root / intent["runtime_backend_file"]
            backend_path.write_text(backend_path.read_text() + "# changed\n")
            adapter = FakeAdapter()
            intent["not_before"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            with self.assertRaisesRegex(Refused, "backend file changed"):
                execute_expiry(intent, digest(intent), root, adapter, runner)
            self.assertEqual([], adapter.calls)

    def test_expiry_backend_rejects_unapproved_credential_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = runtime_backend_file(root)
            backend_path = root / backend
            backend_path.write_text(backend_path.read_text() + 'access_key = "synthetic"\n')
            calls = []
            with self.assertRaisesRegex(Refused, "unsupported settings"):
                prepare_expiry(
                    CONFIG,
                    root,
                    "artifacts/vars.json",
                    runner=lambda *args, **kwargs: calls.append(args),
                    runtime_state_bucket=RUNTIME_STATE_BUCKET,
                    runtime_backend_file=backend,
                )
            self.assertEqual([], calls)

    def test_expiry_initializes_from_backend_snapshot_and_refuses_changed_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "infra/runtime").mkdir(parents=True)
            backend = runtime_backend_file(root)
            backend_path = root / backend
            calls = []

            def runner(argv, **kwargs):
                calls.append(argv)
                if "init" in argv:
                    backend_path.write_text(backend_path.read_text() + "# changed during init\n")
                return ""

            with self.assertRaisesRegex(Refused, "changed during Terraform initialization"):
                prepare_expiry(
                    CONFIG,
                    root,
                    "artifacts/vars.json",
                    runner=runner,
                    runtime_state_bucket=RUNTIME_STATE_BUCKET,
                    runtime_backend_file=backend,
                )
            self.assertEqual(1, len(calls))
            self.assertIn(
                f"-backend-config=bucket={RUNTIME_STATE_BUCKET}",
                calls[0],
            )
            self.assertIn("-backend-config=key=deepresearch/staging/runtime.tfstate", calls[0])
            self.assertNotIn("plan", calls[0])

    def test_expired_parent_intent_after_backup_prevents_destroy(self):
        from unittest.mock import patch

        clock = [datetime.now(timezone.utc)]
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("lifecycle.core.now", side_effect=lambda: clock[0]),
        ):
            root = Path(directory)
            intent, runner = self.prepare(root)
            intent["not_before"] = (clock[0] - timedelta(seconds=1)).isoformat()
            adapter = FakeAdapter()
            snapshot = adapter.snapshot_database

            def slow_snapshot(identifier):
                clock[0] += timedelta(hours=7)
                return snapshot(identifier)

            adapter.snapshot_database = slow_snapshot
            with self.assertRaisesRegex(Refused, "expired"):
                execute_expiry(intent, digest(intent), root, adapter, runner)
            self.assertIn("publish-backup", adapter.calls)
            self.assertNotIn("ingress", adapter.calls)
            self.assertNotIn("unprotect", adapter.calls)

    def test_derived_shutdown_expiry_cannot_exceed_parent_intent(self):
        from unittest.mock import patch

        instant = datetime.now(timezone.utc)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("lifecycle.core.now", return_value=instant),
        ):
            root = Path(directory)
            intent, runner = self.prepare(root)
            intent["not_before"] = (instant - timedelta(seconds=1)).isoformat()
            intent["expires_at"] = (instant + timedelta(seconds=30)).isoformat()
            with patch(
                "lifecycle.core.shutdown_execute", return_value={"executed": True}
            ) as shutdown:
                execute_expiry(intent, digest(intent), root, FakeAdapter(), runner)
            self.assertEqual(intent["expires_at"], shutdown.call_args.args[0]["expires_at"])


class AdmissionTimerTests(unittest.TestCase):
    def test_expiry_uses_admission_start_not_restarted_runtime_clock(self):
        from unittest.mock import patch

        instant = datetime.now(timezone.utc)
        admitted = int((instant - timedelta(hours=1)).timestamp())
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("lifecycle.core.now", return_value=instant),
        ):
            root = Path(directory)
            (root / "infra/runtime").mkdir(parents=True)
            saved = root / "artifacts/expiry.tfplan"

            def runner(argv, **kwargs):
                if "plan" in argv:
                    saved.write_bytes(b"approved")
                return json.dumps(tfplan()) if "show" in argv else ""

            intent = prepare_expiry(
                CONFIG, root, "artifacts/vars.json", runner=runner, admission_start=admitted,
                runtime_state_bucket=RUNTIME_STATE_BUCKET,
                runtime_backend_file=runtime_backend_file(root),
            )
            self.assertEqual(
                admitted + 14400, int(datetime.fromisoformat(intent["not_before"]).timestamp())
            )
            self.assertEqual(
                admitted + 21600, int(datetime.fromisoformat(intent["expires_at"]).timestamp())
            )
            calls = []
            with self.assertRaises(Refused):
                prepare_expiry(
                    CONFIG,
                    root,
                    "artifacts/vars.json",
                    runner=lambda *a, **k: calls.append(a),
                    admission_start=admitted - 86400,
                    runtime_state_bucket=RUNTIME_STATE_BUCKET,
                    runtime_backend_file=runtime_backend_file(root),
                )
            self.assertEqual([], calls)


if __name__ == "__main__":
    unittest.main()
