#!/usr/bin/env python3
"""Create a one-hour repo runner token and stage it as a KMS-encrypted SSM value."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ACCOUNT_ID = "088759500358"
REPOSITORY = "sagar-spkt/DeepResearchAgent"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--kms-key-arn", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,15}", args.session_id):
        parser.error("session ID must be at most 16 lowercase letters, digits, or hyphens")
    if not re.fullmatch(
        rf"arn:aws:kms:us-east-1:{ACCOUNT_ID}:key/[0-9a-f-]{{36}}", args.kms_key_arn
    ):
        parser.error("KMS key ARN must identify a key in the staging account and region")

    try:
        identity = _run(
            ["aws", "sts", "get-caller-identity", "--region", "us-east-1", "--query", "Account", "--output", "text"]
        )
    except RuntimeError:
        print("Could not verify the active AWS account; token was not requested.", file=sys.stderr)
        return 2
    if identity.strip() != ACCOUNT_ID:
        print("AWS identity is not the staging account; token was not requested.", file=sys.stderr)
        return 2

    try:
        response = _run(
            ["gh", "api", "--method", "POST", f"repos/{REPOSITORY}/actions/runners/registration-token"]
        )
    except RuntimeError:
        print("Could not request a repository runner token; no AWS parameter was written.", file=sys.stderr)
        return 2
    try:
        registration = json.loads(response)
        token = registration["token"]
        expires_at = datetime.fromisoformat(registration["expires_at"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        print("GitHub did not return a valid runner registration token.", file=sys.stderr)
        return 2
    if not isinstance(token, str) or not token or expires_at <= datetime.now(timezone.utc) + timedelta(minutes=20):
        print("GitHub returned an empty or soon-to-expire registration token.", file=sys.stderr)
        return 2

    parameter_name = f"/deepresearch/runner/{args.session_id}/registration"
    request = {
        "Name": parameter_name,
        "Description": f"One-time GitHub runner registration token for {args.session_id}",
        "Type": "SecureString",
        "KeyId": args.kms_key_arn,
        "Value": token,
        "Overwrite": False,
        "Tier": "Standard",
    }
    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as tmp:
            path = Path(tmp.name)
            os.chmod(path, 0o600)
            json.dump(request, tmp)
        _run(["aws", "ssm", "put-parameter", "--region", "us-east-1", "--cli-input-json", f"file://{path}"])
    except RuntimeError:
        print(
            "Unable to stage the one-time token in encrypted SSM. It expires within one hour; retry the runner preparation.",
            file=sys.stderr,
        )
        return 2
    finally:
        if path is not None:
            path.unlink(missing_ok=True)

    print(f"Staged one-time runner token at {parameter_name}; GitHub token expires at {expires_at.isoformat()}.")
    return 0


def _run(command: list[str]) -> str:
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("external command failed") from exc
    return result.stdout


if __name__ == "__main__":
    raise SystemExit(main())
