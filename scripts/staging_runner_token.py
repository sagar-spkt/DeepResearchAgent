"""Place a short-lived GitHub runner token in a session-scoped SSM SecureString."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

import boto3


def stage_token(repository: str, account_id: str, session_id: str, kms_key_arn: str, *, profile: str | None = None) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository must be exact owner/repository")
    if not re.fullmatch(r"[0-9]{12}", account_id):
        raise ValueError("account_id must be a twelve-digit AWS account")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,15}", session_id):
        raise ValueError("session_id has invalid characters")
    if not re.fullmatch(rf"arn:aws:kms:us-east-1:{account_id}:key/[0-9a-f-]{{36}}", kms_key_arn):
        raise ValueError("kms_key_arn must be a key in the selected AWS account and region")
    session = boto3.Session(profile_name=profile, region_name="us-east-1")
    sts = session.client("sts")
    if sts.get_caller_identity()["Account"] != account_id:
        raise RuntimeError("AWS identity account does not match the requested account")

    # gh emits only the response field to this process; never print it or include it in errors.
    token = subprocess.run(
        ["gh", "api", "--method", "POST", f"repos/{repository}/actions/runners/registration-token", "--jq", ".token"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not token or not re.fullmatch(r"[A-Za-z0-9_]+", token):
        raise RuntimeError("GitHub did not return a valid registration token")

    name = f"/deepresearch/runner/{session_id}/registration"
    ssm = session.client("ssm")
    try:
        ssm.put_parameter(
            Name=name,
            Description="One-time GitHub Actions runner token; consumed during instance bootstrap",
            Type="SecureString",
            KeyId=kms_key_arn,
            Value=token,
            Overwrite=False,
            Tier="Standard",
        )
    except Exception:
        # Preserve pre-existing values on Overwrite=False collisions. Registration tokens
        # expire at GitHub within an hour; operators can explicitly remove an orphaned value.
        raise RuntimeError("Could not stage the one-time runner token; inspect and clean the session parameter") from None
    finally:
        token = ""
    return name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--kms-key-arn", required=True)
    parser.add_argument("--profile")
    args = parser.parse_args()
    try:
        name = stage_token(args.repository, args.account_id, args.session_id, args.kms_key_arn, profile=args.profile)
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Unable to stage runner registration token: {error}", file=sys.stderr)
        return 1
    print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
