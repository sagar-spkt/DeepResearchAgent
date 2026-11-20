"""Behavior tests use isolated repositories and never require cloud credentials."""

import json
import shlex
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_repository import check
from sync_agent_adapters import ROOT, sync


class RepositoryChecksTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(ROOT / "agent-config/skills", self.root / "agent-config/skills")
        shutil.copytree(ROOT / "agent-config/roles", self.root / "agent-config/roles")
        sync(self.root)

    def test_clean_generation_is_idempotent(self):
        before = sync(self.root, check=True)
        self.assertEqual(before, sync(self.root))
        errors, digest = check(self.root)
        self.assertEqual([], errors)
        self.assertEqual(64, len(digest))

    def test_changed_canonical_skill_requires_regeneration(self):
        skill = self.root / "agent-config/skills/review-change/SKILL.md"
        skill.write_text(skill.read_text() + "\nAdditional review requirement.\n")
        errors, _ = check(self.root)
        self.assertTrue(any("Adapter drift" in e for e in errors), errors)
        sync(self.root)
        self.assertEqual([], check(self.root)[0])

    def test_detects_invalid_metadata_and_missing_references(self):
        (self.root / "README.md").write_text("[Missing](missing.md)\n")
        errors, _ = check(self.root)
        self.assertTrue(any("broken local link" in e for e in errors), errors)
        skill = self.root / "agent-config/skills/review-change/SKILL.md"
        skill.write_text('---\nname: "review-change"\n---\nBody\n')
        self.assertTrue(any("missing description" in e for e in check(self.root)[0]))

    def test_detects_secret_signature_and_unpinned_action(self):
        (self.root / "bad.md").write_text("AWS: " + "AKIA" + "A" * 16 + "\n")
        self.assertTrue(any("access-key signature" in e for e in check(self.root)[0]))
        workflow = self.root / ".github/workflows/check.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text(
            '{"permissions":{"contents":"read"},"on":{"pull_request":{}},"jobs":{"check":{"steps":[{"uses":"actions/checkout@main"}]}}}\n'
        )
        self.assertTrue(any("not SHA-pinned" in e for e in check(self.root)[0]))

    def test_detects_invalid_workflow_shell_without_executing_it(self):
        workflow = self.root / ".github/workflows/invalid-shell.yml"
        workflow.parent.mkdir(parents=True, exist_ok=True)
        marker = self.root / "workflow-command-was-executed"
        workflow.write_text(
            json.dumps(
                {
                    "permissions": {"contents": "read"},
                    "on": {"pull_request": {}},
                    "jobs": {
                        "check": {
                            "steps": [
                                {"run": "if then\nprintf 'invalid syntax\\n'\nfi"},
                                {"run": f"touch {shlex.quote(str(marker))}"},
                            ]
                        }
                    },
                }
            )
            + "\n"
        )

        errors, _ = check(self.root)

        self.assertTrue(any("invalid bash syntax in run block" in error for error in errors))
        self.assertFalse(marker.exists())

    def test_infrastructure_workflow_is_read_only_validation(self):
        workflow = ROOT / ".github/workflows/infrastructure-checks.yml"
        data = json.loads(workflow.read_text())
        self.assertEqual({"contents": "read"}, data["permissions"])
        self.assertEqual(
            {"push", "pull_request", "workflow_dispatch"}, set(data["on"])
        )
        commands = "\n".join(
            step.get("run", "")
            for job in data["jobs"].values()
            for step in job["steps"]
        )
        serialized = json.dumps(data)
        self.assertIn("-backend=false", commands)
        self.assertIn("-lockfile=readonly", commands)
        self.assertIn("terraform fmt -check", commands)
        self.assertIn("helm template", commands)
        self.assertIn("shasum -a 256 -c", commands)
        self.assertIn(
            "5b9f5bf3d7295d0c61eda2a7aea8c7f3369d51eca406c3dc032b84eacc3893d3",
            commands,
        )
        self.assertNotIn("terraform plan", commands)
        self.assertNotIn("terraform apply", commands)
        self.assertNotIn("id-token", serialized)
        self.assertNotIn("aws-actions/configure-aws-credentials", serialized)

    def test_required_ci_workflows_run_on_direct_branch_pushes(self):
        for filename in [
            "application-checks.yml",
            "infrastructure-checks.yml",
            "repository-checks.yml",
        ]:
            with self.subTest(workflow=filename):
                data = json.loads((ROOT / ".github/workflows" / filename).read_text())
                self.assertEqual(
                    {"push", "pull_request", "workflow_dispatch"}, set(data["on"])
                )
                self.assertEqual(["**"], data["on"]["push"]["branches"])

    def test_application_backend_ci_installs_bedrock_test_extra(self):
        workflow = ROOT / ".github/workflows/application-checks.yml"
        data = json.loads(workflow.read_text())
        commands = [
            step.get("run", "").splitlines()
            for step in data["jobs"]["backend"]["steps"]
        ]
        sync_command = next(
            line
            for block in commands
            for line in block
            if ".uv-tool/bin/uv sync " in line
        )
        sync_tokens = shlex.split(sync_command)
        project = (ROOT / "pyproject.toml").read_text()
        bedrock_test = (ROOT / "backend/tests/test_eval_budget.py").read_text()
        self.assertIn("--locked", sync_tokens)
        self.assertTrue(
            any(
                sync_tokens[index : index + 2] == ["--extra", "bedrock"]
                for index in range(len(sync_tokens) - 1)
            ),
            sync_tokens,
        )
        self.assertIn('bedrock = ["langchain-aws==1.7.3"]', project)
        self.assertIn("import langchain_aws", bedrock_test)

    def test_install_preserves_unmanaged_files(self):
        target = self.root / ".claude/agents/implementer.md"
        target.parent.mkdir(parents=True)
        target.write_text("User configuration\n")
        with self.assertRaisesRegex(ValueError, "unmanaged"):
            sync(self.root, install="claude")
        self.assertEqual("User configuration\n", target.read_text())

    def test_native_installation_in_disposable_workspace(self):
        for tool in ("codex", "claude", "antigravity"):
            sync(self.root, install=tool)
        self.assertTrue((self.root / ".codex/agents/reviewer.toml").is_file())
        self.assertTrue((self.root / ".claude/skills/review-change/SKILL.md").is_file())
        self.assertTrue((self.root / ".agents/agents/test-validator.md").is_file())

    def test_installed_adapter_drift_fails_check(self):
        sync(self.root, install="claude")
        target = self.root / ".claude/agents/reviewer.md"
        target.write_text(target.read_text() + "\nStale local change.\n")
        with self.assertRaisesRegex(ValueError, "Adapter drift"):
            sync(self.root, check=True)
        sync(self.root, install="claude")
        sync(self.root, check=True)

    def test_missing_canonical_skill_fails(self):
        (self.root / "agent-config/skills/plan-change/SKILL.md").unlink()
        with self.assertRaisesRegex(ValueError, "skill set"):
            sync(self.root, check=True)

    def test_install_rejects_symlink_outside_repository(self):
        external = tempfile.TemporaryDirectory()
        self.addCleanup(external.cleanup)
        (self.root / ".claude").symlink_to(external.name, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "outside repository"):
            sync(self.root, install="claude")
        self.assertEqual([], list(Path(external.name).iterdir()))

    def test_export_rejects_symlink_outside_repository(self):
        external = tempfile.TemporaryDirectory()
        self.addCleanup(external.cleanup)
        export = self.root / "agent-config/adapters"
        shutil.rmtree(export)
        export.symlink_to(external.name, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "outside repository"):
            sync(self.root)
        self.assertEqual([], list(Path(external.name).iterdir()))


if __name__ == "__main__":
    unittest.main()
