"""Render Kubernetes Secret env files from `terraform output -json` (infra/aws).

Usage: render_k8s_secrets.py <terraform-output.json> <output-dir>

Reads BOOTSTRAP_OWNER_EMAIL (required), TAVILY_API_KEY (optional) and
RELEASE_ID (optional) from the environment. Writes owner-only env files:
application.env, web.env, observability.env and bootstrap.env.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import quote


def render(config: dict[str, str], env: dict[str, str]) -> dict[str, dict[str, str]]:
    owner = env.get("BOOTSTRAP_OWNER_EMAIL", "").strip()
    if not owner or "@" not in owner:
        raise SystemExit("BOOTSTRAP_OWNER_EMAIL must be set to the first owner's email")
    endpoint = f"{config['database_host']}:{config['database_port']}"
    app_url = config["app_url"]
    application = {
        "ALLOWED_ORIGINS": json.dumps([app_url]),
        "AWS_REGION": config["region"],
        "BOOTSTRAP_OWNER_EMAIL": owner,
        "COGNITO_CLIENT_ID": config["cognito_client_id"],
        "COGNITO_ISSUER": config["cognito_issuer"],
        "DATABASE_URL": (
            f"postgresql+psycopg://research_app:{quote(config['database_app_pw'], safe='')}"
            f"@{endpoint}/research?sslmode=require"
        ),
        "LANGFUSE_BASE_URL": "http://langfuse:3000",
        "LANGFUSE_PUBLIC_KEY": config["langfuse_public_key"],
        "LANGFUSE_SECRET_KEY": config["langfuse_secret_key"],
        "RELEASE_ID": env.get("RELEASE_ID", "unknown"),
        "S3_BUCKET": config["documents_bucket"],
        "SQS_QUEUE_URL": config["queue_url"],
    }
    if env.get("TAVILY_API_KEY"):
        application["TAVILY_API_KEY"] = env["TAVILY_API_KEY"]
    web = {
        "APP_BASE_URL": app_url,
        "AUTH_SESSION_SECRET": config["web_session_secret"],
        "COGNITO_CLIENT_ID": config["cognito_client_id"],
        "COGNITO_DOMAIN": config["cognito_domain"],
    }
    observability = {
        "CLICKHOUSE_USER": "langfuse",
        "CLICKHOUSE_PASSWORD": config["clickhouse_password"],
        "DATABASE_URL": (
            f"postgresql://langfuse_app:{quote(config['database_langfuse_pw'], safe='')}"
            f"@{endpoint}/langfuse?sslmode=require"
        ),
        "ENCRYPTION_KEY": config["langfuse_encryption"],
        "NEXTAUTH_SECRET": config["langfuse_nextauth"],
        # Langfuse has no public route; operators use kubectl port-forward.
        "NEXTAUTH_URL": "http://localhost:3000",
        "SALT": config["langfuse_salt"],
        "LANGFUSE_INIT_ORG_ID": "deepresearch",
        "LANGFUSE_INIT_ORG_NAME": "DeepResearch",
        "LANGFUSE_INIT_PROJECT_ID": "deepresearch",
        "LANGFUSE_INIT_PROJECT_NAME": "DeepResearch",
        "LANGFUSE_INIT_PROJECT_PUBLIC_KEY": config["langfuse_public_key"],
        "LANGFUSE_INIT_PROJECT_SECRET_KEY": config["langfuse_secret_key"],
        "LANGFUSE_INIT_USER_EMAIL": owner,
        "LANGFUSE_INIT_USER_NAME": "Owner",
        "LANGFUSE_INIT_USER_PASSWORD": config["langfuse_admin_pw"],
        "LANGFUSE_S3_EVENT_UPLOAD_BUCKET": config["traces_bucket"],
        "LANGFUSE_S3_EVENT_UPLOAD_REGION": config["region"],
    }
    bootstrap = {
        "DATABASE_HOST": config["database_host"],
        "DATABASE_PORT": config["database_port"],
        "RDS_MASTER_USERNAME": config["database_master_user"],
        "RDS_MASTER_PASSWORD": config["database_master_pw"],
        "APP_DB_PASSWORD": config["database_app_pw"],
        "LANGFUSE_DB_PASSWORD": config["database_langfuse_pw"],
    }
    rendered = {
        "application": application,
        "web": web,
        "observability": observability,
        "bootstrap": bootstrap,
    }
    for name, values in rendered.items():
        for key, value in values.items():
            if not value or "\n" in value or "\r" in value:
                raise SystemExit(f"{name} setting {key} is empty or contains a newline")
    return rendered


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    outputs = json.loads(Path(argv[1]).read_text())
    config = outputs["app_config"]["value"]
    out = Path(argv[2])
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, values in render(config, dict(os.environ)).items():
        path = out / f"{name}.env"
        path.write_text("".join(f"{k}={v}\n" for k, v in sorted(values.items())))
        path.chmod(0o600)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
