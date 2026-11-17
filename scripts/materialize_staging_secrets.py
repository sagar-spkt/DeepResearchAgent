"""Turn private staging Secrets Manager JSON values into temporary env files."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

APP_KEYS = {
    "ALLOWED_ORIGINS",
    "AWS_REGION",
    "BOOTSTRAP_OWNER_EMAIL",
    "COGNITO_CLIENT_ID",
    "COGNITO_ISSUER",
    "DATABASE_URL",
    "LANGFUSE_BASE_URL",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "S3_BUCKET",
    "SQS_QUEUE_URL",
    "TAVILY_API_KEY",
    "VLLM_API_KEY",
}
WEB_KEYS = {"APP_BASE_URL", "AUTH_SESSION_SECRET", "COGNITO_CLIENT_ID", "COGNITO_DOMAIN"}
VLLM_KEYS = {"VLLM_API_KEY"}
RDS_KEYS = {"username", "password"}
ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _read_object(path: Path, name: str) -> dict[str, str]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Cannot read {name} secret JSON") from exc
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()
    ):
        raise SystemExit(f"{name} secret must be a JSON object containing string values")
    return value


def _validate_environment(
    values: dict[str, str], required: set[str], name: str, allowed: set[str] | None = None
) -> None:
    if not required.issubset(values):
        raise SystemExit(f"{name} secret is missing required settings")
    if allowed is not None and not set(values).issubset(allowed):
        raise SystemExit(f"{name} secret contains an unexpected setting")
    if any(not ENV_KEY.fullmatch(key) for key in values):
        raise SystemExit(f"{name} secret contains an invalid environment setting")
    _validate_string_values(values, name)


def _validate_string_values(values: dict[str, str], name: str) -> None:
    if any("\n" in value or "\r" in value for value in values.values()):
        raise SystemExit(f"{name} secret contains an invalid value")


def _write_env(path: Path, values: dict[str, str]) -> None:
    path.write_text("".join(f"{key}={value}\n" for key, value in sorted(values.items())))
    path.chmod(0o600)


def materialize(
    application_path: Path,
    web_path: Path,
    vllm_path: Path,
    rds_path: Path,
    database_host: str,
    database_port: int,
    output_dir: Path,
) -> None:
    application = _read_object(application_path, "application")
    web = _read_object(web_path, "web")
    vllm = _read_object(vllm_path, "vLLM")
    rds = _read_object(rds_path, "RDS master")
    _validate_environment(
        application,
        {
            "APP_DB_PASSWORD",
            "BOOTSTRAP_OWNER_EMAIL",
            "COGNITO_CLIENT_ID",
            "COGNITO_ISSUER",
            "DATABASE_URL",
            "LANGFUSE_DB_PASSWORD",
            "S3_BUCKET",
            "SQS_QUEUE_URL",
            "VLLM_API_KEY",
        },
        "application",
    )
    _validate_environment(web, WEB_KEYS, "web", WEB_KEYS)
    _validate_environment(vllm, VLLM_KEYS, "vLLM", VLLM_KEYS)
    if not {"username", "password"}.issubset(rds):
        raise SystemExit("RDS master secret is missing required settings")
    if not set(rds).issubset(RDS_KEYS):
        raise SystemExit("RDS master secret contains an unexpected setting")
    _validate_string_values(rds, "RDS master")
    if application["VLLM_API_KEY"] != vllm["VLLM_API_KEY"]:
        raise SystemExit("Application and vLLM API keys do not match")
    if application["COGNITO_CLIENT_ID"] != web["COGNITO_CLIENT_ID"]:
        raise SystemExit("Application and web Cognito client IDs do not match")
    if not database_host or not 1 <= database_port <= 65535:
        raise SystemExit("Database endpoint is invalid")

    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(output_dir, 0o700)
    _write_env(
        output_dir / "application.env",
        {key: value for key, value in application.items() if key in APP_KEYS},
    )
    _write_env(
        output_dir / "web.env", {key: value for key, value in web.items() if key in WEB_KEYS}
    )
    _write_env(
        output_dir / "vllm.env",
        {key: value for key, value in vllm.items() if key in VLLM_KEYS},
    )
    _write_env(
        output_dir / "bootstrap.env",
        {
            "APP_DB_PASSWORD": application["APP_DB_PASSWORD"],
            "DATABASE_HOST": database_host,
            "DATABASE_PORT": str(database_port),
            "LANGFUSE_DB_PASSWORD": application["LANGFUSE_DB_PASSWORD"],
            "RDS_MASTER_PASSWORD": rds["password"],
            "RDS_MASTER_USERNAME": rds["username"],
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--application-json", type=Path, required=True)
    parser.add_argument("--web-json", type=Path, required=True)
    parser.add_argument("--vllm-json", type=Path, required=True)
    parser.add_argument("--rds-json", type=Path, required=True)
    parser.add_argument("--database-host", required=True)
    parser.add_argument("--database-port", type=int, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    materialize(
        args.application_json,
        args.web_json,
        args.vllm_json,
        args.rds_json,
        args.database_host,
        args.database_port,
        args.output_dir,
    )


if __name__ == "__main__":
    main()
