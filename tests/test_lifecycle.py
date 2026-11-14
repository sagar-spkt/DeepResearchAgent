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
}


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

            intent = prepare_expiry(CONFIG, root, "artifacts/vars.json", runner=runner)
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

            intent = prepare_expiry(CONFIG, root, "artifacts/vars.json", runner=runner)
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
    def test_wrong_account_rejected(self):
        adapter = AWSAdapter(
            CONFIG,
            {"sts": SimpleNamespace(get_caller_identity=lambda: {"Account": "000000000000"})},
        )
        with self.assertRaises(Refused):
            adapter.identity()

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

        return prepare_expiry(CONFIG, root, "artifacts/vars.json", runner=runner), runner

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
                CONFIG, root, "artifacts/vars.json", runner=runner, admission_start=admitted
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
                )
            self.assertEqual([], calls)


if __name__ == "__main__":
    unittest.main()
