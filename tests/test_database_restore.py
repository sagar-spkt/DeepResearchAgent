from __future__ import annotations

import copy
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from lifecycle.aws_adapter import AWSAdapter
from lifecycle.core import (
    Refused,
    digest,
    execute_database_restore,
    prepare_database_restore,
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
}


def backup_receipt():
    created = datetime.now(timezone.utc)
    identifier = "deepresearch-staging-session-1-backup"
    arn = f"arn:aws:rds:us-east-1:123456789012:snapshot:{identifier}"
    return {
        "version": 1,
        "operation": "backup-receipt",
        "config": copy.deepcopy(CONFIG),
        "created_at": created.isoformat(),
        "expires_at": (created + timedelta(hours=24)).isoformat(),
        "drain": {"verified_at": (created - timedelta(seconds=1)).isoformat()},
        "database_snapshot": {
            "identifier": identifier,
            "arn": arn,
            "created_at": created.isoformat(),
        },
        "clickhouse": {"url": "s3://private-backup/clickhouse/"},
        "valkey": {"version_id": "version-1"},
        "identities_retained": True,
        "restore_rehearsed": False,
    }


class DBInstanceNotFoundFault(Exception):
    pass


class FakeWaiter:
    def __init__(self, rds):
        self.rds = rds

    def wait(self, **kwargs):
        self.rds.wait_calls.append(kwargs)
        if self.rds.wait_fails:
            raise RuntimeError("simulated waiter failure")


class FakeRDS:
    def __init__(self, config):
        self.config = config
        self.exceptions = SimpleNamespace(DBInstanceNotFoundFault=DBInstanceNotFoundFault)
        self.snapshot = None
        self.snapshot_tags = {
            "Project": "deepresearch",
            "Environment": config["environment"],
            "Session": config["session_id"],
        }
        self.subnet_group_tags = dict(self.snapshot_tags)
        self.target_tags = {}
        self.target_exists = False
        self.wait_fails = False
        self.target_overrides = {}
        self.restore_calls = []
        self.wait_calls = []

    def describe_db_snapshots(self, **kwargs):
        return {"DBSnapshots": [copy.deepcopy(self.snapshot)]}

    def describe_db_subnet_groups(self, **kwargs):
        return {
            "DBSubnetGroups": [
                {
                    "DBSubnetGroupName": self.config["database_identifier"],
                    "DBSubnetGroupArn": "arn:aws:rds:us-east-1:123456789012:subgrp:deepresearch-staging",
                    "VpcId": "vpc-12345678",
                    "SubnetGroupStatus": "Complete",
                    "Subnets": [
                        {"SubnetIdentifier": "subnet-12345678", "SubnetStatus": "Active"},
                        {"SubnetIdentifier": "subnet-87654321", "SubnetStatus": "Active"},
                    ],
                }
            ]
        }

    def describe_db_instances(self, DBInstanceIdentifier):
        if self.target_exists:
            return {"DBInstances": [self.target_instance(DBInstanceIdentifier)]}
        if self.restore_calls:
            return {"DBInstances": [self.target_instance(DBInstanceIdentifier)]}
        raise DBInstanceNotFoundFault()

    def target_instance(self, target):
        values = {
            "DBInstanceIdentifier": target,
            "DBInstanceArn": f"arn:aws:rds:us-east-1:123456789012:db:{target}",
            "DBInstanceStatus": "available",
            "DBInstanceClass": "db.t4g.small",
            "StorageEncrypted": True,
            "PubliclyAccessible": False,
            "DBSubnetGroup": {"DBSubnetGroupName": self.config["database_identifier"]},
            "VpcSecurityGroups": [{"VpcSecurityGroupId": "sg-12345678", "Status": "active"}],
        }
        values.update(self.target_overrides)
        return values

    def list_tags_for_resource(self, ResourceName):
        if ":snapshot:" in ResourceName:
            return {"TagList": [{"Key": k, "Value": v} for k, v in self.snapshot_tags.items()]}
        if ":subgrp:" in ResourceName:
            return {"TagList": [{"Key": k, "Value": v} for k, v in self.subnet_group_tags.items()]}
        return {"TagList": [{"Key": k, "Value": v} for k, v in self.target_tags.items()]}

    def restore_db_instance_from_db_snapshot(self, **kwargs):
        self.restore_calls.append(kwargs)
        self.target_tags = {item["Key"]: item["Value"] for item in kwargs["Tags"]}

    def get_waiter(self, name):
        assert name == "db_instance_available"
        return FakeWaiter(self)


