"""Build a deterministic private source archive for the staging expiry executor."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import stat
import tempfile
import zipfile
from pathlib import Path

ARCHIVE_EPOCH = (1980, 1, 1, 0, 0, 0)
BACKEND_KEY = re.compile(r'^\s*key\s*=\s*"(?P<value>[^"]+)"\s*$', re.MULTILINE)
BACKEND_BUCKET = re.compile(r'^\s*bucket\s*=\s*"(?P<value>[^"]+)"\s*$', re.MULTILINE)
RUNTIME_VARIABLES_ALLOWED = {
    "region",
    "account_id",
    "environment",
    "availability_zones",
    "gpu_availability_zone",
    "gpu_fallback_availability_zone",
    "gpu_desired_size",
    "administrator_cidrs",
    "operator_principal_arn",
    "kubernetes_version",
    "session_id",
    "database_snapshot_identifier",
    "database_instance_class",
    "foundation",
}
RUNTIME_VARIABLES_REQUIRED = {
    "account_id",
    "environment",
    "availability_zones",
    "administrator_cidrs",
    "operator_principal_arn",
    "kubernetes_version",
    "session_id",
    "foundation",
}


class ArchiveError(ValueError):
    """Raised when the approved source archive inputs are incomplete or unsafe."""


def canonical_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _inside(root: Path, value: str, label: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        raise ArchiveError(f"{label} must be workspace-relative")
    current = root
    for part in candidate.parts:
        current /= part
        if current.is_symlink():
            raise ArchiveError(f"{label} cannot use a symlink")
    resolved = (root / candidate).resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ArchiveError(f"{label} escapes the source workspace")
    if not resolved.is_file() or resolved.is_symlink():
        raise ArchiveError(f"{label} must be a regular file")
    return resolved


def _read_json(data: bytes, label: str) -> dict:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchiveError(f"{label} must be readable JSON") from exc
    if not isinstance(value, dict):
        raise ArchiveError(f"{label} must be a JSON object")
    return value


def _runtime_source_files(root: Path) -> list[tuple[Path, str]]:
    paths = [root / "scripts" / "platform_lifecycle.py"]
    paths.extend(sorted((root / "scripts" / "lifecycle").glob("*.py")))
    paths.extend(sorted((root / "infra" / "runtime").glob("*.tf")))
    paths.extend(
        [
            root / "infra" / "runtime" / ".terraform.lock.hcl",
            root / "infra" / "runtime" / "aws-load-balancer-controller-policy.json",
        ]
    )
    result = []
    for path in paths:
        relative_path = path.relative_to(root)
        current = root
        for part in relative_path.parts:
            current /= part
            if current.is_symlink():
                raise ArchiveError(f"Required expiry source is a symlink: {relative_path}")
        if not path.is_file():
            raise ArchiveError(f"Required expiry source is missing or unsafe: {path.relative_to(root)}")
        relative = relative_path.as_posix()
        if ".terraform" in path.parts or relative.endswith(".tfstate"):
            raise ArchiveError("Terraform cache/state files are forbidden in the source archive")
        result.append((path, relative))
    return result


def _reject_symlinked_parents(path: Path, workspace: Path) -> None:
    trusted_roots = {
        workspace,
        Path.home().absolute(),
        Path(tempfile.gettempdir()).absolute(),
    }
    for trusted_root in sorted(trusted_roots, key=lambda candidate: len(candidate.parts)):
        if path.is_relative_to(trusted_root):
            current = trusted_root
            for part in path.relative_to(trusted_root).parts:
                current /= part
                if current.is_symlink():
                    raise ArchiveError("Output path cannot pass through a symlink")
            return
    raise ArchiveError("Output path must be under the home directory or system temporary directory")


def _validate_runtime_variables(variables: dict, config: dict, session_id: str) -> None:
    unknown = set(variables) - RUNTIME_VARIABLES_ALLOWED
    missing = RUNTIME_VARIABLES_REQUIRED - set(variables)
    if unknown:
        raise ArchiveError("Runtime variables contain unsupported fields: " + ", ".join(sorted(unknown)))
    if missing:
        raise ArchiveError("Runtime variables are missing required fields: " + ", ".join(sorted(missing)))
    if (
        variables.get("environment") != "staging"
        or variables.get("session_id") != session_id
        or variables.get("account_id") != config.get("account_id")
        or variables.get("region", "us-east-1") != "us-east-1"
        or config.get("region") != "us-east-1"
    ):
        raise ArchiveError("Runtime variables do not match the approved staging account, region, and session")
    account = config.get("account_id")
    if not isinstance(account, str) or not re.fullmatch(r"[0-9]{12}", account):
        raise ArchiveError("Approved staging account ID is invalid")
    zones = variables["availability_zones"]
    if (
        not isinstance(zones, list)
        or len(zones) != 2
        or not all(isinstance(zone, str) and re.fullmatch(r"us-east-1[a-z]", zone) for zone in zones)
        or len(set(zones)) != 2
    ):
        raise ArchiveError("Runtime variables must contain two distinct us-east-1 availability zones")
    cidrs = variables["administrator_cidrs"]
    def valid_host_cidr(value: object) -> bool:
        if not isinstance(value, str):
            return False
        try:
            interface = ipaddress.IPv4Interface(value)
        except ValueError:
            return False
        return interface.network.prefixlen == 32 and str(interface) == value

    if (
        not isinstance(cidrs, list)
        or not cidrs
        or not all(valid_host_cidr(cidr) for cidr in cidrs)
        or len(set(cidrs)) != len(cidrs)
    ):
        raise ArchiveError("Runtime variables must contain unique IPv4 /32 administrator hosts")
    operator_arn = variables["operator_principal_arn"]
    if not isinstance(operator_arn, str) or not re.fullmatch(
        rf"arn:aws:iam::{account}:(?:user|role)/[A-Za-z0-9+=,.@_/-]+", operator_arn
    ):
        raise ArchiveError("Runtime operator principal must be an IAM identity in the approved account")
    if not isinstance(variables["kubernetes_version"], str) or not re.fullmatch(
        r"[0-9]+\.[0-9]+", variables["kubernetes_version"]
    ):
        raise ArchiveError("Runtime Kubernetes version must be a numeric minor version")
    if "gpu_desired_size" in variables and (
        type(variables["gpu_desired_size"]) is not int or variables["gpu_desired_size"] not in (0, 1)
    ):
        raise ArchiveError("GPU desired size must be zero or one")
    if "gpu_availability_zone" in variables and (
        not isinstance(variables["gpu_availability_zone"], str)
        or not re.fullmatch(r"us-east-1[a-z]", variables["gpu_availability_zone"])
    ):
        raise ArchiveError("GPU Availability Zone must be in us-east-1")
    if "gpu_fallback_availability_zone" in variables and (
        not isinstance(variables["gpu_fallback_availability_zone"], str)
        or not re.fullmatch(r"us-east-1[a-z]", variables["gpu_fallback_availability_zone"])
    ):
        raise ArchiveError("Fallback GPU Availability Zone must be in us-east-1")
    if "database_instance_class" in variables and (
        not isinstance(variables["database_instance_class"], str)
        or not re.fullmatch(r"db\.[a-z0-9.]+", variables["database_instance_class"])
    ):
        raise ArchiveError("Database instance class must be an RDS class name")
    if "database_snapshot_identifier" in variables and variables["database_snapshot_identifier"] is not None:
        snapshot = variables["database_snapshot_identifier"]
        if not isinstance(snapshot, str) or not re.fullmatch(
            r"deepresearch-staging-[a-z0-9][a-z0-9-]{0,31}-final", snapshot
        ):
            raise ArchiveError("Database snapshot identifier must name a staging final snapshot")
    foundation = variables["foundation"]
    expected_foundation = {
        "buckets",
        "queue_url",
        "queue_arn",
        "alert_topic_arn",
        "alert_kms_key_arn",
        "secrets",
    }
    if not isinstance(foundation, dict) or set(foundation) != expected_foundation:
        raise ArchiveError("Foundation variables must contain only the reviewed staging resource references")
    buckets, secrets = foundation["buckets"], foundation["secrets"]
    if not isinstance(buckets, dict) or set(buckets) != {"documents", "backups", "traces"}:
        raise ArchiveError("Foundation bucket variables must name only documents, backups, and traces")
    if not all(isinstance(value, str) and re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", value) for value in buckets.values()):
        raise ArchiveError("Foundation bucket variables must be bucket names, not credentials")
    if not isinstance(secrets, dict) or set(secrets) != {"application", "vllm", "langfuse", "web"}:
        raise ArchiveError("Foundation secret variables must contain only the four expected staging secret references")
    region = config.get("region")
    expected_secret_prefix = f"arn:aws:secretsmanager:{region}:{account}:secret:"
    if not all(isinstance(value, str) and value.startswith(expected_secret_prefix) for value in secrets.values()):
        raise ArchiveError("Foundation secret variables must be ARNs, not secret values")
    queue_url = f"https://sqs.{region}.amazonaws.com/{account}/deepresearch-staging-research"
    if foundation["queue_url"] != queue_url:
        raise ArchiveError("Foundation queue URL must target the approved staging account and region")
    expected_arns = {
        "queue_arn": f"arn:aws:sqs:{region}:{account}:deepresearch-staging-research",
        "alert_topic_arn": f"arn:aws:sns:{region}:{account}:deepresearch-staging-alerts",
    }
    if any(foundation[key] != value for key, value in expected_arns.items()):
        raise ArchiveError("Foundation queue and alert variables must be exact staging ARNs")
    if not isinstance(foundation["alert_kms_key_arn"], str) or not re.fullmatch(
        rf"arn:aws:kms:{region}:{account}:key/[a-f0-9-]{{36}}", foundation["alert_kms_key_arn"]
    ):
        raise ArchiveError("Foundation alert KMS variable must be a staging account key ARN")
    for key in ("documents", "backups", "traces"):
        expected_bucket = f"deepresearch-staging-{key}-{account}"
        if buckets[key] != expected_bucket:
            raise ArchiveError("Foundation bucket variables must be exact staging bucket names")
    for name, value in secrets.items():
        prefix = f"arn:aws:secretsmanager:{region}:{account}:secret:deepresearch-staging/{name}-"
        if not isinstance(value, str) or not re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9]{6}", value):
            raise ArchiveError("Foundation secret variables must be staging secret ARNs, not secret values")


def _validate_inputs(root: Path, args: argparse.Namespace) -> list[tuple[str, bytes]]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,15}", args.session_id):
        raise ArchiveError("Session ID must be at most sixteen safe lowercase characters")
    if not re.fullmatch(r"[a-f0-9]{64}", args.approved_intent_sha256):
        raise ArchiveError("Approved expiry intent must be a SHA-256 hex digest")

    intent_path = _inside(root, args.intent, "Intent")
    plan_path = _inside(root, args.saved_plan, "Saved plan")
    variables_path = _inside(root, args.runtime_variables, "Runtime variables")
    backend_path = _inside(root, args.runtime_backend, "Runtime backend config")
    intent_bytes = intent_path.read_bytes()
    plan_bytes = plan_path.read_bytes()
    variables_bytes = variables_path.read_bytes()
    backend_bytes = backend_path.read_bytes()
    intent = _read_json(intent_bytes, "Expiry intent")
    variables = _read_json(variables_bytes, "Runtime variable file")
    config = intent.get("config")
    if (
        intent.get("version") != 1
        or intent.get("operation") != "expiry"
        or not isinstance(config, dict)
        or config.get("environment") != "staging"
        or config.get("session_id") != args.session_id
    ):
        raise ArchiveError("Expiry intent must target this staging session")
    if canonical_digest(intent) != args.approved_intent_sha256:
        raise ArchiveError("Expiry intent does not match the explicitly approved digest")
    if (
        intent.get("runtime_state_bucket") != args.approved_state_bucket
        or intent.get("runtime_state_key") != "deepresearch/staging/runtime.tfstate"
        or intent.get("runtime_backend_file") != "artifacts/runtime.backend.hcl"
    ):
        raise ArchiveError("Expiry intent must bind the explicitly approved staging state bucket and key")
    if intent.get("saved_plan") != "artifacts/expiry.tfplan":
        raise ArchiveError("Expiry intent must reference artifacts/expiry.tfplan")
    if intent.get("receipt") != "artifacts/expiry-backup-receipt.json":
        raise ArchiveError("Expiry intent receipt path is not the reviewed fixed path")
    if hashlib.sha256(plan_bytes).hexdigest() != intent.get("saved_plan_sha256"):
        raise ArchiveError("Saved binary plan does not match the expiry intent")
    _validate_runtime_variables(variables, config, args.session_id)

    try:
        backend_text = backend_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArchiveError("Runtime backend config must be UTF-8 text") from exc
    if hashlib.sha256(backend_bytes).hexdigest() != intent.get("runtime_backend_sha256"):
        raise ArchiveError("Runtime backend file changed after expiry intent approval")
    bucket_match = BACKEND_BUCKET.search(backend_text)
    key_match = BACKEND_KEY.search(backend_text)
    bucket = bucket_match.group("value") if bucket_match else ""
    if (
        not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
        or bucket == "replace-with-retained-terraform-state-bucket"
        or bucket != args.approved_state_bucket
    ):
        raise ArchiveError("Runtime backend must match the explicitly approved retained state bucket")
    if key_match is None or key_match.group("value") != "deepresearch/staging/runtime.tfstate":
        raise ArchiveError("Runtime backend must reference the staging runtime state key")
    if not re.search(r'^\s*region\s*=\s*"us-east-1"\s*$', backend_text, re.MULTILINE):
        raise ArchiveError("Runtime backend region must be us-east-1")
    if not re.search(r"^\s*encrypt\s*=\s*true\s*$", backend_text, re.MULTILINE):
        raise ArchiveError("Runtime backend encryption must be enabled")
    if not re.search(r"^\s*use_lockfile\s*=\s*true\s*$", backend_text, re.MULTILINE):
        raise ArchiveError("Runtime backend lockfile must be enabled")

    files = [
        (name, path.read_bytes())
        for path, name in _runtime_source_files(root)
    ]
    files.extend(
        [
            ("artifacts/expiry-intent.json", intent_bytes),
            ("artifacts/expiry.tfplan", plan_bytes),
            ("artifacts/runtime.auto.tfvars.json", variables_bytes),
            ("artifacts/runtime.backend.hcl", backend_bytes),
            ("infra/runtime/artifacts/runtime.backend.hcl", backend_bytes),
        ]
    )
    names = [name for name, _ in files]
    if len(names) != len(set(names)):
        raise ArchiveError("Archive entries must have unique fixed paths")
    return files


def build_archive(root: Path, args: argparse.Namespace) -> tuple[Path, str]:
    root = root.resolve(strict=True)
    files = _validate_inputs(root, args)
    destination = Path(args.output).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise ArchiveError("Output already exists; approved archives are never overwritten")
    _reject_symlinked_parents(destination.parent, root)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _reject_symlinked_parents(destination.parent, root)

    temporary_name = None
    linked = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b", prefix=".expiry-archive-", dir=destination.parent, delete=False
        ) as temporary:
            temporary_name = temporary.name
            os.chmod(temporary_name, 0o600)
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
                for name, data in sorted(files, key=lambda entry: entry[0]):
                    info = zipfile.ZipInfo(name, date_time=ARCHIVE_EPOCH)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFREG | 0o600) << 16
                    info.compress_type = zipfile.ZIP_STORED
                    archive.writestr(info, data)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.link(temporary_name, destination)
        linked = True
        os.chmod(destination, 0o600)
        with destination.open("rb") as stream:
            os.fsync(stream.fileno())
    except Exception:
        if linked and destination.exists():
            destination.unlink()
        raise
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)

    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    return destination, digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--approved-intent-sha256", required=True)
    parser.add_argument("--intent", required=True)
    parser.add_argument("--saved-plan", required=True)
    parser.add_argument("--runtime-variables", required=True)
    parser.add_argument("--runtime-backend", required=True)
    parser.add_argument("--approved-state-bucket", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        output, digest = build_archive(args.workspace, args)
    except (ArchiveError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps({"archive": str(output), "sha256": digest, "mode": "0600"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
