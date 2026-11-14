import copy
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lifecycle.aws_adapter import AWSAdapter
from lifecycle.control import ControlStore, month_reservations
from lifecycle.core import Refused

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
}


class FakeDynamo:
    """Conditional storage fixture; live DynamoDB semantics need a separate test."""

    def __init__(self):
        self.items = {}
        self.lock = threading.Lock()

    def describe_table(self, **_):
        return {
            "Table": {
                "TableArn": "arn:aws:dynamodb:us-east-1:123456789012:table/deepresearch-control",
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

    def get_item(self, **kw):
        with self.lock:
            return {"Item": copy.deepcopy(self.items.get(kw["Key"]["key"]["S"], {}))}

    def transact_write_items(self, **kw):
        with self.lock:
            pending = copy.deepcopy(self.items)
            for action in kw["TransactItems"]:
                if "ConditionCheck" in action:
                    key = action["ConditionCheck"]["Key"]["key"]["S"]
                    if key in pending:
                        raise ValueError("transaction condition failed")
                elif "Update" in action:
                    update = action["Update"]
                    key = update["Key"]["key"]["S"]
                    values = update["ExpressionAttributeValues"]
                    current = int(pending.get(key, {}).get("reserved_seconds", {}).get("N", "0"))
                    if current > int(values[":remaining"]["N"]):
                        raise ValueError("transaction condition failed")
                    pending[key] = {
                        "key": {"S": key},
                        "reserved_seconds": {"N": str(current + int(values[":amount"]["N"]))},
                    }
                else:
                    item = action["Put"]["Item"]
                    key = item["key"]["S"]
                    if key in pending:
                        raise ValueError("transaction condition failed")
                    pending[key] = copy.deepcopy(item)
            self.items = pending

    def update_item(self, **kw):
        with self.lock:
            key = kw["Key"]["key"]["S"]
            values = kw["ExpressionAttributeValues"]
            item = self.items.get(key, {})
            if ":session" in values:
                if item.get("owner_id") and int(item["expires_at"]["N"]) >= int(
                    values[":now"]["N"]
                ):
                    raise ValueError("lock held")
                item = {
                    "key": {"S": key},
                    "owner_id": values[":owner"],
                    "expires_at": values[":expires"],
                    "session_id": values[":session"],
                }
            else:
                if item.get("owner_id") != values[":owner"] or int(
                    item.get("expires_at", {}).get("N", "0")
                ) <= int(values[":now"]["N"]):
                    raise ValueError("lock lost")
                item["expires_at"] = values[":expires"]
            self.items[key] = item

    def delete_item(self, **kw):
        with self.lock:
            key = kw["Key"]["key"]["S"]
            if self.items.get(key, {}).get("owner_id") != kw["ExpressionAttributeValues"][":owner"]:
                raise ValueError("not owner")
            self.items.pop(key, None)


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.clock = [int(datetime(2026, 11, 13, tzinfo=timezone.utc).timestamp())]
        self.client = FakeDynamo()
        self.store = ControlStore(self.client, clock=lambda: self.clock[0])

    def test_competing_environments_admit_only_one_without_double_reservation(self):
        other = {
            **CONFIG,
            "environment": "production",
            "cluster_name": "deepresearch-production",
            "database_identifier": "deepresearch-production",
            "document_bucket": "deepresearch-production-documents-123456789012",
            "backup_bucket": "deepresearch-production-backups-123456789012",
            "trace_bucket": "deepresearch-production-traces-123456789012",
        }

        def attempt(config):
            try:
                return self.store.reserve(config, self.clock[0])["admitted"]
            except Refused:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(attempt, [CONFIG, other]))
        self.assertEqual(1, sum(result))
        self.assertEqual("21600", self.client.items["month#2026-11"]["reserved_seconds"]["N"])

    def test_twenty_hour_cap_fails_atomically(self):
        self.client.items["month#2026-11"] = {"reserved_seconds": {"N": "64800"}}
        with self.assertRaises(Refused):
            self.store.reserve(CONFIG, self.clock[0])
        self.assertNotIn("active-session", self.client.items)
        self.assertEqual("64800", self.client.items["month#2026-11"]["reserved_seconds"]["N"])

    def test_completed_session_id_cannot_be_readmitted_or_reserve_more_time(self):
        self.client.items["completion#staging#session-1"] = {"receipt_json": {"S": "{}"}}
        self.client.items["month#2026-11"] = {"reserved_seconds": {"N": "21600"}}
        with self.assertRaises(Refused):
            self.store.reserve(CONFIG, self.clock[0])
        self.assertNotIn("active-session", self.client.items)
        self.assertEqual("21600", self.client.items["month#2026-11"]["reserved_seconds"]["N"])

    def test_reservations_split_at_utc_month_boundary(self):
        start = int(datetime(2026, 12, 31, 23, tzinfo=timezone.utc).timestamp())
        self.assertEqual({"2026-12": 3600, "2027-01": 18000}, month_reservations(start, 21600))

    def test_wrong_or_missing_admission_blocks_lease(self):
        with self.assertRaises(Refused):
            self.store.acquire(CONFIG)
        self.store.reserve(CONFIG, self.clock[0])
        with self.assertRaises(Refused):
            self.store.acquire({**CONFIG, "session_id": "other"})

    def test_lease_competition_renewal_and_lost_holder_cannot_release_new_holder(self):
        self.store.reserve(CONFIG, self.clock[0])
        first = self.store.acquire(CONFIG)
        with self.assertRaises(Refused):
            self.store.acquire(CONFIG)
        self.clock[0] += 30
        first.renew()
        first.assert_owned()
        self.clock[0] += 121
        second = self.store.acquire(CONFIG)
        with self.assertRaises(Refused):
            first.assert_owned()
        first.__exit__(None, None, None)
        second.assert_owned()
        second.__exit__(None, None, None)
        self.assertNotIn("lifecycle-lock", self.client.items)
        self.assertIn("active-session", self.client.items)

    def test_missing_or_lost_lease_prevents_snapshot_call(self):
        adapter = AWSAdapter(CONFIG, {})
        with self.assertRaises(Refused):
            adapter.snapshot_database("snapshot")
        self.store.reserve(CONFIG, self.clock[0])
        lease = self.store.acquire(CONFIG)
        adapter.exclusive_guard = lease.assert_owned
        self.clock[0] += 121
        with self.assertRaises(Refused):
            adapter.snapshot_database("snapshot")

    def test_expired_admission_is_never_automatically_freed(self):
        self.store.reserve(CONFIG, self.clock[0])
        self.clock[0] += 86400
        with self.assertRaises(Refused):
            self.store.reserve({**CONFIG, "session_id": "next"}, self.clock[0])


class BoundaryTests(unittest.TestCase):
    def test_release_plan_is_private_pure_and_requires_explicit_execution(self):
        import contextlib
        import io
        import json
        import tempfile
        from unittest.mock import patch

        import platform_lifecycle

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text(json.dumps(CONFIG))
            output = Path(directory) / "release-plan.json"
            argv = [
                "platform_lifecycle.py",
                "plan-release",
                "--config",
                str(config),
                "--output",
                str(output),
            ]
            with (
                patch.object(sys, "argv", argv),
                patch.object(
                    platform_lifecycle, "adapter", side_effect=AssertionError("no clients")
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(0, platform_lifecycle.main())
            self.assertEqual("release", json.loads(output.read_text())["operation"])
            self.assertEqual(0o600, output.stat().st_mode & 0o777)
            receipt = Path(directory) / "completion.json"
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "platform_lifecycle.py",
                        "release",
                        "--plan",
                        str(output),
                        "--output",
                        str(receipt),
                    ],
                ),
                patch.object(
                    platform_lifecycle, "adapter", side_effect=AssertionError("no clients")
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(2, platform_lifecycle.main())
            self.assertFalse(receipt.exists())

    def test_bad_release_approval_fails_before_clients_or_journal(self):
        import contextlib
        import io
        import json
        import tempfile
        from unittest.mock import patch

        import platform_lifecycle
        from lifecycle.inventory import release_plan

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "plan.json"
            source.write_text(json.dumps(release_plan(CONFIG)))
            output = Path(directory) / "receipt.json"
            argv = [
                "platform_lifecycle.py",
                "release",
                "--plan",
                str(source),
                "--output",
                str(output),
                "--approved-plan-hash",
                "0" * 64,
                "--execute",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch.object(
                    platform_lifecycle, "adapter", side_effect=AssertionError("no clients")
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(2, platform_lifecycle.main())
            self.assertFalse(output.exists())

    def test_correct_hash_for_wrong_operation_fails_before_clients_or_journal(self):
        import contextlib
        import io
        import json
        import tempfile
        from unittest.mock import patch

        import platform_lifecycle
        from lifecycle.core import backup_plan, digest

        plan = backup_plan(CONFIG)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "plan.json"
            source.write_text(json.dumps(plan))
            for operation in ("release", "expiry", "shutdown", "admit"):
                with self.subTest(operation=operation):
                    output = Path(directory) / (operation + ".json")
                    argv = [
                        "platform_lifecycle.py",
                        operation,
                        "--plan",
                        str(source),
                        "--output",
                        str(output),
                        "--approved-plan-hash",
                        digest(plan),
                        "--execute",
                    ]
                    with (
                        patch.object(sys, "argv", argv),
                        patch.object(
                            platform_lifecycle, "adapter", side_effect=AssertionError("no clients")
                        ),
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        self.assertEqual(2, platform_lifecycle.main())
                    self.assertFalse(output.exists())

    def test_mutation_environment_conflict_fails_before_clients_or_journal(self):
        import contextlib
        import io
        import json
        import tempfile
        from unittest.mock import patch

        import platform_lifecycle
        from lifecycle.core import backup_plan, digest

        plan = backup_plan(CONFIG)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "plan.json"
            source.write_text(json.dumps(plan))
            output = Path(directory) / "receipt.json"
            argv = [
                "platform_lifecycle.py",
                "backup",
                "--environment",
                "production",
                "--plan",
                str(source),
                "--output",
                str(output),
                "--approved-plan-hash",
                digest(plan),
                "--execute",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch.object(
                    platform_lifecycle,
                    "adapter",
                    side_effect=AssertionError("must not create clients"),
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(2, platform_lifecycle.main())
            self.assertFalse(output.exists())

    def test_lease_lost_during_valkey_copy_blocks_s3_write(self):
        from types import SimpleNamespace

        calls = []

        def runner(argv):
            if "cp" in argv:
                Path(argv[argv.index("cp") + 2]).write_bytes(b"synthetic valkey")
            return ""

        def guard():
            calls.append("guard")
            if len(calls) >= 3:
                raise Refused("lease lost during copy")

        def forbidden(**_):
            self.fail("S3 put after lease loss")

        adapter = AWSAdapter(CONFIG, {"s3": SimpleNamespace(put_object=forbidden)}, runner=runner)
        adapter.exclusive_guard = guard
        with self.assertRaisesRegex(Refused, "lease lost"):
            adapter.backup_valkey("sessions/staging/session-1/")


if __name__ == "__main__":
    unittest.main()
