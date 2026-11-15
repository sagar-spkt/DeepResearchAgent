"""Create local-only Langfuse bootstrap credentials without displaying secrets."""

from __future__ import annotations

import argparse
import json
import secrets
from pathlib import Path


def read_env(path: Path) -> tuple[list[str], dict[str, str]]:
    if path.is_symlink():
        raise ValueError(f"Refusing to write through symlink: {path.name}")
    lines = path.read_text().splitlines() if path.exists() else []
    values = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key and not key.startswith("#"):
            values[key] = value
    return lines, values


def update_env(path: Path, values: dict[str, str]) -> None:
    lines, _current = read_env(path)
    updates = dict(values)
    output = []
    seen = set()
    for line in lines:
        key, separator, _value = line.partition("=")
        if separator and key in updates:
            if key not in seen:
                output.append(f"{key}={updates[key]}")
                seen.add(key)
        else:
            output.append(line)
    output.extend(f"{key}={value}" for key, value in updates.items() if key not in seen)
    path.write_text("\n".join(output) + "\n")
    path.chmod(0o600)


def configure(root: Path, enable_app_tracing: bool = False) -> None:
    platform_path = root / ".env.platform"
    app_path = root / ".env"
    if not platform_path.is_file() or not app_path.is_file():
        raise FileNotFoundError("Create .env and .env.platform from their example files first")
    blob_config_dir = root / "local-data" / "langfuse"
    config_path = blob_config_dir / "seaweedfs-s3.json"
    for output_path in (root / "local-data", blob_config_dir, config_path):
        if output_path.is_symlink():
            raise ValueError(f"Refusing to write through symlink: {output_path.relative_to(root)}")

    _lines, platform = read_env(platform_path)
    generated = {
        "LOCAL_DB_PASSWORD": secrets.token_hex(32),
        "LOCAL_LANGFUSE_ENCRYPTION_KEY": secrets.token_hex(32),
        "LOCAL_LANGFUSE_AUTH_SECRET": secrets.token_hex(32),
        "LOCAL_LANGFUSE_SALT": secrets.token_hex(32),
        "LOCAL_CLICKHOUSE_PASSWORD": secrets.token_hex(32),
        "LOCAL_VALKEY_PASSWORD": secrets.token_hex(32),
        "LOCAL_SEAWEEDFS_ACCESS_KEY": "local-" + secrets.token_hex(12),
        "LOCAL_SEAWEEDFS_SECRET_KEY": secrets.token_hex(32),
        "LANGFUSE_INIT_PROJECT_PUBLIC_KEY": "pk-lf-" + secrets.token_hex(24),
        "LANGFUSE_INIT_PROJECT_SECRET_KEY": "sk-lf-" + secrets.token_hex(32),
        "LANGFUSE_INIT_USER_PASSWORD": secrets.token_hex(32),
    }
    defaults = {
        "LANGFUSE_INIT_ORG_ID": "deepresearch-local",
        "LANGFUSE_INIT_ORG_NAME": "DeepResearchLocal",
        "LANGFUSE_INIT_PROJECT_ID": "research-agent-local",
        "LANGFUSE_INIT_PROJECT_NAME": "ResearchAgentLocal",
        "LANGFUSE_INIT_USER_EMAIL": "demo@example.test",
        "LANGFUSE_INIT_USER_NAME": "LocalResearchOwner",
    }
    for key, value in defaults.items():
        if not platform.get(key) or platform[key].startswith("generated-by-"):
            platform[key] = value
    for key, value in generated.items():
        if not platform.get(key) or platform[key].startswith(("generated-by-", "replace-")):
            platform[key] = value

    required = (*defaults.keys(), *generated.keys())
    missing = [key for key in required if not platform.get(key)]
    if missing:
        raise ValueError(f"Set required values in .env.platform: {', '.join(missing)}")

    update_env(platform_path, platform)
    blob_config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    blob_config_dir.chmod(0o700)
    blob_config = {
        "identities": [
            {
                "name": "langfuse",
                "credentials": [
                    {
                        "accessKey": platform["LOCAL_SEAWEEDFS_ACCESS_KEY"],
                        "secretKey": platform["LOCAL_SEAWEEDFS_SECRET_KEY"],
                    }
                ],
                "actions": ["Admin", "Read", "Write", "List", "Tagging"],
            }
        ]
    }
    config_path.write_text(json.dumps(blob_config, indent=2) + "\n")
    config_path.chmod(0o600)
    if enable_app_tracing:
        update_env(
            app_path,
            {
                "LANGFUSE_BASE_URL": "http://localhost:3001",
                "LANGFUSE_PUBLIC_KEY": platform["LANGFUSE_INIT_PROJECT_PUBLIC_KEY"],
                "LANGFUSE_SECRET_KEY": platform["LANGFUSE_INIT_PROJECT_SECRET_KEY"],
            },
        )
        update_env(
            platform_path,
            {"LOCAL_LANGFUSE_CONTAINER_BASE_URL": "http://langfuse:3000"},
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--enable-app-tracing",
        action="store_true",
        help="write the local project keys into .env to send app traces to Langfuse",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    configure(root, enable_app_tracing=args.enable_app_tracing)
    print("Local Langfuse credentials are configured in ignored, owner-only .env files.")
    print("Langfuse signup is disabled; the initial owner email is demo@example.test.")
    if args.enable_app_tracing:
        print("Application trace ingestion is enabled; restart API and worker after setup.")


if __name__ == "__main__":
    main()
