#!/usr/bin/env python3
"""Validate a Terraform management destroy plan before external finalization."""

import argparse
import json
import sys
from pathlib import Path

from lifecycle.core import Refused
from lifecycle.management_finalizer import inspect_management_destroy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-json", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--account-id", default="088759500358")
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()
    try:
        plan = json.loads(args.plan_json.read_text())
        addresses = inspect_management_destroy(
            plan,
            {
                "account_id": args.account_id,
                "region": args.region,
                "environment": "staging",
                "session_id": args.session_id,
            },
        )
        print(json.dumps({"validated": True, "resource_addresses": addresses}))
        return 0
    except (OSError, json.JSONDecodeError, Refused) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
