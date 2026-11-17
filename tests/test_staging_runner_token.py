from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from scripts.staging_runner_token import stage_token


class RunnerTokenTests(unittest.TestCase):
    @patch("scripts.staging_runner_token.subprocess.run")
    @patch("scripts.staging_runner_token.boto3.Session")
    def test_token_is_put_as_non_overwriting_secure_string(self, session_factory: Mock, run: Mock) -> None:
        aws = Mock()
        session_factory.return_value = aws
        aws.client.return_value.get_caller_identity.return_value = {"Account": "123456789012"}
        ssm = Mock()
        aws.client.side_effect = [aws.client.return_value, ssm]
        run.return_value.stdout = "onetimeToken123\n"

        key_arn = "arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789012"
        path = stage_token("owner/repo", "123456789012", "stg-1", key_arn)

        self.assertEqual(path, "/deepresearch/runner/stg-1/registration")
        run.assert_called_once()
        args = ssm.put_parameter.call_args.kwargs
        self.assertEqual(args["Type"], "SecureString")
        self.assertEqual(args["Overwrite"], False)
        self.assertEqual(args["Value"], "onetimeToken123")
        self.assertEqual(args["KeyId"], key_arn)
        self.assertNotIn("onetimeToken123", repr(run.call_args.args))

    @patch("scripts.staging_runner_token.boto3.Session")
    def test_rejects_wrong_aws_account_before_minting(self, session_factory: Mock) -> None:
        aws = Mock()
        session_factory.return_value = aws
        aws.client.return_value.get_caller_identity.return_value = {"Account": "999999999999"}
        with patch("scripts.staging_runner_token.subprocess.run") as run:
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                stage_token("owner/repo", "123456789012", "stg-1", "arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789012")
            run.assert_not_called()

    def test_rejects_invalid_scope(self) -> None:
        with self.assertRaisesRegex(ValueError, "exact owner/repository"):
            stage_token("owner/../other", "123456789012", "stg-1", "arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789012")


if __name__ == "__main__":
    unittest.main()
