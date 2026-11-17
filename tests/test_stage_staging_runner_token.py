from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import stage_staging_runner_token


class StageStagingRunnerTokenTests(unittest.TestCase):
    def test_wrong_account_stops_before_requesting_runner_token(self) -> None:
        with patch.object(stage_staging_runner_token, "_run", return_value="999999999999") as run:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                with patch("sys.argv", ["stage", "--session-id", "stg-1", "--kms-key-arn", _kms_arn()]):
                    self.assertEqual(stage_staging_runner_token.main(), 2)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(
            stderr.getvalue(),
            "AWS identity is not the staging account; token was not requested.\n",
        )

    def test_token_is_only_written_to_private_temporary_ssm_request(self) -> None:
        token = "short-lived-registration-token"
        captured: dict[str, object] = {}
        expires = "2099-01-01T00:00:00Z"

        def fake_run(command: list[str]) -> str:
            if command[:3] == ["aws", "sts", "get-caller-identity"]:
                return stage_staging_runner_token.ACCOUNT_ID
            if command[0] == "gh":
                return json.dumps({"token": token, "expires_at": expires})
            request_path = Path(command[-1].removeprefix("file://"))
            captured["path"] = request_path
            captured["mode"] = request_path.stat().st_mode & 0o777
            captured["request"] = json.loads(request_path.read_text())
            return "{\"Version\":1}"

        with patch.object(stage_staging_runner_token, "_run", side_effect=fake_run):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                with patch(
                    "sys.argv",
                    ["stage", "--session-id", "stg-test", "--kms-key-arn", _kms_arn()],
                ):
                    self.assertEqual(stage_staging_runner_token.main(), 0)

        request = captured["request"]
        self.assertEqual(captured["mode"], 0o600)
        self.assertEqual(request["Name"], "/deepresearch/runner/stg-test/registration")
        self.assertEqual(request["Type"], "SecureString")
        self.assertEqual(request["KeyId"], _kms_arn())
        self.assertEqual(request["Value"], token)
        self.assertFalse(request["Overwrite"])
        self.assertFalse(Path(captured["path"]).exists())
        self.assertNotIn(token, stdout.getvalue())


def _kms_arn() -> str:
    return "arn:aws:kms:us-east-1:088759500358:key/12345678-1234-1234-1234-123456789012"


if __name__ == "__main__":
    unittest.main()
