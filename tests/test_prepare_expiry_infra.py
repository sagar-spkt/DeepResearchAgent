"""Fake-runner tests for private archive upload/config preparation."""

import base64
import hashlib
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_backup_policy import CONFIG

from scripts.prepare_expiry_infra import PreparationError, prepare_tfvars, upload_archive


class PrepareExpiryInfraTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive = self.root / "source.zip"
        self.archive.write_bytes(b"private reviewed archive")
        self.kms = "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-4111-8111-111111111111"
        self.version = "version-abc123"
        self.commands = []

    def runner(self, argv):
        self.commands.append(argv)
        if argv[0] == "terraform":
            return json.dumps(self.runtime_plan)
        if argv[2] == "put-object":
            data = self.archive.read_bytes()
            checksum = base64.b64encode(hashlib.sha256(data).digest()).decode()
            return json.dumps({
                "VersionId": self.version,
                "ServerSideEncryption": "aws:kms",
                "SSEKMSKeyId": self.kms,
                "ChecksumSHA256": checksum,
            })
        raise AssertionError("unexpected command")

    def test_upload_uses_content_addressed_version_and_put_response_checksum(self):
        result = upload_archive(self.archive, "expiry-source-123456789012", self.kms, "session-1", self.runner)
        self.assertEqual(result["version_id"], self.version)
        self.assertIn(result["sha256"], result["key"])
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.commands[0][2], "put-object")
        self.assertNotIn("--metadata", self.commands[0])

    def test_upload_refuses_unversioned_or_mismatched_put_response(self):
        with self.assertRaisesRegex(PreparationError, "exact version/KMS/checksum verification"):
            upload_archive(self.archive, "expiry-source-123456789012", self.kms, "session-1", lambda _: "{}")
        def bad_put(argv):
            data = self.archive.read_bytes()
            return json.dumps({
                "VersionId": self.version,
                "ServerSideEncryption": "AES256",
                "SSEKMSKeyId": self.kms,
                "ChecksumSHA256": base64.b64encode(hashlib.sha256(data).digest()).decode(),
            })
        with self.assertRaisesRegex(PreparationError, "failed exact version/KMS/checksum verification"):
            upload_archive(self.archive, "expiry-source-123456789012", self.kms, "session-1", bad_put)

    def write_json(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        return path

    def test_prepares_private_tfvars_bound_to_exact_intent_plan_and_upload(self):
        binary_plan = b"approved saved plan"
        self.runtime_plan = {"resource_changes": [{"address": "aws_vpc.main", "change": {"before": {"id": "vpc-abcdef01234567890"}}}]}
        intent = {
            "version": 1, "operation": "expiry", "config": CONFIG,
            "runtime_state_bucket": "state-bucket-123456789012",
            "runtime_state_key": "deepresearch/staging/runtime.tfstate",
            "not_before": "2026-11-17T22:00:00+00:00",
            "saved_plan_sha256": hashlib.sha256(binary_plan).hexdigest(),
            "deletions": ["aws_vpc.main"],
        }
        approved = hashlib.sha256(json.dumps(intent, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        inputs = {
            "approved_intent_sha256": approved,
            "runtime_state_bucket": "state-bucket-123456789012",
            "management_vpc_id": "vpc-0123456789abcdef0",
            "management_subnet_ids": ["subnet-0123456789abcdef0"],
            "management_security_group_ids": ["sg-0123456789abcdef0"],
            "source_bucket": "expiry-source-123456789012",
            "source_kms_key_arn": self.kms,
            "runner_image_digest": "expiry-executor@sha256:" + "a" * 64,
            "runner_repository_arn": "arn:aws:ecr:us-east-1:123456789012:repository/expiry-executor",
        }
        artifacts = self.root / "artifacts"
        artifacts.mkdir()
        (artifacts / "expiry.tfplan").write_bytes(binary_plan)
        intent_path = self.write_json("intent.json", intent)
        inputs_path = self.write_json("inputs.json", inputs)
        output = self.root / "expiry.auto.tfvars.json"
        with patch("scripts.prepare_expiry_infra.inspect_destroy", return_value=["aws_vpc.main"]), \
             patch("scripts.prepare_expiry_infra.expiry_executor_policies", return_value=[{"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "sts:GetCallerIdentity", "Resource": "*"}]}]), \
             patch("scripts.prepare_expiry_infra._verify_archive"):
            result, summary = prepare_tfvars(intent_path, inputs_path, self.archive, output, self.runner,
                                             workspace=self.root)
        value = json.loads(result.read_text())
        self.assertEqual(value["expire_at_utc"], "2026-11-17T22:00:00Z")
        self.assertEqual(value["runtime_vpc_id"], "vpc-abcdef01234567890")
        self.assertEqual(value["source_version_id"], self.version)
        self.assertEqual(len(value["executor_policy_documents"]), 1)
        self.assertEqual(summary["intent_sha256"], approved)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_rejects_mismatched_intent_before_upload(self):
        intent = {
            "version": 1, "operation": "expiry", "config": CONFIG,
            "runtime_state_bucket": "state-bucket-123456789012",
            "runtime_state_key": "deepresearch/staging/runtime.tfstate",
            "not_before": "2026-11-17T22:00:00+00:00",
        }
        inputs = {
            "approved_intent_sha256": "0" * 64,
            "runtime_state_bucket": "state-bucket-123456789012",
        }
        with self.assertRaisesRegex(PreparationError, "explicitly reviewed digest"):
            prepare_tfvars(self.write_json("intent.json", intent), self.write_json("inputs.json", inputs),
                           self.archive, self.root / "out.json", self.runner, workspace=self.root)


if __name__ == "__main__":
    unittest.main()
