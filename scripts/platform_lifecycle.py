"""Plan, collect, back up and safely prepare runtime teardown. No implicit AWS writes."""

import argparse
import json
import os
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path

from lifecycle.aws_adapter import AWSAdapter
from lifecycle.control import ControlStore
from lifecycle.core import (
    Refused,
    backup_execute,
    backup_plan,
    bootstrap_cleanup_execute,
    command,
    digest,
    execute_database_restore,
    execute_expiry,
    now,
    prepare_bootstrap_cleanup,
    prepare_database_restore,
    prepare_expiry,
    prepare_shutdown,
    restore_prepare,
    shutdown_execute,
    validate_config,
)
from lifecycle.inventory import CLIENTS, InventoryCollector, release_admission, release_plan
from lifecycle.recovery_job import run_restore_verifier


def load(path):
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise Refused("Expected JSON object")
    return value


def save(path, value):
    target = Path(path)
    # Receipts include private object names. Never overwrite approved artifacts.
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"artifact": str(target), "sha256": digest(value)}))


def adapter(config):
    import boto3

    session = boto3.Session(region_name=config["region"])
    return AWSAdapter(
        config,
        {name: session.client(name) for name in CLIENTS},
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "operation",
        choices=[
            "plan-expiry",
            "expiry",
            "plan-backup",
            "collect",
            "backup",
            "verify-receipt",
            "plan-shutdown",
            "shutdown",
            "plan-bootstrap-cleanup",
            "bootstrap-cleanup",
            "plan-database-restore",
            "database-restore",
            "verify-database-restore",
            "restore",
            "deploy",
            "promote",
            "purge",
            "backup-shutdown",
            "plan-admission",
            "admit",
            "inventory",
            "plan-release",
            "release",
        ],
    )
    p.add_argument("--config", type=Path)
    p.add_argument("--plan", type=Path)
    p.add_argument("--receipt", type=Path)
    p.add_argument("--backup-receipt", type=Path)
    p.add_argument("--user-email")
    p.add_argument("--conversation-id")
    p.add_argument("--query")
    p.add_argument("--output", type=Path)
    p.add_argument("--approved-plan-hash")
    p.add_argument("--workspace", type=Path, default=Path.cwd())
    p.add_argument("--runtime-dir", type=Path)
    p.add_argument("--var-file", type=Path)
    p.add_argument("--saved-plan", type=Path)
    p.add_argument("--runtime-state-bucket")
    p.add_argument("--runtime-backend", type=Path)
    p.add_argument("--bootstrap-cleanup-plan", type=Path)
    p.add_argument("--management-cleanup-receipt", type=Path)
    p.add_argument("--environment", choices=["staging", "production"])
    p.add_argument("--execute", action="store_true")
    args = p.parse_args()
    started = False
    reserved = None
    stack = ExitStack()
    try:
        if args.operation in ["deploy", "promote", "purge", "backup-shutdown"]:
            raise Refused(
                "Use approved lifecycle runbooks. Deployment, promotion, and foundation purge are not enabled."
            )
        if args.operation in [
            "backup",
            "shutdown",
            "bootstrap-cleanup",
            "database-restore",
            "verify-database-restore",
            "expiry",
            "admit",
            "release",
        ]:
            if not (args.execute and args.plan and args.approved_plan_hash and args.output):
                raise Refused("Mutation requires --execute --plan --approved-plan-hash --output")
            plan = load(args.plan)
            # Validate approval before constructing clients or reading cloud resources.
            from lifecycle.core import approve

            approve(plan, args.approved_plan_hash)
            expected_operation = {
                "backup": "backup",
                "shutdown": "shutdown",
                "bootstrap-cleanup": "bootstrap-cleanup",
                "database-restore": "database-restore",
                "verify-database-restore": "database-restore",
                "expiry": "expiry",
                "admit": "admission",
            }.get(args.operation)
            if args.operation == "release":
                if plan.get("operation") not in {
                    "release",
                    "bootstrap-release",
                    "empty-admission-release",
                }:
                    raise Refused("Command operation does not match the approved plan")
            elif plan.get("operation") != expected_operation:
                raise Refused("Command operation does not match the approved plan")
            cleanup_plan = cleanup_receipt = None
            if plan.get("operation") == "bootstrap-release":
                if not args.bootstrap_cleanup_plan or not args.receipt:
                    raise Refused(
                        "Bootstrap release requires --bootstrap-cleanup-plan and --receipt"
                    )
                cleanup_plan = load(args.bootstrap_cleanup_plan)
                cleanup_receipt = load(args.receipt)
                from lifecycle.inventory import _validate_bootstrap_cleanup_evidence

                _validate_bootstrap_cleanup_evidence(plan["config"], cleanup_plan, cleanup_receipt)
                if plan.get("bootstrap_cleanup_plan_sha256") != digest(cleanup_plan) or plan.get(
                    "bootstrap_cleanup_receipt_sha256"
                ) != digest(cleanup_receipt):
                    raise Refused(
                        "Bootstrap release artifacts do not match the approved release plan"
                    )
            elif args.bootstrap_cleanup_plan:
                raise Refused("--bootstrap-cleanup-plan is only valid for bootstrap release")
            management_cleanup_receipt = None
            if plan.get("operation") == "empty-admission-release":
                if not args.management_cleanup_receipt:
                    raise Refused("Empty-session release requires --management-cleanup-receipt")
                management_cleanup_receipt = load(args.management_cleanup_receipt)
                from lifecycle.inventory import _validate_empty_session_cleanup_receipt

                _validate_empty_session_cleanup_receipt(plan["config"], management_cleanup_receipt)
                if plan.get("management_cleanup_receipt_sha256") != digest(
                    management_cleanup_receipt
                ):
                    raise Refused("Management cleanup receipt does not match the approved release plan")
            elif args.management_cleanup_receipt:
                raise Refused("--management-cleanup-receipt is only valid for empty-session release")
            restore_receipt = None
            verification_backup_receipt = None
            verification_user_email = verification_conversation_id = verification_query = None
            if args.operation in {"database-restore", "verify-database-restore"}:
                if not args.receipt:
                    raise Refused("Database restore operation requires --receipt")
                restore_receipt = load(args.receipt)
                if args.operation == "database-restore":
                    if restore_receipt.get("config") != plan.get("config") or digest(
                        restore_receipt
                    ) != plan.get("receipt_sha256"):
                        raise Refused(
                            "Backup receipt does not match the approved database restore plan"
                        )
                else:
                    if not all(
                        [args.backup_receipt, args.user_email, args.conversation_id, args.query]
                    ):
                        raise Refused(
                            "Restore verification requires --backup-receipt, --user-email, --conversation-id and --query"
                        )
                    verification_backup_receipt = load(args.backup_receipt)
                    verification_user_email = args.user_email
                    verification_conversation_id = args.conversation_id
                    verification_query = args.query
                    if (
                        verification_backup_receipt.get("config") != plan.get("config")
                        or digest(verification_backup_receipt) != plan.get("receipt_sha256")
                    ):
                        raise Refused("Backup receipt does not match the approved restore plan")
            if args.environment and args.environment != plan["config"]["environment"]:
                raise Refused("Environment argument conflicts with the approved plan")
            reserved = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(
                reserved,
                json.dumps(
                    {
                        "status": "pending",
                        "operation": args.operation,
                        "plan_hash": digest(plan),
                        "config": plan["config"],
                    }
                ).encode(),
            )
            os.fsync(reserved)
            aws = adapter(plan["config"])
            started = True
            control = ControlStore(aws.clients["dynamodb"])
            if args.operation == "admit":
                if plan.get("operation") != "admission" or plan.get("reserved_seconds") != 21600:
                    raise Refused("Not an approved six-hour admission plan")
                result = control.reserve(plan["config"], plan["start_epoch"])
            else:
                lease = stack.enter_context(control.acquire(plan["config"]))
                aws.exclusive_guard = lease.assert_owned

                def guarded_runner(argv, **kwargs):
                    lease.assert_owned()
                    return command(argv, **kwargs)

                aws.runner = guarded_runner
                if args.operation == "release":
                    result = release_admission(
                        control,
                        plan["config"],
                        lease,
                        aws.clients,
                        plan,
                        args.approved_plan_hash,
                        cleanup_plan=cleanup_plan,
                        cleanup_receipt=cleanup_receipt,
                        management_cleanup_receipt=management_cleanup_receipt,
                    )
                elif args.operation == "expiry":
                    result = execute_expiry(
                        plan, args.approved_plan_hash, args.workspace, aws, runner=guarded_runner
                    )
                elif args.operation == "database-restore":
                    result = execute_database_restore(
                        plan, restore_receipt, args.approved_plan_hash, aws
                    )
                elif args.operation == "verify-database-restore":
                    result = run_restore_verifier(
                        aws,
                        plan,
                        verification_backup_receipt,
                        restore_receipt,
                        verification_user_email,
                        verification_conversation_id,
                        verification_query,
                    )
                elif args.operation == "backup":
                    result = backup_execute(plan, args.approved_plan_hash, aws)
                elif args.operation == "bootstrap-cleanup":
                    result = bootstrap_cleanup_execute(
                        plan, args.approved_plan_hash, aws, runner=guarded_runner
                    )
                else:
                    result = shutdown_execute(
                        plan, args.approved_plan_hash, aws, runner=guarded_runner
                    )
        else:
            if args.execute:
                raise Refused("--execute requires a supported mutating operation")
            if not args.output:
                raise Refused("--output is required")
            config = validate_config(load(args.config)) if args.config else None
            if args.environment and config and args.environment != config["environment"]:
                raise Refused("Environment mismatch")
            if args.operation == "plan-release":
                if config is None:
                    raise Refused("--config required")
                if bool(args.bootstrap_cleanup_plan) != bool(args.receipt):
                    raise Refused(
                        "Bootstrap release planning requires both --bootstrap-cleanup-plan and --receipt"
                    )
                if args.management_cleanup_receipt and (
                    args.bootstrap_cleanup_plan or args.receipt
                ):
                    raise Refused("Empty-session and bootstrap cleanup evidence cannot be combined")
                result = release_plan(
                    config,
                    bootstrap_cleanup_plan=(
                        load(args.bootstrap_cleanup_plan) if args.bootstrap_cleanup_plan else None
                    ),
                    cleanup_receipt=load(args.receipt) if args.receipt else None,
                    management_cleanup_receipt=(
                        load(args.management_cleanup_receipt)
                        if args.management_cleanup_receipt
                        else None
                    ),
                )
            elif args.operation == "inventory":
                if config is None:
                    raise Refused("--config required")
                aws = adapter(config)
                result = InventoryCollector(config, aws.clients).collect()
            elif args.operation == "plan-admission":
                if config is None:
                    raise Refused("--config required")
                instant = now()
                result = {
                    "version": 1,
                    "operation": "admission",
                    "config": config,
                    "created_at": instant.isoformat(),
                    "expires_at": (instant + timedelta(seconds=120)).isoformat(),
                    "start_epoch": int(instant.timestamp()),
                    "reserved_seconds": 21600,
                    "monthly_limit_seconds": 72000,
                }
            elif args.operation == "plan-expiry":
                if config is None or not args.var_file or not args.runtime_state_bucket or not args.runtime_backend:
                    raise Refused("config, var-file, runtime-state-bucket, and runtime-backend required")
                aws = adapter(config)
                admission = ControlStore(aws.clients["dynamodb"]).assert_admitted(config)
                result = prepare_expiry(
                    config,
                    args.workspace,
                    str(args.var_file),
                    str(args.saved_plan or "artifacts/expiry.tfplan"),
                    admission_start=int(admission["started_at"]["N"]),
                    runtime_state_bucket=args.runtime_state_bucket,
                    runtime_backend_file=str(args.runtime_backend),
                )
            elif args.operation == "plan-backup":
                if config is None:
                    raise Refused("--config required")
                result = backup_plan(config)
            elif args.operation == "plan-database-restore":
                if not args.receipt:
                    raise Refused("--receipt required")
                result = prepare_database_restore(load(args.receipt))
            elif args.operation == "collect":
                if config is None:
                    raise Refused("--config required")
                aws = adapter(config)
                aws.verify_scope(config)
                result = {
                    "config": config,
                    "object_inventory": aws.inventory(),
                    "cluster_load_balancers": aws.cluster_albs(),
                    "executed": False,
                }
            elif args.operation == "plan-shutdown":
                if not all(
                    [config, args.receipt, args.runtime_dir, args.var_file, args.saved_plan]
                ):
                    raise Refused("config/receipt/runtime-dir/var-file/saved-plan required")
                aws = adapter(config)
                aws.verify_receipt(load(args.receipt))
                result = prepare_shutdown(
                    config, args.receipt, args.runtime_dir, args.var_file, args.saved_plan
                )
            elif args.operation == "plan-bootstrap-cleanup":
                if (
                    config is None
                    or not args.runtime_dir
                    or not args.var_file
                    or not args.saved_plan
                ):
                    raise Refused("config/runtime-dir/var-file/saved-plan required")
                result = prepare_bootstrap_cleanup(
                    config, args.runtime_dir, args.var_file, args.saved_plan
                )
            elif args.operation in ["restore", "verify-receipt"]:
                if not args.receipt:
                    raise Refused("--receipt required")
                receipt = load(args.receipt)
                aws = adapter(receipt["config"])
                result = (
                    restore_prepare(receipt, aws)
                    if args.operation == "restore"
                    else (
                        aws.verify_receipt(receipt)
                        or {"provider_verified": True, "receipt_hash": digest(receipt)}
                    )
                )
            else:
                raise Refused("Unknown operation")
        if reserved is not None:
            os.lseek(reserved, 0, os.SEEK_SET)
            os.ftruncate(reserved, 0)
            os.write(reserved, (json.dumps(result, indent=2) + "\n").encode())
            os.fsync(reserved)
            os.close(reserved)
            reserved = None
            print(json.dumps({"artifact": str(args.output), "sha256": digest(result)}))
        else:
            save(args.output, result)
        return 0
    except Exception as exc:
        # Do not serialize provider responses or secret values in error output.
        print(
            json.dumps(
                {
                    "executed": None if started else False,
                    "mutations_may_have_started": started,
                    "ready": False,
                    "error_type": type(exc).__name__,
                    "detail": str(exc)
                    if isinstance(exc, Refused)
                    else "Operation failed; inspect private provider diagnostics. No success receipt issued.",
                }
            )
        )
        return 2
    finally:
        stack.close()
        if reserved is not None:
            os.close(reserved)


if __name__ == "__main__":
    raise SystemExit(main())
