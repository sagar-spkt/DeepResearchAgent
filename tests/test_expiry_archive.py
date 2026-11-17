import hashlib
import json
import stat
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from zipfile import ZipFile

from scripts.package_expiry_archive import ArchiveError, build_archive, canonical_digest


class ExpiryArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "source"
        (self.root / "scripts/lifecycle").mkdir(parents=True)
        (self.root / "infra/runtime").mkdir(parents=True)
        (self.root / "scripts/platform_lifecycle.py").write_text("# lifecycle entrypoint\n")
        (self.root / "scripts/lifecycle/core.py").write_text("# lifecycle core\n")
        (self.root / "infra/runtime/main.tf").write_text("terraform {}\n")
        (self.root / "infra/runtime/.terraform.lock.hcl").write_text("provider lock\n")
        (self.root / "infra/runtime/aws-load-balancer-controller-policy.json").write_text("{}\n")
        artifacts = self.root / "artifacts"
        artifacts.mkdir()
        plan = b"synthetic binary plan"
        (artifacts / "expiry.tfplan").write_bytes(plan)
        (artifacts / "runtime.auto.tfvars.json").write_text(
            json.dumps(
                {
                    "account_id": "123456789012",
                    "environment": "staging",
                    "availability_zones": ["us-east-1a", "us-east-1b"],
                    "administrator_cidrs": ["198.51.100.10/32"],
                    "operator_principal_arn": "arn:aws:iam::123456789012:user/test-operator",
                    "kubernetes_version": "1.37",
                    "session_id": "stg-20261117",
                    "foundation": {
                        "buckets": {
                            "documents": "deepresearch-staging-documents-123456789012",
                            "backups": "deepresearch-staging-backups-123456789012",
                            "traces": "deepresearch-staging-traces-123456789012",
                        },
                        "queue_url": "https://sqs.us-east-1.amazonaws.com/123456789012/deepresearch-staging-research",
                        "queue_arn": "arn:aws:sqs:us-east-1:123456789012:deepresearch-staging-research",
                        "alert_topic_arn": "arn:aws:sns:us-east-1:123456789012:deepresearch-staging-alerts",
                        "alert_kms_key_arn": "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-4111-8111-111111111111",
                        "secrets": {
                            name: f"arn:aws:secretsmanager:us-east-1:123456789012:secret:deepresearch-staging/{name}-abcd12"
                            for name in ["application", "vllm", "langfuse", "web"]
                        },
                    },
                }
            )
        )
        (artifacts / "runtime.backend.hcl").write_text(
            'bucket = "retained-state-bucket"\n'
            'key = "deepresearch/staging/runtime.tfstate"\n'
            'region = "us-east-1"\n'
            "encrypt = true\n"
            "use_lockfile = true\n"
        )
        self.intent = {
            "version": 1,
            "operation": "expiry",
            "config": {
                "environment": "staging",
                "session_id": "stg-20261117",
                "account_id": "123456789012",
                "region": "us-east-1",
            },
            "runtime_state_bucket": "retained-state-bucket",
            "runtime_state_key": "deepresearch/staging/runtime.tfstate",
            "runtime_backend_file": "artifacts/runtime.backend.hcl",
            "runtime_backend_sha256": hashlib.sha256(
                (artifacts / "runtime.backend.hcl").read_bytes()
            ).hexdigest(),
            "saved_plan": "artifacts/expiry.tfplan",
            "saved_plan_sha256": hashlib.sha256(plan).hexdigest(),
            "receipt": "artifacts/expiry-backup-receipt.json",
        }
        (artifacts / "expiry-intent.json").write_text(json.dumps(self.intent))

    def args(self, output="archive.zip"):
        return Namespace(
            session_id="stg-20261117",
            approved_intent_sha256=canonical_digest(self.intent),
            intent="artifacts/expiry-intent.json",
            saved_plan="artifacts/expiry.tfplan",
            runtime_variables="artifacts/runtime.auto.tfvars.json",
            runtime_backend="artifacts/runtime.backend.hcl",
            approved_state_bucket="retained-state-bucket",
            output=str(Path(self.temporary.name) / output),
        )

    def test_archive_is_deterministic_private_and_contains_only_reviewed_inputs(self):
        first, first_hash = build_archive(self.root, self.args("one.zip"))
        second, second_hash = build_archive(self.root, self.args("two.zip"))

        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(first_hash, second_hash)
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o600)
        with ZipFile(first) as archive:
            names = set(archive.namelist())
            self.assertEqual(
                names,
                {
                    "scripts/platform_lifecycle.py",
                    "scripts/lifecycle/core.py",
                    "infra/runtime/main.tf",
                    "infra/runtime/.terraform.lock.hcl",
                    "infra/runtime/aws-load-balancer-controller-policy.json",
                    "artifacts/expiry-intent.json",
                    "artifacts/expiry.tfplan",
                    "artifacts/runtime.auto.tfvars.json",
                    "artifacts/runtime.backend.hcl",
                    "infra/runtime/artifacts/runtime.backend.hcl",
                },
            )
            self.assertEqual(archive.getinfo("artifacts/expiry.tfplan").date_time, (1980, 1, 1, 0, 0, 0))
            self.assertEqual(archive.getinfo("artifacts/expiry.tfplan").compress_type, 0)
            self.assertEqual(
                archive.read("artifacts/runtime.backend.hcl"),
                archive.read("infra/runtime/artifacts/runtime.backend.hcl"),
            )

    def test_refuses_intent_digest_mismatch(self):
        args = self.args()
        args.approved_intent_sha256 = "0" * 64
        with self.assertRaisesRegex(ArchiveError, "explicitly approved digest"):
            build_archive(self.root, args)

    def test_refuses_tampered_saved_plan(self):
        (self.root / "artifacts/expiry.tfplan").write_bytes(b"changed")
        with self.assertRaisesRegex(ArchiveError, "does not match the expiry intent"):
            build_archive(self.root, self.args())

    def test_refuses_runtime_session_mismatch(self):
        variables = json.loads((self.root / "artifacts/runtime.auto.tfvars.json").read_text())
        variables["session_id"] = "another-session"
        (self.root / "artifacts/runtime.auto.tfvars.json").write_text(json.dumps(variables))
        with self.assertRaisesRegex(ArchiveError, "do not match"):
            build_archive(self.root, self.args())

    def test_refuses_credential_or_unknown_runtime_fields(self):
        variables = json.loads((self.root / "artifacts/runtime.auto.tfvars.json").read_text())
        variables["api_key"] = "synthetic-secret"
        (self.root / "artifacts/runtime.auto.tfvars.json").write_text(json.dumps(variables))
        with self.assertRaisesRegex(ArchiveError, "unsupported fields"):
            build_archive(self.root, self.args())

    def test_refuses_wrong_state_key_or_unencrypted_backend(self):
        backend = self.root / "artifacts/runtime.backend.hcl"
        backend.write_text(backend.read_text().replace("encrypt = true", "encrypt = false"))
        self.intent["runtime_backend_sha256"] = hashlib.sha256(backend.read_bytes()).hexdigest()
        (self.root / "artifacts/expiry-intent.json").write_text(json.dumps(self.intent))
        with self.assertRaisesRegex(ArchiveError, "encryption must be enabled"):
            build_archive(self.root, self.args())

    def test_refuses_backend_bucket_that_differs_from_approved_bucket(self):
        backend = self.root / "artifacts/runtime.backend.hcl"
        backend.write_text(backend.read_text().replace("retained-state-bucket", "another-state-bucket"))
        self.intent["runtime_backend_sha256"] = hashlib.sha256(backend.read_bytes()).hexdigest()
        (self.root / "artifacts/expiry-intent.json").write_text(json.dumps(self.intent))
        args = self.args()
        with self.assertRaisesRegex(ArchiveError, "match the explicitly approved retained state bucket"):
            build_archive(self.root, args)

    def test_refuses_state_bucket_not_bound_by_approved_intent(self):
        self.intent["runtime_state_bucket"] = "another-state-bucket"
        (self.root / "artifacts/expiry-intent.json").write_text(json.dumps(self.intent))
        with self.assertRaisesRegex(ArchiveError, "bind the explicitly approved"):
            build_archive(self.root, self.args())

    def test_refuses_symlinked_input_and_traversal(self):
        outside = Path(self.temporary.name) / "outside.json"
        outside.write_text(json.dumps(self.intent))
        (self.root / "artifacts/linked-intent.json").symlink_to(outside)
        args = self.args()
        args.intent = "artifacts/linked-intent.json"
        with self.assertRaisesRegex(ArchiveError, "symlink"):
            build_archive(self.root, args)
        args.intent = "../outside.json"
        with self.assertRaisesRegex(ArchiveError, "escapes"):
            build_archive(self.root, args)

    def test_refuses_symlinked_source_directory(self):
        lifecycle = self.root / "scripts/lifecycle"
        saved = self.root / "scripts/lifecycle-original"
        lifecycle.rename(saved)
        outside = Path(self.temporary.name) / "outside-source"
        outside.mkdir()
        (outside / "injected.py").write_text("private content\n")
        lifecycle.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ArchiveError, "symlink"):
            build_archive(self.root, self.args())

    def test_refuses_existing_output(self):
        args = self.args()
        output = Path(args.output)
        output.write_text("preserve")
        with self.assertRaisesRegex(ArchiveError, "never overwritten"):
            build_archive(self.root, args)
        self.assertEqual(output.read_text(), "preserve")

    def test_refuses_symlinked_output_directory(self):
        actual = Path(self.temporary.name) / "actual"
        actual.mkdir()
        linked = Path(self.temporary.name) / "linked"
        linked.symlink_to(actual, target_is_directory=True)
        args = self.args()
        args.output = str(linked / "nested" / "archive.zip")
        with self.assertRaisesRegex(ArchiveError, "symlink"):
            build_archive(self.root, args)
        self.assertEqual(list(actual.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
