import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("render_k8s_secrets", ROOT / "scripts/render_k8s_secrets.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

CONFIG = {
    "app_url": "https://staging.example.test",
    "region": "us-east-1",
    "bedrock_model_id": "amazon.nova-lite-v1:0",
    "documents_bucket": "docs",
    "traces_bucket": "traces",
    "queue_url": "https://sqs.example/q",
    "cognito_issuer": "https://cognito-idp.us-east-1.amazonaws.com/pool",
    "cognito_client_id": "client",
    "cognito_domain": "https://auth.example",
    "database_host": "db.example",
    "database_port": "5432",
    "database_master_user": "admin",
    "database_master_pw": "master",
    "database_app_pw": "a/p@ss",
    "database_langfuse_pw": "lf",
    "web_session_secret": "session",
    "langfuse_nextauth": "nextauth",
    "langfuse_salt": "salt",
    "langfuse_encryption": "00" * 32,
    "langfuse_admin_pw": "admin-pw",
    "langfuse_public_key": "pk-lf-1",
    "langfuse_secret_key": "sk-lf-1",
    "clickhouse_password": "ch",
}


class RenderSecretsTest(unittest.TestCase):
    def test_renders_consistent_application_web_and_langfuse_settings(self):
        rendered = module.render(CONFIG, {"BOOTSTRAP_OWNER_EMAIL": "owner@example.test"})
        app, web, obs = rendered["application"], rendered["web"], rendered["observability"]
        self.assertEqual(
            "postgresql+psycopg://research_app:a%2Fp%40ss@db.example:5432/research?sslmode=require",
            app["DATABASE_URL"],
        )
        self.assertEqual(["https://staging.example.test"], json.loads(app["ALLOWED_ORIGINS"]))
        self.assertEqual(app["COGNITO_CLIENT_ID"], web["COGNITO_CLIENT_ID"])
        self.assertEqual(app["LANGFUSE_PUBLIC_KEY"], obs["LANGFUSE_INIT_PROJECT_PUBLIC_KEY"])
        self.assertEqual(app["LANGFUSE_SECRET_KEY"], obs["LANGFUSE_INIT_PROJECT_SECRET_KEY"])
        self.assertNotIn("TAVILY_API_KEY", app)
        self.assertNotIn("RDS_MASTER_PASSWORD", app)

    def test_owner_email_is_required(self):
        with self.assertRaises(SystemExit):
            module.render(CONFIG, {})

    def test_optional_tavily_key_is_passed_through_and_files_are_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            outputs = Path(tmp) / "tf.json"
            outputs.write_text(json.dumps({"app_config": {"value": CONFIG}}))
            env = {"BOOTSTRAP_OWNER_EMAIL": "owner@example.test", "TAVILY_API_KEY": "tvly-x"}
            with patch.dict("os.environ", env, clear=True):
                self.assertEqual(0, module.main(["x", str(outputs), f"{tmp}/out"]))
            app_env = Path(tmp, "out", "application.env")
            self.assertIn("TAVILY_API_KEY=tvly-x\n", app_env.read_text())
            self.assertEqual(0o600, app_env.stat().st_mode & 0o777)


if __name__ == "__main__":
    unittest.main()
