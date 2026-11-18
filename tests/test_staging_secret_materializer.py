import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from scripts.materialize_staging_secrets import LANGFUSE_KEYS, materialize


class StagingSecretMaterializerTests(unittest.TestCase):
    def test_materializes_scoped_private_environment_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = {
                "DATABASE_URL": "postgresql+psycopg://research_app:secret@db:5432/research",
                "APP_DB_PASSWORD": "app-secret",
                "LANGFUSE_DB_PASSWORD": "trace@%/&secret",
                "LANGFUSE_PUBLIC_KEY": "pk-test",
                "LANGFUSE_SECRET_KEY": "sk-test",
                "LANGFUSE_BASE_URL": "http://langfuse:3000",
                "VLLM_API_KEY": "model-secret",
                "S3_BUCKET": "private-documents",
                "SQS_QUEUE_URL": "https://sqs.example.test/queue",
                "COGNITO_ISSUER": "https://cognito-idp.us-east-1.amazonaws.com/pool",
                "COGNITO_CLIENT_ID": "client-id",
                "BOOTSTRAP_OWNER_EMAIL": "owner@example.test",
                "AWS_REGION": "us-east-1",
                "UNEXPECTED_SECRET": "must-not-reach-app",
            }
            web = {
                "APP_BASE_URL": "https://staging.example.test",
                "AUTH_SESSION_SECRET": "session-secret",
                "COGNITO_CLIENT_ID": "client-id",
                "COGNITO_DOMAIN": "https://login.example.test",
            }
            vllm = {"VLLM_API_KEY": "model-secret"}
            rds = {"username": "researchadmin", "password": "master-secret"}
            sources = []
            for name, value in (
                ("application", application),
                ("web", web),
                ("vllm", vllm),
                ("rds", rds),
            ):
                path = root / f"{name}.json"
                path.write_text(json.dumps(value))
                sources.append(path)

            output = root / "private-env"
            langfuse = dict.fromkeys(LANGFUSE_KEYS, "sample")
            langfuse.update(
                CLICKHOUSE_PASSWORD="xml<&>",
                LANGFUSE_INIT_PROJECT_PUBLIC_KEY="pk-test",
                LANGFUSE_INIT_PROJECT_SECRET_KEY="sk-test",
                LANGFUSE_S3_EVENT_UPLOAD_BUCKET="deepresearch-staging-traces-088759500358",
                LANGFUSE_S3_EVENT_UPLOAD_REGION="us-east-1",
            )
            langfuse_path = root / "langfuse.json"
            langfuse_path.write_text(json.dumps(langfuse))
            materialize(*sources, "db.example.test", 5432, output, langfuse_path)
            obs = (output / "observability.env").read_text()
            self.assertIn("postgresql://langfuse_app:trace%40%25%2F%26secret@db.example.test:5432/langfuse?sslmode=require", obs)
            self.assertNotIn("master-secret", obs)
            self.assertEqual("xml<&>", ET.parse(output / "config.xml").findtext("password"))
            self.assertEqual((output / "config.xml").stat().st_mode & 0o777, 0o600)
            langfuse["LANGFUSE_INIT_PROJECT_SECRET_KEY"] = "mismatch"
            langfuse_path.write_text(json.dumps(langfuse))
            with self.assertRaisesRegex(SystemExit, "settings do not match"):
                materialize(*sources, "db.example.test", 5432, output, langfuse_path)


            app_env = (output / "application.env").read_text()
            bootstrap_env = (output / "bootstrap.env").read_text()
            self.assertIn("DATABASE_URL=postgresql+psycopg://", app_env)
            self.assertNotIn("APP_DB_PASSWORD", app_env)
            self.assertNotIn("UNEXPECTED_SECRET", app_env)
            self.assertIn("APP_DB_PASSWORD=app-secret", bootstrap_env)
            self.assertIn("RDS_MASTER_PASSWORD=master-secret", bootstrap_env)
            self.assertNotIn("RDS_MASTER_PASSWORD", app_env)
            self.assertEqual((output / "application.env").stat().st_mode & 0o777, 0o600)
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)

    def test_rejects_mismatched_vllm_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = {
                "application": {
                    "DATABASE_URL": "postgresql+psycopg://research_app:secret@db:5432/research",
                    "APP_DB_PASSWORD": "app-secret",
                    "LANGFUSE_DB_PASSWORD": "trace-secret",
                    "VLLM_API_KEY": "one",
                    "S3_BUCKET": "private-documents",
                    "SQS_QUEUE_URL": "https://sqs.example.test/queue",
                    "COGNITO_ISSUER": "https://cognito-idp.us-east-1.amazonaws.com/pool",
                    "COGNITO_CLIENT_ID": "client-id",
                    "BOOTSTRAP_OWNER_EMAIL": "owner@example.test",
                },
                "web": {
                    "APP_BASE_URL": "https://staging.example.test",
                    "AUTH_SESSION_SECRET": "session-secret",
                    "COGNITO_CLIENT_ID": "client-id",
                    "COGNITO_DOMAIN": "https://login.example.test",
                },
                "vllm": {"VLLM_API_KEY": "two"},
                "rds": {"username": "researchadmin", "password": "master-secret"},
            }
            paths = []
            for name, value in values.items():
                path = root / f"{name}.json"
                path.write_text(json.dumps(value))
                paths.append(path)
            with self.assertRaisesRegex(SystemExit, "do not match"):
                materialize(*paths, "db.example.test", 5432, root / "env")

    def test_rejects_unapproved_web_and_vllm_secret_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = {
                "application": {
                    "DATABASE_URL": "postgresql+psycopg://research_app:secret@db:5432/research",
                    "APP_DB_PASSWORD": "app-secret",
                    "LANGFUSE_DB_PASSWORD": "trace-secret",
                    "VLLM_API_KEY": "model-secret",
                    "S3_BUCKET": "private-documents",
                    "SQS_QUEUE_URL": "https://sqs.example.test/queue",
                    "COGNITO_ISSUER": "https://cognito-idp.us-east-1.amazonaws.com/pool",
                    "COGNITO_CLIENT_ID": "client-id",
                    "BOOTSTRAP_OWNER_EMAIL": "owner@example.test",
                },
                "web": {
                    "APP_BASE_URL": "https://staging.example.test",
                    "AUTH_SESSION_SECRET": "session-secret",
                    "COGNITO_CLIENT_ID": "client-id",
                    "COGNITO_DOMAIN": "https://login.example.test",
                    "UNEXPECTED_SECRET": "must-not-reach-web",
                },
                "vllm": {"VLLM_API_KEY": "model-secret"},
                "rds": {"username": "researchadmin", "password": "master-secret"},
            }
            paths = []
            for name, value in values.items():
                path = root / f"{name}.json"
                path.write_text(json.dumps(value))
                paths.append(path)
            with self.assertRaisesRegex(SystemExit, "unexpected setting"):
                materialize(*paths, "db.example.test", 5432, root / "env")

            values["web"].pop("UNEXPECTED_SECRET")
            values["vllm"]["UNEXPECTED_SECRET"] = "must-not-reach-vllm"
            paths[1].write_text(json.dumps(values["web"]))
            paths[2].write_text(json.dumps(values["vllm"]))
            with self.assertRaisesRegex(SystemExit, "unexpected setting"):
                materialize(*paths, "db.example.test", 5432, root / "env-vllm")


if __name__ == "__main__":
    unittest.main()
