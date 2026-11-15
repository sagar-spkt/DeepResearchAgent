import importlib.util
import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "release_manifest", ROOT / "scripts/validate_release_manifest.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PlatformToolsTest(unittest.TestCase):
    def test_mutable_or_injected_images_rejected(self):
        for value in ["image:latest", "image@sha256:" + "a" * 64 + "; echo unsafe"]:
            with self.assertRaises(ValueError):
                module.validate(
                    {
                        "backend_image": value,
                        "web_image": value,
                        "source_commit": "a" * 40,
                        "staging_validated": True,
                    }
                )

    def test_valid_manifest(self):
        data = {
            "backend_image": "example/backend@sha256:" + "a" * 64,
            "web_image": "example/web@sha256:" + "b" * 64,
            "source_commit": "c" * 40,
            "staging_validated": True,
        }
        self.assertEqual(data, module.validate(data))

    def test_lifecycle_execute_fails_closed(self):
        for operation in ["deploy", "promote", "backup-shutdown", "restore", "purge"]:
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/platform_lifecycle.py"),
                    operation,
                    "--environment",
                    "staging",
                    "--execute",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(2, result.returncode)
            self.assertIn('"executed": false', result.stdout)

    def test_promotion_refuses_before_aws_adapter_creation(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        spec = importlib.util.spec_from_file_location(
            "platform_lifecycle", ROOT / "scripts/platform_lifecycle.py"
        )
        lifecycle = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lifecycle)
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["platform_lifecycle.py", "promote", "--environment", "production"]),
            patch.object(lifecycle, "adapter", side_effect=AssertionError("AWS adapter was constructed")),
            redirect_stdout(output),
        ):
            self.assertEqual(2, lifecycle.main())
        result = json.loads(output.getvalue())
        self.assertFalse(result["executed"])
        self.assertEqual("Refused", result["error_type"])
        self.assertIn("promotion", result["detail"].lower())

    def test_aws_lifecycle_workflows_remain_read_only_and_fail_closed(self):
        expected_operations = {
            "deploy.yml": "deploy",
            "promote.yml": "promote",
            "restore.yml": "restore",
            "purge.yml": "purge",
            "backup-shutdown.yml": "backup-shutdown",
        }
        for filename, operation in expected_operations.items():
            with self.subTest(workflow=filename):
                workflow = json.loads(
                    (ROOT / ".github/workflows" / filename).read_text()
                )
                self.assertEqual(
                    {"name", "on", "permissions", "concurrency", "jobs"},
                    set(workflow),
                )
                self.assertEqual({"contents": "read"}, workflow["permissions"])
                self.assertEqual(
                    {
                        "workflow_dispatch": {
                            "inputs": {
                                "environment": {
                                    "type": "choice",
                                    "options": ["staging", "production"],
                                    "required": True,
                                    "default": "staging",
                                }
                            }
                        }
                    },
                    workflow["on"],
                )
                self.assertEqual(
                    {
                        "group": "deepresearch-aws-lifecycle",
                        "cancel-in-progress": False,
                    },
                    workflow["concurrency"],
                )
                self.assertEqual({"preflight"}, set(workflow["jobs"]))

                serialized = json.dumps(workflow)
                self.assertNotIn("id-token", serialized)
                self.assertNotIn("aws-actions/configure-aws-credentials", serialized)
                self.assertNotIn("--execute", serialized)
                self.assertNotIn("terraform apply", serialized)

                self.assertEqual(
                    {
                        "runs-on": "ubuntu-24.04",
                        "environment": "${{ inputs.environment }}",
                        "timeout-minutes": 5,
                        "steps": [
                            {
                                "uses": "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
                                "with": {"persist-credentials": False},
                            },
                            {
                                "name": "Inspect lifecycle readiness (fails closed until implementation completes)",
                                "env": {
                                    "TARGET_ENVIRONMENT": "${{ inputs.environment }}"
                                },
                                "run": (
                                    f'python3 scripts/platform_lifecycle.py {operation} '
                                    '--environment "$TARGET_ENVIRONMENT"'
                                ),
                            },
                        ],
                    },
                    workflow["jobs"]["preflight"],
                )


if __name__ == "__main__":
    unittest.main()
