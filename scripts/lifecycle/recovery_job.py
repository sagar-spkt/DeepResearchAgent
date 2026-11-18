"""Run the hash-only recovery verifier in a short-lived staging pod."""

import hashlib
import json
import re
import signal
import tempfile
import threading
import uuid
from pathlib import Path
from uuid import UUID

from .core import Refused, digest, prepare_database_restore, timestamp


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def run_restore_verifier(
    adapter, plan, backup_receipt, restore_receipt, user_email, conversation_id, query
):
    config = plan["config"]
    if config.get("environment") != "staging":
        raise Refused("Synthetic restore verification is staging-only")
    try:
        expected_plan = prepare_database_restore(
            backup_receipt, instant=timestamp(plan["created_at"])
        )
    except (KeyError, ValueError) as exc:
        raise Refused("Recovery verification requires the approved database restore plan") from exc
    for key in (
        "config",
        "receipt_sha256",
        "source_database_identifier",
        "source_snapshot_identifier",
        "source_snapshot_arn",
        "target_database_identifier",
        "db_subnet_group_name",
        "restore_security_group_name",
        "db_instance_class",
        "tags",
    ):
        if plan.get(key) != expected_plan.get(key):
            raise Refused("Recovery verification plan does not match its retained backup receipt")
    if any(
        plan.get(key) is not False
        for key in (
            "restore_rehearsed",
            "database_restore_verified",
            "application_restore_rehearsed",
            "full_recovery_accepted",
        )
    ):
        raise Refused("Recovery verification plan cannot claim recovery acceptance")
    if (
        backup_receipt.get("config") != config
        or digest(backup_receipt) != plan.get("receipt_sha256")
        or restore_receipt.get("operation") != "database-restore-receipt"
        or restore_receipt.get("config") != config
        or restore_receipt.get("plan_hash") != digest(plan)
        or restore_receipt.get("provider_verified") is not True
        or restore_receipt.get("database_restore_verified") is not True
        or restore_receipt.get("target_database_identifier") != plan.get("target_database_identifier")
        or restore_receipt.get("target_database_arn")
        != f"arn:aws:rds:{config['region']}:{config['account_id']}:db:{plan['target_database_identifier']}"
        or restore_receipt.get("source_snapshot_arn") != plan.get("source_snapshot_arn")
    ):
        raise Refused("Recovery verification artifacts do not bind to the approved staging restore")
    if (
        not isinstance(user_email, str)
        or not re.fullmatch(r"recovery\+[a-z0-9-]+@example\.test", user_email)
        or not isinstance(conversation_id, str)
        or not isinstance(query, str)
        or not query.strip()
        or len(query) > 1000
    ):
        raise Refused("Restore verification requires a reserved synthetic user, conversation and query")
    try:
        if str(UUID(conversation_id)) != conversation_id:
            raise ValueError("noncanonical UUID")
    except ValueError as exc:
        raise Refused("Synthetic recovery conversation ID must be a canonical UUID") from exc

    target_endpoint = restore_receipt.get("target_database_endpoint")
    if (
        not isinstance(target_endpoint, str)
        or not re.fullmatch(r"[a-zA-Z0-9.-]+\.rds\.amazonaws\.com", target_endpoint)
        or restore_receipt.get("target_database_port") != 5432
    ):
        raise Refused("Restore receipt has no verified private PostgreSQL endpoint")

    job_name = "restore-verify-" + hashlib.sha256(config["session_id"].encode()).hexdigest()[:8] + "-" + uuid.uuid4().hex[:8]
    secret_name = job_name + "-input"
    access = None
    job_created = False
    secret_created = False
    primary_error = None
    old_handlers = {}
    if threading.current_thread() is threading.main_thread():
        def cleanup_on_signal(signum, frame):
            raise InterruptedError(f"restore verifier interrupted by signal {signum}")

        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, cleanup_on_signal)
    try:
        deployment = json.loads(adapter.kubectl("get", "deployment", "research-api", "-o", "json"))
        pod_template = deployment.get("spec", {}).get("template", {}).get("spec", {})
        containers = pod_template.get("containers", [])
        if len(containers) != 1:
            raise Refused("Research API deployment does not identify one verifier image")
        image = containers[0].get("image")
        env_from = containers[0].get("envFrom", [])
        app_secret_refs = [
            item.get("secretRef", {}).get("name")
            for item in env_from
            if item.get("secretRef", {}).get("name")
        ]
        if (
            not isinstance(image, str)
            or not re.fullmatch(r".+@sha256:[0-9a-f]{64}", image)
            or pod_template.get("serviceAccountName") != "research-application"
            or len(app_secret_refs) != 1
        ):
            raise Refused("Research API verifier image, service account, or secret is not immutable and exact")

        access = adapter.open_restore_verifier_ingress(plan, restore_receipt)
        secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": secret_name, "namespace": "research"},
            "type": "Opaque",
            "stringData": {
                "restore-receipt.json": _json(restore_receipt),
                "backup-receipt.json": _json(backup_receipt),
                "request.json": _json({
                    "user_email": user_email,
                    "conversation_id": conversation_id,
                    "query": query,
                }),
            },
        }
        with tempfile.TemporaryDirectory(prefix="recovery-verifier-") as temp_dir:
            secret_path = Path(temp_dir) / "input-secret.json"
            secret_path.write_text(_json(secret))
            secret_path.chmod(0o600)
            secret_created = True
            adapter.kubectl("create", "-f", str(secret_path))

        job = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": job_name, "namespace": "research"},
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": 600,
                "ttlSecondsAfterFinished": 600,
                "template": {
                    "metadata": {"labels": {"app": "deepresearch-restore-verifier"}},
                    "spec": {
                        "restartPolicy": "Never",
                        "serviceAccountName": "research-application",
                        "nodeSelector": {"workload": "cpu"},
                        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "fsGroup": 10001},
                        "containers": [{
                            "name": "verifier",
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "resources": containers[0].get("resources", {}),
                            "command": ["python", "-m", "research_app.recovery_verifier", "rehearse"],
                            "args": [
                                "--backup-receipt", "/run/recovery/backup-receipt.json",
                                "--restore-receipt", "/run/recovery/restore-receipt.json",
                                "--request", "/run/recovery/request.json",
                                "--output", "/tmp/recovery-verification.json",
                            ],
                            "env": [
                                *containers[0].get("env", []),
                                {"name": "RECOVERY_TARGET_DATABASE_ENDPOINT", "value": target_endpoint},
                            ],
                            "envFrom": env_from,
                            "volumeMounts": [{"name": "input", "mountPath": "/run/recovery", "readOnly": True}],
                            "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                        }],
                        "volumes": [{"name": "input", "secret": {"secretName": secret_name, "defaultMode": 288}}],
                    },
                },
            },
        }
        with tempfile.TemporaryDirectory(prefix="recovery-verifier-") as temp_dir:
            job_path = Path(temp_dir) / "job.json"
            job_path.write_text(_json(job))
            job_path.chmod(0o600)
            job_created = True
            adapter.kubectl("create", "-f", str(job_path))
        adapter.kubectl("wait", "--for=condition=complete", f"job/{job_name}", "--timeout=10m")
        result = json.loads(adapter.kubectl("exec", f"job/{job_name}", "--", "cat", "/tmp/recovery-verification.json"))
        if (
            result.get("operation") != "application-recovery-verification-receipt"
            or result.get("restore_receipt_sha256") != digest(restore_receipt)
            or not isinstance(result.get("fixture_manifest_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", result["fixture_manifest_sha256"])
            or result.get("application_data_verified") is not True
            or result.get("retrieval_verified") is not True
        ):
            raise Refused("In-cluster recovery verifier returned an incomplete or mismatched receipt")
        result["temporary_ingress_source_security_group_ids"] = access["source_security_group_ids"]
        result["temporary_ingress_removed"] = True
        return result
    except Exception as exc:
        primary_error = exc
        raise
    finally:
        cleanup_errors = []
        try:
            for resource, created in (("job", job_created), ("secret", secret_created)):
                if created:
                    try:
                        adapter.kubectl("delete", resource, job_name if resource == "job" else secret_name, "--ignore-not-found=true", "--wait=true", "--timeout=60s")
                    except Exception as exc:
                        cleanup_errors.append(f"could not delete verifier {resource}: {exc}")
            if access is not None:
                try:
                    adapter.close_restore_verifier_ingress(access)
                except Exception as exc:
                    cleanup_errors.append(f"could not revoke verifier ingress: {exc}")
        finally:
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)
        if cleanup_errors:
            message = "; ".join(cleanup_errors)
            if primary_error is not None:
                raise Refused(f"Verifier failed ({primary_error}); cleanup failed: {message}") from primary_error
            raise Refused(message)
