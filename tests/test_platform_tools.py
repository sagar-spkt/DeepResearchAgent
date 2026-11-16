import importlib.util
import io
import json
import os
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

    def test_production_promotion_requires_exact_manual_confirmation(self):
        workflow = json.loads((ROOT / ".github/workflows/promote.yml").read_text())
        step = next(
            step
            for step in workflow["jobs"]["preflight"]["steps"]
            if step.get("name") == "Require deliberate production authorization"
        )
        for environment, confirmation, expected in [
            ("production", "", 1),
            ("production", "approve", 1),
            ("production", "I AUTHORIZE PRODUCTION PROMOTION", 0),
            ("staging", "", 1),
        ]:
            with self.subTest(environment=environment, confirmation=confirmation):
                result = subprocess.run(
                    ["bash", "-c", step["run"]],
                    env={
                        **os.environ,
                        "TARGET_ENVIRONMENT": environment,
                        "PRODUCTION_CONFIRMATION": confirmation,
                        "GITHUB_ACTOR": "test-operator",
                        "GITHUB_SHA": "a" * 40,
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(expected, result.returncode)

    def test_other_lifecycle_workflows_require_environment_specific_confirmation(self):
        cases = {
            "backup-shutdown.yml": (
                "Require explicit backup and shutdown authorization",
                "I AUTHORIZE STAGING BACKUP AND SHUTDOWN",
                "I AUTHORIZE PRODUCTION BACKUP AND SHUTDOWN",
            ),
            "restore.yml": (
                "Require explicit restore authorization",
                "I AUTHORIZE STAGING RESTORE",
                "I AUTHORIZE PRODUCTION RESTORE",
            ),
            "purge.yml": (
                "Require separate explicit purge authorization",
                "I AUTHORIZE STAGING PURGE",
                "I AUTHORIZE PRODUCTION PURGE",
            ),
        }
        for filename, (step_name, staging_phrase, production_phrase) in cases.items():
            workflow = json.loads((ROOT / ".github/workflows" / filename).read_text())
            step = next(
                step
                for step in workflow["jobs"]["preflight"]["steps"]
                if step.get("name") == step_name
            )
            for environment, phrase, expected in [
                ("staging", "", 1),
                ("staging", "I AUTHORIZE PRODUCTION PURGE", 1),
                ("staging", staging_phrase, 0),
                ("production", staging_phrase, 1),
                ("production", production_phrase, 0),
            ]:
                with self.subTest(workflow=filename, environment=environment, phrase=phrase):
                    result = subprocess.run(
                        ["bash", "-c", step["run"]],
                        env={
                            **os.environ,
                            "TARGET_ENVIRONMENT": environment,
                            "CONFIRMATION": phrase,
                            "GITHUB_ACTOR": "test-operator",
                            "GITHUB_SHA": "a" * 40,
                        },
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(expected, result.returncode)

    def test_staging_deploy_requires_exact_manual_confirmation(self):
        workflow = json.loads((ROOT / ".github/workflows/deploy.yml").read_text())
        step = next(
            step
            for step in workflow["jobs"]["preflight"]["steps"]
            if step.get("name") == "Require deliberate staging authorization"
        )
        for environment, confirmation, expected in [
            ("staging", "", 1),
            ("staging", "launch", 1),
            ("staging", "I AUTHORIZE STAGING DEPLOYMENT", 0),
            ("production", "I AUTHORIZE STAGING DEPLOYMENT", 1),
        ]:
            with self.subTest(environment=environment, confirmation=confirmation):
                result = subprocess.run(
                    ["bash", "-c", step["run"]],
                    env={
                        **os.environ,
                        "TARGET_ENVIRONMENT": environment,
                        "STAGING_CONFIRMATION": confirmation,
                        "GITHUB_ACTOR": "test-operator",
                        "GITHUB_SHA": "a" * 40,
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(expected, result.returncode)

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
                environment_options = (
                    ["staging"]
                    if filename == "deploy.yml"
                    else ["production"]
                    if filename == "promote.yml"
                    else ["staging", "production"]
                )
                expected_inputs = {
                    "environment": {
                        "type": "choice",
                        "options": environment_options,
                        "required": True,
                        "default": "production" if filename == "promote.yml" else "staging",
                    }
                }
                if filename == "promote.yml":
                    expected_inputs["production_confirmation"] = {
                        "description": "For production only, enter exactly: I AUTHORIZE PRODUCTION PROMOTION",
                        "type": "string",
                        "required": False,
                    }
                if filename == "deploy.yml":
                    expected_inputs["staging_confirmation"] = {
                        "description": "Enter exactly: I AUTHORIZE STAGING DEPLOYMENT",
                        "type": "string",
                        "required": False,
                    }
                if filename in {"restore.yml", "purge.yml", "backup-shutdown.yml"}:
                    phrases = {
                        "restore.yml": (
                            "I AUTHORIZE STAGING RESTORE",
                            "I AUTHORIZE PRODUCTION RESTORE",
                        ),
                        "purge.yml": (
                            "I AUTHORIZE STAGING PURGE",
                            "I AUTHORIZE PRODUCTION PURGE",
                        ),
                        "backup-shutdown.yml": (
                            "I AUTHORIZE STAGING BACKUP AND SHUTDOWN",
                            "I AUTHORIZE PRODUCTION BACKUP AND SHUTDOWN",
                        ),
                    }[filename]
                    expected_inputs["confirmation"] = {
                        "description": f"For staging: {phrases[0]}. For production: {phrases[1]}.",
                        "type": "string",
                        "required": False,
                    }
                self.assertEqual(
                    {"workflow_dispatch": {"inputs": expected_inputs}}, workflow["on"]
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

                expected_steps = [
                    {
                        "uses": "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
                        "with": {"persist-credentials": False},
                    }
                ]
                if filename == "promote.yml":
                    expected_steps.append(
                        {
                            "name": "Require deliberate production authorization",
                            "env": {
                                "TARGET_ENVIRONMENT": "${{ inputs.environment }}",
                                "PRODUCTION_CONFIRMATION": "${{ inputs.production_confirmation }}",
                            },
                            "run": "if [[ \"$TARGET_ENVIRONMENT\" != \"production\" || \"$PRODUCTION_CONFIRMATION\" != \"I AUTHORIZE PRODUCTION PROMOTION\" ]]; then echo 'Production promotion requires the exact confirmation phrase.' >&2; exit 1; fi; echo \"Production self-authorization confirmed by $GITHUB_ACTOR for $GITHUB_SHA\"",
                        }
                    )
                if filename == "deploy.yml":
                    expected_steps.append(
                        {
                            "name": "Require deliberate staging authorization",
                            "env": {
                                "TARGET_ENVIRONMENT": "${{ inputs.environment }}",
                                "STAGING_CONFIRMATION": "${{ inputs.staging_confirmation }}",
                            },
                            "run": "if [[ \"$TARGET_ENVIRONMENT\" != \"staging\" || \"$STAGING_CONFIRMATION\" != \"I AUTHORIZE STAGING DEPLOYMENT\" ]]; then echo 'Staging deployment requires the exact confirmation phrase.' >&2; exit 1; fi; echo \"Staging self-authorization confirmed by $GITHUB_ACTOR for $GITHUB_SHA\"",
                        }
                    )
                other_operation_gates = {
                    "backup-shutdown.yml": (
                        "Require explicit backup and shutdown authorization",
                        "case \"$TARGET_ENVIRONMENT:$CONFIRMATION\" in staging:'I AUTHORIZE STAGING BACKUP AND SHUTDOWN'|production:'I AUTHORIZE PRODUCTION BACKUP AND SHUTDOWN') ;; *) echo 'Backup and shutdown require the exact environment-specific confirmation phrase.' >&2; exit 1 ;; esac; echo \"Backup/shutdown self-authorization confirmed by $GITHUB_ACTOR for $GITHUB_SHA\"",
                    ),
                    "restore.yml": (
                        "Require explicit restore authorization",
                        "case \"$TARGET_ENVIRONMENT:$CONFIRMATION\" in staging:'I AUTHORIZE STAGING RESTORE'|production:'I AUTHORIZE PRODUCTION RESTORE') ;; *) echo 'Restore requires the exact environment-specific confirmation phrase.' >&2; exit 1 ;; esac; echo \"Restore self-authorization confirmed by $GITHUB_ACTOR for $GITHUB_SHA\"",
                    ),
                    "purge.yml": (
                        "Require separate explicit purge authorization",
                        "case \"$TARGET_ENVIRONMENT:$CONFIRMATION\" in staging:'I AUTHORIZE STAGING PURGE'|production:'I AUTHORIZE PRODUCTION PURGE') ;; *) echo 'Purge requires the exact environment-specific confirmation phrase.' >&2; exit 1 ;; esac; echo \"Purge self-authorization confirmed by $GITHUB_ACTOR for $GITHUB_SHA\"",
                    ),
                }
                if filename in other_operation_gates:
                    name, run = other_operation_gates[filename]
                    expected_steps.append(
                        {
                            "name": name,
                            "env": {
                                "TARGET_ENVIRONMENT": "${{ inputs.environment }}",
                                "CONFIRMATION": "${{ inputs.confirmation }}",
                            },
                            "run": run,
                        }
                    )
                expected_steps.append(
                    {
                        "name": "Inspect lifecycle readiness (fails closed until implementation completes)",
                        "env": {"TARGET_ENVIRONMENT": "${{ inputs.environment }}"},
                        "run": (
                            f'python3 scripts/platform_lifecycle.py {operation} '
                            '--environment "$TARGET_ENVIRONMENT"'
                        ),
                    }
                )
                self.assertEqual(
                    {
                        "runs-on": "ubuntu-24.04",
                        "environment": "${{ inputs.environment }}",
                        "timeout-minutes": 5,
                        "steps": expected_steps,
                    },
                    workflow["jobs"]["preflight"],
                )


if __name__ == "__main__":
    unittest.main()
