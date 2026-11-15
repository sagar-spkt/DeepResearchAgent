import json
import tempfile
import unittest
from pathlib import Path

from scripts.setup_local_langfuse import configure


def make_workspace(root: Path):
    (root / ".env").write_text("MODEL_PROVIDER=bedrock\nTAVILY_API_KEY=do-not-change\n")
    (root / ".env.platform").write_text(
        "LOCAL_DB_PASSWORD=existing-db-password\n"
        "LOCAL_LANGFUSE_ENCRYPTION_KEY=" + "a" * 64 + "\n"
    )


def read_values(path: Path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


class LocalLangfuseSetupTests(unittest.TestCase):
    def test_setup_is_private_idempotent_and_preserves_app_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_workspace(root)

            configure(root, enable_app_tracing=True)
            first_platform = read_values(root / ".env.platform")
            first_app = read_values(root / ".env")
            configure(root, enable_app_tracing=True)
            second_platform = read_values(root / ".env.platform")
            second_app = read_values(root / ".env")

            self.assertEqual(first_platform, second_platform)
            self.assertEqual(first_app, second_app)
            self.assertEqual(first_platform["LOCAL_DB_PASSWORD"], "existing-db-password")
            self.assertEqual(first_platform["LANGFUSE_INIT_USER_EMAIL"], "demo@example.test")
            self.assertTrue(first_platform["LANGFUSE_INIT_PROJECT_PUBLIC_KEY"].startswith("pk-lf-"))
            self.assertTrue(first_platform["LANGFUSE_INIT_PROJECT_SECRET_KEY"].startswith("sk-lf-"))
            self.assertEqual(len(first_platform["LOCAL_LANGFUSE_ENCRYPTION_KEY"]), 64)
            self.assertEqual(len(first_platform["LOCAL_VALKEY_PASSWORD"]), 64)
            self.assertEqual(first_app["MODEL_PROVIDER"], "bedrock")
            self.assertEqual(first_app["TAVILY_API_KEY"], "do-not-change")
            self.assertEqual(first_app["LANGFUSE_BASE_URL"], "http://localhost:3001")
            self.assertEqual(
                first_platform["LOCAL_LANGFUSE_CONTAINER_BASE_URL"], "http://langfuse:3000"
            )
            self.assertEqual(
                first_app["LANGFUSE_PUBLIC_KEY"], first_platform["LANGFUSE_INIT_PROJECT_PUBLIC_KEY"]
            )
            self.assertEqual(
                first_app["LANGFUSE_SECRET_KEY"], first_platform["LANGFUSE_INIT_PROJECT_SECRET_KEY"]
            )
            seaweed_config_path = root / "local-data/langfuse/seaweedfs-s3.json"
            seaweed_config = json.loads(seaweed_config_path.read_text())
            credentials = seaweed_config["identities"][0]["credentials"][0]
            self.assertEqual(credentials["accessKey"], first_platform["LOCAL_SEAWEEDFS_ACCESS_KEY"])
            self.assertEqual(credentials["secretKey"], first_platform["LOCAL_SEAWEEDFS_SECRET_KEY"])
            self.assertEqual(seaweed_config_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual((root / ".env").stat().st_mode & 0o777, 0o600)
            self.assertEqual((root / ".env.platform").stat().st_mode & 0o777, 0o600)

    def test_setup_refuses_symlinked_secret_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_workspace(root)
            outside = root / "outside.env"
            outside.write_text("LOCAL_DB_PASSWORD=keep\n")
            (root / ".env.platform").unlink()
            (root / ".env.platform").symlink_to(outside)

            with self.assertRaisesRegex(ValueError, "symlink"):
                configure(root)

    def test_setup_refuses_symlinked_seaweedfs_credentials_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_workspace(root)
            blob_dir = root / "local-data/langfuse"
            blob_dir.mkdir(parents=True)
            outside = root / "outside.json"
            outside.write_text("keep this file unchanged")
            (blob_dir / "seaweedfs-s3.json").symlink_to(outside)

            with self.assertRaisesRegex(ValueError, "symlink"):
                configure(root)
            self.assertEqual(outside.read_text(), "keep this file unchanged")
