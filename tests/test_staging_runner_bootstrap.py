from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
USER_DATA = ROOT / "infra/management/runner-user-data.sh.tftpl"


class RunnerBootstrapTests(unittest.TestCase):
    def test_host_tools_and_docker_readiness_precede_one_time_token_read(self) -> None:
        source = USER_DATA.read_text()
        token_read = source.index("ssm get-parameter")
        for required in (
            "for command in docker git python3",
            "docker buildx version",
            "docker info",
        ):
            with self.subTest(required=required):
                self.assertLess(source.index(required), token_read)
        self.assertIn("if [[ \"$docker_ready\" != true ]]; then exit 1; fi", source)

    def test_su_wrapper_command_does_not_interpolate_registration_token(self) -> None:
        source = USER_DATA.read_text()
        wrapper_command = next(
            line for line in source.splitlines() if line.startswith("if ! su - runner -c")
        )
        self.assertEqual(
            wrapper_command,
            "if ! su - runner -c /opt/actions-runner/configure-once.sh >/run/actions-runner/configure.log 2>&1; then",
        )
        self.assertNotIn("$token", wrapper_command)
        self.assertNotIn("--token", wrapper_command)

    def test_token_file_is_protected_and_removed_before_runner_service(self) -> None:
        source = USER_DATA.read_text()
        self.assertIn("chmod 600 \"$token_file\"", source)
        self.assertIn('chown runner:"$runner_group" "$token_file"', source)
        self.assertIn("su - runner -c /opt/actions-runner/configure-once.sh", source)
        self.assertIn(
            "rm -f /run/actions-runner/token /run/actions-runner/configure.log /opt/actions-runner/configure-once.sh",
            source,
        )
        success_cleanup = "rm -f /opt/actions-runner/configure-once.sh /run/actions-runner/configure.log /run/actions-runner/token"
        self.assertIn(success_cleanup, source)
        self.assertLess(
            source.index("if ! su - runner -c /opt/actions-runner/configure-once.sh"),
            source.index(success_cleanup),
        )
        self.assertLess(
            source.index(success_cleanup),
            source.index("systemctl enable --now actions-runner.service"),
        )
        self.assertIn("--token \"$token\"", source)
        self.assertIn("rm -f /run/actions-runner/token", source)


if __name__ == "__main__":
    unittest.main()
