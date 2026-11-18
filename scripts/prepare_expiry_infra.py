"""Upload a reviewed expiry archive and prepare private Terraform inputs.

This command prepares artifacts only. It never runs Terraform or mutates AWS
infrastructure; the only optional remote operation is the explicit S3 archive
upload requested by its caller.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from scripts.lifecycle.core import Refused, command, inspect_destroy, validate_config
from scripts.lifecycle.policies import expiry_executor_policy
from scripts.package_expiry_archive import _runtime_source_files


class PreparationError(ValueError):
    """Inputs are not sufficient to prepare a safe expiry stack."""


def _json_file(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparationError(f"{label} must be readable JSON") from exc
    if not isinstance(value, dict):
        raise PreparationError(f"{label} must be a JSON object")
    return value


def upload_archive(archive: Path, bucket: str, key_arn: str, session_id: str, runner=command) -> dict:
    """Upload one archive version and verify its exact S3 version/checksum."""
    if not archive.is_file() or archive.is_symlink():
        raise PreparationError("Expiry archive must be a regular, non-symlink file")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise PreparationError("Expiry source bucket name is invalid")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,15}", session_id):
        raise PreparationError("Expiry session ID is invalid")
    arn_match = re.fullmatch(r"arn:aws:kms:([a-z0-9-]+):(\d{12}):key/([a-f0-9-]{36})", key_arn)
    if not arn_match:
        raise PreparationError("Expiry archive KMS key must be a customer-managed key ARN")
    data = archive.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    checksum = base64.b64encode(bytes.fromhex(sha256)).decode("ascii")
    key = f"expiry/{session_id}/{sha256}/source.zip"
    put = json.loads(runner([
        "aws", "s3api", "put-object", "--bucket", bucket, "--key", key,
        "--body", str(archive.resolve()), "--server-side-encryption", "aws:kms",
        "--ssekms-key-id", key_arn, "--checksum-algorithm", "SHA256",
        "--checksum-sha256", checksum, "--metadata", "sha256=" + sha256,
        "--output", "json",
    ]))
    version = put.get("VersionId")
    if not isinstance(version, str) or not version or version == "null":
        raise PreparationError("Expiry source bucket must return a non-null object VersionId")
    head = json.loads(runner([
        "aws", "s3api", "head-object", "--bucket", bucket, "--key", key,
        "--version-id", version, "--checksum-mode", "ENABLED", "--output", "json",
    ]))
    if (
        head.get("VersionId") != version
        or head.get("ServerSideEncryption") != "aws:kms"
        or head.get("SSEKMSKeyId") != key_arn
        or head.get("ChecksumSHA256") != checksum
        or head.get("ContentLength") != len(data)
        or head.get("Metadata", {}).get("sha256") != sha256
    ):
        raise PreparationError("Uploaded expiry source version failed exact KMS/checksum verification")
    return {
        "bucket": bucket,
        "key": key,
        "version_id": version,
        "kms_key_arn": key_arn,
        "sha256": sha256,
        "size": len(data),
    }


def _write_private_new(path: Path, data: bytes) -> None:
    path = path.expanduser().absolute()
    if path.exists() or path.is_symlink():
        raise PreparationError("Terraform variables output already exists; it is never overwritten")
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise PreparationError("Terraform variables parent must be an existing real directory")
    linked = False
    with tempfile.NamedTemporaryFile(mode="wb", prefix=".expiry-tfvars-", dir=parent, delete=False) as stream:
        temporary = Path(stream.name)
        os.chmod(temporary, 0o600)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.link(temporary, path)
            linked = True
        except Exception:
            if linked and path.exists():
                path.unlink()
            raise
        finally:
            temporary.unlink(missing_ok=True)
    os.chmod(path, 0o600)


def _verify_archive(archive_path: Path, workspace: Path, intent_path: Path) -> None:
    """Bind uploaded archive bytes to this checkout and the exact reviewed plan."""
    if archive_path.is_symlink() or not archive_path.is_file():
        raise PreparationError("Expiry archive must be a regular, non-symlink file")
    workspace = workspace.resolve(strict=True)
    try:
        with ZipFile(archive_path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise PreparationError("Expiry archive contains duplicate paths")
            source_files = _runtime_source_files(workspace)
            fixed_names = {
                "artifacts/expiry-intent.json", "artifacts/expiry.tfplan",
                "artifacts/runtime.auto.tfvars.json", "artifacts/runtime.backend.hcl",
                "infra/runtime/artifacts/runtime.backend.hcl",
            }
            expected_names = {name for _, name in source_files} | fixed_names
            if set(names) != expected_names:
                raise PreparationError("Expiry archive file list differs from the reviewed source package")
            for info in infos:
                mode = info.external_attr >> 16
                if mode and (mode & 0o170000) != 0o100000:
                    raise PreparationError("Expiry archive may contain only regular files")
            for path, name in source_files:
                if archive.read(name) != path.read_bytes():
                    raise PreparationError("Expiry archive source differs from the current reviewed checkout")
            intent_bytes = intent_path.read_bytes()
            if archive.read("artifacts/expiry-intent.json") != intent_bytes:
                raise PreparationError("Expiry archive intent differs from the supplied reviewed intent")
            saved_plan = archive.read("artifacts/expiry.tfplan")
            intent = _json_file(intent_path, "Expiry intent")
            if hashlib.sha256(saved_plan).hexdigest() != intent.get("saved_plan_sha256"):
                raise PreparationError("Expiry archive binary plan does not match its approved intent")
            local_plan = workspace / "artifacts/expiry.tfplan"
            if not local_plan.is_file() or saved_plan != local_plan.read_bytes():
                raise PreparationError("Expiry archive binary plan differs from the reviewed workspace plan")
            backend = archive.read("artifacts/runtime.backend.hcl")
            if backend != archive.read("infra/runtime/artifacts/runtime.backend.hcl"):
                raise PreparationError("Expiry archive backend copies differ")
            if hashlib.sha256(backend).hexdigest() != intent.get("runtime_backend_sha256"):
                raise PreparationError("Expiry archive backend does not match the approved intent")
            local_backend = workspace / "artifacts/runtime.backend.hcl"
            local_variables = workspace / "artifacts/runtime.auto.tfvars.json"
            if not local_backend.is_file() or not local_variables.is_file():
                raise PreparationError("Reviewed runtime variable/backend files are missing from the workspace")
            if backend != local_backend.read_bytes():
                raise PreparationError("Expiry archive backend differs from the current reviewed workspace")
            variables_bytes = archive.read("artifacts/runtime.auto.tfvars.json")
            if variables_bytes != local_variables.read_bytes():
                raise PreparationError("Expiry archive runtime variables differ from the current reviewed workspace")
            variables = json.loads(variables_bytes)
            if (variables.get("environment") != intent["config"].get("environment")
                    or variables.get("session_id") != intent["config"].get("session_id")
                    or variables.get("account_id") != intent["config"].get("account_id")):
                raise PreparationError("Expiry archive runtime variables do not match the approved intent")
    except (OSError, BadZipFile, KeyError, json.JSONDecodeError) as exc:
        raise PreparationError("Expiry archive is corrupt or incomplete") from exc


def prepare_tfvars(intent_path: Path, inputs_path: Path,
                   archive_path: Path, output_path: Path, runner=command,
                   workspace: Path = Path.cwd()) -> tuple[Path, dict]:
    intent = _json_file(intent_path, "Expiry intent")
    inputs = _json_file(inputs_path, "Expiry deployment inputs")
    config = intent.get("config")
    try:
        validate_config(config)
    except (Refused, TypeError) as exc:
        raise PreparationError("Expiry intent has invalid lifecycle configuration") from exc
    if intent.get("version") != 1 or intent.get("operation") != "expiry":
        raise PreparationError("A reviewed expiry intent is required")
    if intent.get("runtime_state_key") != f"deepresearch/{config['environment']}/runtime.tfstate":
        raise PreparationError("Expiry intent does not bind the exact environment runtime state key")
    if intent.get("runtime_state_bucket") != inputs.get("runtime_state_bucket"):
        raise PreparationError("Expiry deployment inputs differ from the approved runtime state bucket")
    approved_hash = inputs.get("approved_intent_sha256")
    canonical = json.dumps(intent, sort_keys=True, separators=(",", ":")).encode()
    if not isinstance(approved_hash, str) or hashlib.sha256(canonical).hexdigest() != approved_hash:
        raise PreparationError("Expiry intent does not match its explicitly reviewed digest")
    if config["environment"] != "staging":
        raise PreparationError("This preparation command only accepts admitted staging sessions")
    if set(inputs) != {
        "approved_intent_sha256", "runtime_state_bucket", "management_vpc_id",
        "management_subnet_ids", "management_security_group_ids", "source_bucket",
        "source_kms_key_arn", "runner_image_digest", "runner_repository_arn",
    }:
        raise PreparationError("Expiry deployment inputs contain missing or unsupported fields")

    workspace = workspace.resolve(strict=True)
    try:
        _verify_archive(archive_path, workspace, intent_path)
        local_plan = workspace / "artifacts/expiry.tfplan"
        if hashlib.sha256(local_plan.read_bytes()).hexdigest() != intent.get("saved_plan_sha256"):
            raise PreparationError("Runtime binary plan does not match the approved expiry intent")
        raw_plan = runner([
            "terraform", f"-chdir={workspace / 'infra/runtime'}", "show", "-json", str(local_plan),
        ])
        runtime_plan = json.loads(raw_plan)
        deletions = inspect_destroy(runtime_plan, config)
    except Refused as exc:
        raise PreparationError("Runtime plan is not the exact approved staging destroy scope") from exc
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise PreparationError("Could not inspect the exact approved Terraform binary plan") from exc
    if intent.get("deletions") != deletions:
        raise PreparationError("Inspected runtime deletions differ from the reviewed expiry intent")
    changes = {row.get("address"): row for row in runtime_plan.get("resource_changes", [])}
    vpc = changes.get("aws_vpc.main", {}).get("change", {}).get("before", {})
    runtime_vpc_id = vpc.get("id")
    if not isinstance(runtime_vpc_id, str) or not re.fullmatch(r"vpc-[a-f0-9]{8,17}", runtime_vpc_id):
        raise PreparationError("Runtime plan does not contain the exact runtime VPC ID")
    for name in ("management_vpc_id",):
        if not isinstance(inputs[name], str) or not re.fullmatch(r"vpc-[a-f0-9]{8,17}", inputs[name]):
            raise PreparationError("Management VPC ID is invalid")
    if inputs["management_vpc_id"] == runtime_vpc_id:
        raise PreparationError("Expiry executor network must be separate from the runtime VPC")
    for name, pattern in (("management_subnet_ids", r"subnet-[a-f0-9]{8,17}"),
                          ("management_security_group_ids", r"sg-[a-f0-9]{8,17}")):
        value = inputs[name]
        if not isinstance(value, list) or not value or not all(isinstance(x, str) and re.fullmatch(pattern, x) for x in value):
            raise PreparationError("Management subnet/security group IDs are invalid")
    account, region = config["account_id"], config["region"]
    expected_repo_prefix = f"arn:aws:ecr:{region}:{account}:repository/"
    image = inputs["runner_image_digest"]
    repository_arn = inputs["runner_repository_arn"]
    if not isinstance(repository_arn, str) or not repository_arn.startswith(expected_repo_prefix):
        raise PreparationError("Runner ECR repository must belong to the approved account and region")
    repository_name = repository_arn[len(expected_repo_prefix):]
    if not isinstance(image, str) or not re.fullmatch(re.escape(repository_name) + r"@sha256:[a-f0-9]{64}", image):
        raise PreparationError("Runner image must be the exact immutable digest in the configured ECR repository")
    source_key_arn = inputs["source_kms_key_arn"]
    if not isinstance(source_key_arn, str) or not re.fullmatch(
        rf"arn:aws:kms:{re.escape(region)}:{account}:key/[a-f0-9-]{{36}}", source_key_arn
    ):
        raise PreparationError("Expiry source KMS key must belong to the approved account and region")
    policy = expiry_executor_policy(config, runtime_plan, inputs["runtime_state_bucket"])
    not_before = datetime.fromisoformat(intent["not_before"].replace("Z", "+00:00"))
    if not_before.tzinfo is None:
        raise PreparationError("Expiry trigger must include an explicit timezone")
    expire_at = not_before.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Complete local scope validation before the explicit object upload.
    upload = upload_archive(archive_path, inputs["source_bucket"], source_key_arn, config["session_id"], runner)
    tfvars = {
        "region": region,
        "environment": config["environment"],
        "session_id": config["session_id"],
        "expire_at_utc": expire_at,
        "source_bucket": upload["bucket"],
        "source_key": upload["key"],
        "source_version_id": upload["version_id"],
        "source_kms_key_arn": upload["kms_key_arn"],
        "runner_image_digest": image,
        "runner_repository_arn": repository_arn,
        "approved_intent_sha256": approved_hash,
        "executor_policy_json": json.dumps(policy, sort_keys=True, separators=(",", ":")),
        "management_vpc_id": inputs["management_vpc_id"],
        "runtime_vpc_id": runtime_vpc_id,
        "management_subnet_ids": inputs["management_subnet_ids"],
        "management_security_group_ids": inputs["management_security_group_ids"],
        "alert_topic_arn": config["alert_topic_arn"],
    }
    output_path = output_path.expanduser().absolute()
    _write_private_new(output_path, (json.dumps(tfvars, sort_keys=True, indent=2) + "\n").encode())
    return output_path, {"source": upload, "intent_sha256": approved_hash, "deletion_count": len(deletions)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intent", type=Path, required=True)
    parser.add_argument("--deployment-inputs", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        output, summary = prepare_tfvars(args.intent, args.deployment_inputs, args.archive, args.output, workspace=args.workspace)
    except (PreparationError, OSError, Refused, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps({"tfvars": str(output), **summary}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