class FakeEC2:
    def __init__(self):
        self.security_group = None
        self.create_calls = []
        self.revoke_calls = []

    def describe_subnets(self, **kwargs):
        return {
            "Subnets": [
                {
                    "SubnetId": "subnet-12345678",
                    "VpcId": "vpc-12345678",
                    "MapPublicIpOnLaunch": False,
                },
                {
                    "SubnetId": "subnet-87654321",
                    "VpcId": "vpc-12345678",
                    "MapPublicIpOnLaunch": False,
                },
            ]
        }

    def describe_route_tables(self, **kwargs):
        return {
            "RouteTables": [
                {
                    "Associations": [{"Main": True}],
                    "Routes": [{"DestinationCidrBlock": "10.60.0.0/16", "GatewayId": "local"}],
                }
            ]
        }

    def describe_security_groups(self, **kwargs):
        if self.security_group is None:
            return {"SecurityGroups": []}
        return {"SecurityGroups": [copy.deepcopy(self.security_group)]}

    def create_security_group(self, **kwargs):
        self.create_calls.append(kwargs)
        self.security_group = {
            "GroupId": "sg-12345678",
            "GroupName": kwargs["GroupName"],
            "VpcId": kwargs["VpcId"],
            "Tags": kwargs["TagSpecifications"][0]["Tags"],
            "IpPermissions": [],
            "IpPermissionsEgress": [{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}],
        }
        return {"GroupId": "sg-12345678"}

    def revoke_security_group_egress(self, **kwargs):
        self.revoke_calls.append(kwargs)
        self.security_group["IpPermissionsEgress"] = []
        return {}


class FakeRestoreAdapter(AWSAdapter):
    def __init__(self, receipt):
        self.rds = FakeRDS(receipt["config"])
        self.ec2 = FakeEC2()
        snap = receipt["database_snapshot"]
        self.rds.snapshot = {
            "DBSnapshotIdentifier": snap["identifier"],
            "DBSnapshotArn": snap["arn"],
            "DBInstanceIdentifier": receipt["config"]["database_identifier"],
            "Status": "available",
            "Encrypted": True,
            "SnapshotCreateTime": datetime.fromisoformat(snap["created_at"]),
        }
        clients = {"rds": self.rds, "ec2": self.ec2}
        super().__init__(receipt["config"], clients)
        self.exclusive_guard = lambda: None

    def verify_receipt(self, receipt, **kwargs):
        if receipt["config"] != self.config:
            raise Refused("receipt config mismatch")
        self.verify_backup_snapshot(receipt)


