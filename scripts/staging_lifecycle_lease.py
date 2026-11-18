"""Manage the staging GitHub workflow's lease in the shared lifecycle table."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

TABLE = "deepresearch-control"
REGION = "us-east-1"
ENVIRONMENT = "staging"
ACCOUNT_ID = "088759500358"
LEASE_SECONDS = 120
RUNTIME_SECONDS = 4 * 60 * 60
RESERVATION_SECONDS = 6 * 60 * 60


class LeaseError(RuntimeError):
    pass


def validate_session_id(session_id):
    if not isinstance(session_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,15}", session_id):
        raise LeaseError("The staging session ID is invalid")
    return session_id


def request(args, runner=subprocess.run):
    result = runner(
        ["aws", "dynamodb", *args, "--region", REGION, "--output", "json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise LeaseError("DynamoDB lifecycle lease operation failed")
    if not result.stdout.strip() and any(
        operation in args for operation in ("get-item", "update-item", "put-item", "transact-write-items")
    ):
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise LeaseError("DynamoDB returned an invalid lifecycle response") from exc


def item(key, runner=subprocess.run):
    result = request(
        [
            "get-item",
            "--table-name",
            TABLE,
            "--consistent-read",
            "--key",
            json.dumps({"key": {"S": key}}),
        ],
        runner,
    )
    return result.get("Item", {})


def deployment_marker_key(session_id):
    return "app-writers#staging#" + validate_session_id(session_id)


def assert_admission(session_id, runner=subprocess.run):
    session_id = validate_session_id(session_id)
    current = item("active-session", runner)
    expected = {
        "session_id": session_id,
        "environment": ENVIRONMENT,
        "account_id": ACCOUNT_ID,
    }
    if any(current.get(key, {}).get("S") != value for key, value in expected.items()):
        raise LeaseError("The expected staging session is not the active admission")
    try:
        return int(current["started_at"]["N"])
    except (KeyError, TypeError, ValueError) as exc:
        raise LeaseError("The staging admission start time is invalid") from exc


def acquire(owner, session_id, now=None, runner=subprocess.run):
    session_id = validate_session_id(session_id)
    start = assert_admission(session_id, runner)
    instant = int(time.time() if now is None else now)
    deadline = start + RUNTIME_SECONDS
    if instant >= deadline:
        raise LeaseError("The staging runtime deadline has passed")
    request(
        [
            "update-item",
            "--table-name",
            TABLE,
            "--key",
            json.dumps({"key": {"S": "lifecycle-lock"}}),
            "--update-expression",
            "SET owner_id = :owner, expires_at = :expires, session_id = :session",
            "--condition-expression",
            "attribute_not_exists(owner_id) OR expires_at < :now",
            "--expression-attribute-values",
            json.dumps(
                {
                    ":owner": {"S": owner},
                    ":expires": {"N": str(min(instant + LEASE_SECONDS, deadline))},
                    ":session": {"S": session_id},
                    ":now": {"N": str(instant)},
                }
            ),
        ],
        runner,
    )


def assert_owner(owner, session_id, now=None, runner=subprocess.run):
    session_id = validate_session_id(session_id)
    start = assert_admission(session_id, runner)
    current = item("lifecycle-lock", runner)
    instant = int(time.time() if now is None else now)
    deadline = start + RUNTIME_SECONDS
    expires = int(current.get("expires_at", {}).get("N", "0"))
    if (
        instant >= deadline
        or expires > deadline
        or current.get("owner_id", {}).get("S") != owner
        or current.get("session_id", {}).get("S") != session_id
        or expires <= instant
    ):
        raise LeaseError("The staging lifecycle lease expired or changed owner")


def renew(owner, session_id, now=None, runner=subprocess.run):
    session_id = validate_session_id(session_id)
    start = assert_admission(session_id, runner)
    instant = int(time.time() if now is None else now)
    deadline = start + RUNTIME_SECONDS
    if instant >= deadline:
        raise LeaseError("The staging runtime deadline has passed")
    request(
        [
            "update-item",
            "--table-name",
            TABLE,
            "--key",
            json.dumps({"key": {"S": "lifecycle-lock"}}),
            "--update-expression",
            "SET expires_at = :expires",
            "--condition-expression",
            "owner_id = :owner AND session_id = :session AND expires_at > :now AND expires_at <= :deadline",
            "--expression-attribute-values",
            json.dumps(
                {
                    ":owner": {"S": owner},
                    ":session": {"S": session_id},
                    ":expires": {"N": str(min(instant + LEASE_SECONDS, deadline))},
                    ":now": {"N": str(instant)},
                    ":deadline": {"N": str(deadline)},
                }
            ),
        ],
        runner,
    )


def assert_finalizer_window(start, instant):
    runtime_deadline = start + RUNTIME_SECONDS
    reservation_deadline = start + RESERVATION_SECONDS
    if instant < runtime_deadline:
        raise LeaseError("The staging finalizer may start only after the runtime deadline")
    if instant >= reservation_deadline:
        # An abandoned admission still owns billable management resources.
        # Only the empty-session destroy workflow uses this short lease.
        return instant + LEASE_SECONDS
    return reservation_deadline


def assert_no_deployment_marker(session_id, runner=subprocess.run):
    if item(deployment_marker_key(session_id), runner):
        raise LeaseError("A deployment marker exists; use the backup and shutdown path")


def acquire_finalizer(owner, session_id, now=None, runner=subprocess.run):
    """Acquire the session lease for post-deadline empty-session finalization only."""
    session_id = validate_session_id(session_id)
    start = assert_admission(session_id, runner)
    instant = int(time.time() if now is None else now)
    deadline = assert_finalizer_window(start, instant)
    assert_no_deployment_marker(session_id, runner)
    request(
        [
            "update-item",
            "--table-name",
            TABLE,
            "--key",
            json.dumps({"key": {"S": "lifecycle-lock"}}),
            "--update-expression",
            "SET owner_id = :owner, expires_at = :expires, session_id = :session",
            "--condition-expression",
            "attribute_not_exists(owner_id) OR expires_at < :now",
            "--expression-attribute-values",
            json.dumps(
                {
                    ":owner": {"S": owner},
                    ":expires": {"N": str(min(instant + LEASE_SECONDS, deadline))},
                    ":session": {"S": session_id},
                    ":now": {"N": str(instant)},
                }
            ),
        ],
        runner,
    )


def assert_finalizer_owner(owner, session_id, now=None, runner=subprocess.run):
    session_id = validate_session_id(session_id)
    start = assert_admission(session_id, runner)
    instant = int(time.time() if now is None else now)
    deadline = assert_finalizer_window(start, instant)
    assert_no_deployment_marker(session_id, runner)
    current = item("lifecycle-lock", runner)
    expires = int(current.get("expires_at", {}).get("N", "0"))
    if (
        current.get("owner_id", {}).get("S") != owner
        or current.get("session_id", {}).get("S") != session_id
        or expires <= instant
        or expires > deadline
    ):
        raise LeaseError("The staging finalizer lease expired or changed owner")


def renew_finalizer(owner, session_id, now=None, runner=subprocess.run):
    session_id = validate_session_id(session_id)
    start = assert_admission(session_id, runner)
    instant = int(time.time() if now is None else now)
    deadline = assert_finalizer_window(start, instant)
    assert_finalizer_owner(owner, session_id, now=instant, runner=runner)
    request(
        [
            "update-item",
            "--table-name",
            TABLE,
            "--key",
            json.dumps({"key": {"S": "lifecycle-lock"}}),
            "--update-expression",
            "SET expires_at = :expires",
            "--condition-expression",
            "owner_id = :owner AND session_id = :session AND expires_at > :now AND expires_at <= :deadline",
            "--expression-attribute-values",
            json.dumps(
                {
                    ":owner": {"S": owner},
                    ":expires": {"N": str(min(instant + LEASE_SECONDS, deadline))},
                    ":now": {"N": str(instant)},
                    ":session": {"S": session_id},
                    ":deadline": {"N": str(deadline)},
                }
            ),
        ],
        runner,
    )


def mark_deployment_started(owner, session_id, commit_sha, workflow_run_id, now=None, runner=subprocess.run):
    """Permanently record that this admitted session may have started app writers."""
    session_id = validate_session_id(session_id)
    if not isinstance(commit_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
        raise LeaseError("The deployment commit SHA is invalid")
    if not isinstance(workflow_run_id, str) or not re.fullmatch(r"[0-9]+", workflow_run_id):
        raise LeaseError("The deployment workflow run ID is invalid")
    assert_owner(owner, session_id, now=now, runner=runner)
    instant = int(time.time() if now is None else now)
    start = assert_admission(session_id, runner)
    marker_key = deployment_marker_key(session_id)
    existing = item(marker_key, runner)
    if existing:
        if (
            existing.get("key", {}).get("S") != marker_key
            or existing.get("session_id", {}).get("S") != session_id
            or existing.get("environment", {}).get("S") != ENVIRONMENT
        ):
            raise LeaseError("The durable deployment marker is malformed or belongs to another session")
        assert_owner(owner, session_id, now=now, runner=runner)
        return
    record = {
        "key": {"S": marker_key},
        "session_id": {"S": session_id},
        "environment": {"S": ENVIRONMENT},
        "started_at": {"N": str(instant)},
        "commit_sha": {"S": commit_sha},
        "workflow_run_id": {"N": workflow_run_id},
    }
    deadline = start + 4 * 60 * 60
    transaction = [
        {
            "ConditionCheck": {
                "TableName": TABLE,
                "Key": {"key": {"S": "lifecycle-lock"}},
                "ConditionExpression": "owner_id = :owner AND session_id = :session AND expires_at > :now AND expires_at <= :deadline",
                "ExpressionAttributeValues": {
                    ":owner": {"S": owner},
                    ":session": {"S": session_id},
                    ":now": {"N": str(instant)},
                    ":deadline": {"N": str(deadline)},
                },
            }
        },
        {
            "Put": {
                "TableName": TABLE,
                "Item": record,
                "ConditionExpression": "attribute_not_exists(#key)",
                "ExpressionAttributeNames": {"#key": "key"},
            }
        },
    ]
    request(
        [
            "transact-write-items",
            "--transact-items",
            json.dumps(transaction),
        ],
        runner,
    )
    assert_owner(owner, session_id, now=now, runner=runner)


def release(owner, session_id, runner=subprocess.run):
    """Release only the lock still owned by this workflow and session."""
    validate_session_id(session_id)
    request(
        [
            "update-item",
            "--table-name",
            TABLE,
            "--key",
            json.dumps({"key": {"S": "lifecycle-lock"}}),
            "--update-expression",
            "REMOVE owner_id, expires_at, session_id",
            "--condition-expression",
            "owner_id = :owner AND session_id = :session",
            "--expression-attribute-values",
            json.dumps({":owner": {"S": owner}, ":session": {"S": session_id}}),
        ],
        runner,
    )


def read_owner(path):
    try:
        value = Path(path).read_text().strip()
    except OSError as exc:
        raise LeaseError("The staging lifecycle owner file is unavailable") from exc
    if not value or len(value) > 64:
        raise LeaseError("The staging lifecycle owner file is invalid")
    return value


def write_heartbeat_status(owner_file, lost=False):
    status = owner_file.with_name(owner_file.name + (".lost" if lost else ".heartbeat"))
    descriptor = os.open(status, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as target:
        target.write(str(int(time.time())) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=[
            "acquire",
            "assert",
            "heartbeat",
            "release",
            "mark-deployment",
            "acquire-finalizer",
            "assert-finalizer",
            "heartbeat-finalizer",
        ],
    )
    parser.add_argument("--owner-file", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--commit-sha")
    parser.add_argument("--workflow-run-id")
    parser.add_argument("--interval", type=int, default=30)
    args = parser.parse_args()
    try:
        if args.operation == "acquire":
            owner = str(uuid4())
            acquire(owner, args.session_id)
            descriptor = os.open(args.owner_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as target:
                target.write(owner + "\n")
        elif args.operation == "acquire-finalizer":
            owner = str(uuid4())
            acquire_finalizer(owner, args.session_id)
            descriptor = os.open(args.owner_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as target:
                target.write(owner + "\n")
        else:
            owner = read_owner(args.owner_file)
            if args.operation == "assert":
                assert_owner(owner, args.session_id)
            elif args.operation == "assert-finalizer":
                assert_finalizer_owner(owner, args.session_id)
            elif args.operation == "release":
                release(owner, args.session_id)
            elif args.operation == "mark-deployment":
                mark_deployment_started(
                    owner,
                    args.session_id,
                    args.commit_sha,
                    args.workflow_run_id,
                )
            else:
                if not 10 <= args.interval <= 60:
                    raise LeaseError("Heartbeat interval must be 10–60 seconds")
                while True:
                    try:
                        if args.operation == "heartbeat-finalizer":
                            renew_finalizer(owner, args.session_id)
                        else:
                            renew(owner, args.session_id)
                        write_heartbeat_status(args.owner_file)
                    except LeaseError:
                        write_heartbeat_status(args.owner_file, lost=True)
                        raise
                    time.sleep(args.interval)
        return 0
    except (LeaseError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
