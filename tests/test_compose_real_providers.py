import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ComposeRealProvidersTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compose = json.loads((ROOT / "compose.yaml").read_text())
        cls.services = cls.compose["services"]

    def test_application_services_do_not_default_to_synthetic_providers(self):
        for name in ("migrate", "api", "worker"):
            with self.subTest(service=name):
                service = self.services[name]
                environment = service["environment"]
                self.assertEqual(service["platform"], "linux/amd64")
                self.assertEqual(environment["MODEL_PROVIDER"], "bedrock")
                self.assertTrue(environment["BEDROCK_MODEL_ID"])
                self.assertEqual(environment["EMBEDDING_PROVIDER"], "sentence-transformers")
                self.assertEqual(environment["EMBEDDING_MODEL"], "/models/bge-small-en-v1.5")
                self.assertEqual(
                    service["build"]["args"]["INSTALL_EXTRAS"], "bedrock,embeddings"
                )
                self.assertIn(
                    "./local-data/models/bge-small-en-v1.5:/models/bge-small-en-v1.5:ro",
                    service["volumes"],
                )

    def test_worker_emits_to_the_configured_langfuse_project(self):
        api = self.services["api"]["environment"]
        worker = self.services["worker"]["environment"]
        for key in ("LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
            with self.subTest(setting=key):
                self.assertEqual(worker[key], api[key])


if __name__ == "__main__":
    unittest.main()