class DatabaseRestoreTests(unittest.TestCase):
    def test_restores_exact_snapshot_to_private_session_target_without_accepting_full_recovery(
        self,
    ):
        receipt = backup_receipt()
        before = copy.deepcopy(receipt)
        plan = prepare_database_restore(receipt)
        adapter = FakeRestoreAdapter(receipt)
        result = execute_database_restore(plan, receipt, digest(plan), adapter)

        call = adapter.rds.restore_calls[0]
        self.assertEqual(call["DBInstanceIdentifier"], "deepresearch-staging-restore-session-1")
        self.assertEqual(call["DBSnapshotIdentifier"], receipt["database_snapshot"]["identifier"])
        self.assertEqual(call["DBSubnetGroupName"], CONFIG["database_identifier"])
        self.assertEqual(call["VpcSecurityGroupIds"], ["sg-12345678"])
        self.assertEqual(adapter.ec2.create_calls[0]["VpcId"], "vpc-12345678")
        self.assertEqual(len(adapter.ec2.revoke_calls), 1)
        self.assertFalse(call["PubliclyAccessible"])
        self.assertFalse(call["MultiAZ"])
        self.assertEqual(
            adapter.rds.wait_calls[0]["DBInstanceIdentifier"], call["DBInstanceIdentifier"]
        )
        self.assertEqual(result["source_database_identifier"], CONFIG["database_identifier"])
        self.assertEqual(result["source_snapshot_arn"], receipt["database_snapshot"]["arn"])
        self.assertTrue(result["database_restore_verified"])
        self.assertFalse(result["restore_rehearsed"])
        self.assertFalse(result["application_restore_rehearsed"])
        self.assertFalse(result["full_recovery_accepted"])
        self.assertEqual(receipt, before)

    def test_rejects_wrong_snapshot_session_or_encryption_before_restore(self):
        for mutation, message in (
            (
                lambda r, a: setattr(
                    a.rds, "snapshot", {**a.rds.snapshot, "DBSnapshotArn": "arn:wrong"}
                ),
                "snapshot verification",
            ),
            (
                lambda r, a: a.rds.snapshot_tags.update(Session="other-session"),
                "snapshot verification",
            ),
            (lambda r, a: a.rds.snapshot.update(Encrypted=False), "snapshot verification"),
        ):
            with self.subTest(message=message):
                receipt = backup_receipt()
                plan = prepare_database_restore(receipt)
                adapter = FakeRestoreAdapter(receipt)
                mutation(receipt, adapter)
                with self.assertRaisesRegex(Refused, message):
                    execute_database_restore(plan, receipt, digest(plan), adapter)
                self.assertEqual(adapter.rds.restore_calls, [])

    def test_refuses_existing_restore_target_without_replacing_it(self):
        receipt = backup_receipt()
        plan = prepare_database_restore(receipt)
        adapter = FakeRestoreAdapter(receipt)
        adapter.rds.target_exists = True
        with self.assertRaisesRegex(Refused, "already exists"):
            execute_database_restore(plan, receipt, digest(plan), adapter)
        self.assertEqual(adapter.rds.restore_calls, [])
        self.assertEqual(adapter.ec2.create_calls, [])
        self.assertEqual(adapter.ec2.revoke_calls, [])

    def test_waiter_failure_does_not_issue_restore_success(self):
        receipt = backup_receipt()
        plan = prepare_database_restore(receipt)
        adapter = FakeRestoreAdapter(receipt)
        adapter.rds.wait_fails = True
        with self.assertRaisesRegex(RuntimeError, "waiter failure"):
            execute_database_restore(plan, receipt, digest(plan), adapter)
        self.assertEqual(len(adapter.rds.restore_calls), 1)

    def test_refuses_existing_restore_security_group_with_ingress(self):
        receipt = backup_receipt()
        plan = prepare_database_restore(receipt)
        adapter = FakeRestoreAdapter(receipt)
        adapter.ec2.security_group = {
            "GroupId": "sg-12345678",
            "GroupName": plan["restore_security_group_name"],
            "VpcId": "vpc-12345678",
            "Tags": [
                {"Key": key, "Value": value}
                for key, value in {
                    **plan["tags"],
                    "Component": "database-restore-security-group",
                }.items()
            ],
            "IpPermissions": [{"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432}],
            "IpPermissionsEgress": [],
        }
        with self.assertRaisesRegex(Refused, "ingress-isolated"):
            execute_database_restore(plan, receipt, digest(plan), adapter)
        self.assertEqual(adapter.rds.restore_calls, [])

    def test_rejects_provider_attaching_unexpected_security_group(self):
        receipt = backup_receipt()
        plan = prepare_database_restore(receipt)
        adapter = FakeRestoreAdapter(receipt)
        adapter.rds.target_overrides = {
            "VpcSecurityGroups": [{"VpcSecurityGroupId": "sg-wrong1234", "Status": "active"}]
        }
        with self.assertRaisesRegex(Refused, "provider state"):
            execute_database_restore(plan, receipt, digest(plan), adapter)

    def test_lease_is_rechecked_before_security_group_mutations(self):
        for guard_failure, expected_create, expected_revoke in (
            (2, 0, 0),
            (3, 1, 0),
        ):
            with self.subTest(guard_failure=guard_failure):
                receipt = backup_receipt()
                plan = prepare_database_restore(receipt)
                adapter = FakeRestoreAdapter(receipt)
                guard_calls = 0

                def guard():
                    nonlocal guard_calls
                    guard_calls += 1
                    if guard_calls == guard_failure:
                        raise Refused("lease lost")

                adapter.exclusive_guard = guard
                with self.assertRaisesRegex(Refused, "lease lost"):
                    execute_database_restore(plan, receipt, digest(plan), adapter)
                self.assertEqual(len(adapter.ec2.create_calls), expected_create)
                self.assertEqual(len(adapter.ec2.revoke_calls), expected_revoke)
                self.assertEqual(adapter.rds.restore_calls, [])

    def test_plan_rejects_snapshot_outside_receipt_account_region_or_source(self):
        receipt = backup_receipt()
        receipt["database_snapshot"]["arn"] = "arn:aws:rds:us-west-2:123456789012:snapshot:foreign"
        with self.assertRaisesRegex(Refused, "identity"):
            prepare_database_restore(receipt)


if __name__ == "__main__":
    unittest.main()
