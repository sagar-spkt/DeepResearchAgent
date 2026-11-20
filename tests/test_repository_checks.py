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

    def test_infrastructure_workflow_renders_admin_console_guards(self):
        workflow = ROOT / ".github/workflows/infrastructure-checks.yml"
        data = json.loads(workflow.read_text())
        default_step = next(
            step
            for step in data["jobs"]["helm"]["steps"]
            if step.get("name") == "Keep the Langfuse console disabled by default"
        )
        self.assertIn("helm template ci helm/research", default_step["run"])
        self.assertIn("if grep -F 'name: research-console'", default_step["run"])
        self.assertIn("exit 1", default_step["run"])
        step = next(
            step
            for step in data["jobs"]["helm"]["steps"]
            if step.get("name") == "Render the optional administrator-only Langfuse console"
        )
        command = step["run"]
        self.assertIn('--set-string console.image="$nginx_image"', command)
        self.assertIn("--set console.enabled=true", command)
        self.assertIn("research.example.test", command)
        self.assertIn("auth_request /_admin_access;", command)
        self.assertIn("proxy_pass http://research-web:3000/api/admin/access;", command)
        self.assertIn("map $http_cookie $cookie_without_leading_session", command)
        self.assertIn("~^research_session=[^;]*", command)
        self.assertIn("(?<session_tail>.*)$", command)
        self.assertIn("default $http_cookie;", command)
        self.assertIn("$session_tail;", command)
        self.assertIn("map $cookie_without_leading_session $native_cookie", command)
        self.assertIn("(?<session_head>.+?);[[:space:]]*research_session=", command)
        self.assertIn("(?<session_suffix>;.*|)$", command)
        self.assertIn("default $cookie_without_leading_session;", command)
        self.assertIn("$session_head$session_suffix", command)
        self.assertIn("fastcgi_temp_path /tmp/fastcgi;", command)
        self.assertIn("uwsgi_temp_path /tmp/uwsgi;", command)
        self.assertIn("scgi_temp_path /tmp/scgi;", command)
        self.assertIn("proxy_set_header Cookie $native_cookie;", command)
        self.assertIn("admin_location=$(awk", command)
        self.assertIn("langfuse_location=$(awk", command)
        self.assertIn("The native console must not receive the application session cookie.", command)
        self.assertIn("The admin authorization subrequest must not receive", command)
        self.assertIn("proxy_set_header Upgrade", command)
        self.assertIn("proxy_set_header Connection", command)
        self.assertIn("grep -Ei '^[[:space:]]*proxy_set_header (Upgrade|Connection)", command)
        self.assertIn("grep -Eiv '^[[:space:]]*proxy_set_header (Upgrade|Connection)", command)
        self.assertIn("must not forward WebSocket upgrade headers.", command)
        self.assertIn("research-console.yaml", command)
        self.assertIn("nginx_image=ghcr.io/nginx/nginx-unprivileged:1.30.7-alpine3.24@sha256:", command)
        self.assertIn(
            "sha256:0c53a1f2890c3526bfd201440f0fd3970e5b78f80630f58284aad0d34b5ec7bd",
            command,
        )
        self.assertIn("nginx_config=\"$RUNNER_TEMP/research-console.conf\"", command)
        self.assertIn("--network none --read-only --cap-drop ALL --security-opt no-new-privileges", command)
        self.assertIn("--entrypoint nginx ghcr.io/nginx/nginx-unprivileged:", command)
        self.assertIn(" -t\nconsole_container=", command)
        runtime_launch = next(
            line for line in command.splitlines() if "docker run --rm --detach" in line
        )
        self.assertIn("--network none --read-only --cap-drop ALL", runtime_launch)
        self.assertNotIn("--entrypoint", runtime_launch)
        self.assertIn('"$nginx_image"', runtime_launch)
        self.assertIn("docker exec \"$console_container\" wget -qO-", command)
        self.assertIn("http://127.0.0.1:8080/healthz", command)
        self.assertIn("docker stop \"$console_container\"", command)

    def test_application_ci_scans_the_pinned_console_proxy_image(self):
        application = json.loads(
            (ROOT / ".github/workflows/application-checks.yml").read_text()
        )
        infra = json.loads(
            (ROOT / ".github/workflows/infrastructure-checks.yml").read_text()
        )
        infra_command = next(
            step["run"]
            for step in infra["jobs"]["helm"]["steps"]
            if step.get("name") == "Render the optional administrator-only Langfuse console"
        )
        image_line = next(
            line for line in infra_command.splitlines() if line.startswith("nginx_image=")
        )
        nginx_image = image_line.split("=", 1)[1]
        steps = application["jobs"]["container-security"]["steps"]
        pull = next(
            step
            for step in steps
            if step.get("name") == "Pull pinned Langfuse console proxy image"
        )
        self.assertEqual(f"docker pull {nginx_image}", pull["run"])
        scan = next(step["run"] for step in steps if step.get("name") == "Scan and create SBOMs")
        self.assertIn(f"console_image={nginx_image}", scan)
        self.assertIn("langfuse-console-nginx-trivy.json", scan)
        self.assertIn("langfuse-console-nginx-sbom.cdx.json", scan)
        app_scan = next(line for line in scan.splitlines() if "${name}-trivy.json" in line)
        self.assertIn(
            "--scanners vuln,license --severity UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL",
            app_scan,
        )
        console_scan = next(
            line for line in scan.splitlines() if "langfuse-console-nginx-trivy.json" in line
        )
        self.assertIn(
            "--scanners vuln,license --severity UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL",
            console_scan,
        )
        self.assertIn("--scanners vuln --severity CRITICAL --exit-code 1 \"$console_image\"", scan)
        self.assertIn(
            "--scanners vuln --ignore-unfixed --severity HIGH --exit-code 1 \"$console_image\"",
            scan,
        )

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
