"""Embedding artifact metadata stays pinned and available offline."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODEL_REVISION = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
sys.path.insert(0, str(ROOT / "scripts"))
from verify_embedding_model import (  # noqa: E402
    MODEL_SHA256,
    verify_model_artifacts,
)


class EmbeddingArtifactTests(unittest.TestCase):
    def test_local_downloader_caches_the_pinned_model_card(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            fake_bin = root / "bin"
            scripts.mkdir()
            fake_bin.mkdir()
            downloader = scripts / "download_embedding_model.sh"
            downloader.write_bytes(
                (ROOT / "scripts/download_embedding_model.sh").read_bytes()
            )
            fake_curl = fake_bin / "curl"
            fake_curl.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "output=\n"
                "while [ $# -gt 0 ]; do\n"
                "  if [ \"$1\" = --output ]; then shift; output=$1; fi\n"
                "  shift\n"
                "done\n"
                "printf 'synthetic model artifact\\n' > \"$output\"\n"
            )
            fake_curl.chmod(0o755)
            verifier_log = root / "verifier-args.txt"
            fake_python = fake_bin / "python3"
            fake_python.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "printf '%s\\n' \"$@\" > \"$VERIFIER_ARGS_FILE\"\n"
            )
            fake_python.chmod(0o755)
            environment = dict(os.environ)
            environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
            environment["VERIFIER_ARGS_FILE"] = str(verifier_log)

            subprocess.run(
                ["sh", str(downloader)],
                check=True,
                env=environment,
                capture_output=True,
                text=True,
            )

            model_dir = root / "local-data/models/bge-small-en-v1.5"
            self.assertTrue((model_dir / "README.md").is_file())
            self.assertGreater((model_dir / "README.md").stat().st_size, 0)
            self.assertTrue((model_dir / "model.safetensors").is_file())
            self.assertFalse((model_dir / "README.md.partial").exists())
            verifier_args = verifier_log.read_text().splitlines()
            self.assertEqual("--revision", verifier_args[-2])
            self.assertEqual(MODEL_REVISION, verifier_args[-1])

    def test_checksum_verifier_accepts_only_expected_weights_and_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            model_dir = Path(temporary)
            weights = model_dir / "model.safetensors"
            weights.write_bytes(b"synthetic model weights")
            (model_dir / "README.md").write_text("synthetic pinned model card")
            digest = hashlib.sha256(weights.read_bytes()).hexdigest()
            with mock.patch("verify_embedding_model.MODEL_SHA256", digest):
                self.assertEqual(
                    digest, verify_model_artifacts(model_dir, MODEL_REVISION)
                )
                weights.write_bytes(b"corrupted weights")
                with self.assertRaisesRegex(ValueError, "SHA-256"):
                    verify_model_artifacts(model_dir, MODEL_REVISION)
                with self.assertRaisesRegex(ValueError, "revision"):
                    verify_model_artifacts(model_dir, "0" * 40)

    def test_checksum_verifier_requires_model_card(self):
        with tempfile.TemporaryDirectory() as temporary:
            model_dir = Path(temporary)
            with self.assertRaisesRegex(FileNotFoundError, "model card"):
                verify_model_artifacts(model_dir, MODEL_REVISION)

    def test_model_weight_digest_matches_pinned_upstream_digest(self):
        self.assertEqual(
            "3c9f31665447c8911517620762200d2245a2518d6e7208acc78cd9db317e21ad",
            MODEL_SHA256,
        )

    def test_embedding_image_fetches_model_card_at_model_revision(self):
        dockerfile = (ROOT / "Dockerfile.backend").read_text()
        self.assertIn(
            "hf_hub_download('BAAI/bge-small-en-v1.5', 'README.md', revision=revision",
            dockerfile,
        )
        self.assertIn("assert os.path.getsize(path)>0", dockerfile)
        self.assertIn("verify_embedding_model.py", dockerfile)
        self.assertIn(MODEL_SHA256, (ROOT / "scripts/verify_embedding_model.py").read_text())
        self.assertIn("COPY --from=embedding-dependencies /opt/models /opt/models", dockerfile)
        self.assertEqual(
            MODEL_REVISION,
            (ROOT / "scripts/download_embedding_model.sh")
            .read_text()
            .split("revision=", 1)[1]
            .splitlines()[0],
        )

    def test_application_ci_checks_model_card_in_network_disabled_image(self):
        workflow = json.loads(
            (ROOT / ".github/workflows/application-checks.yml").read_text()
        )
        steps = workflow["jobs"]["container-security"]["steps"]
        step = next(
            item
            for item in steps
            if item.get("name")
            == "Verify the embedding model card in the offline image"
        )
        self.assertIn("docker run --rm --network none", step["run"])
        self.assertIn("deepresearch-embeddings:ci", step["run"])
        self.assertIn("/opt/models/bge-small-en-v1.5/README.md", step["run"])
        self.assertIn("p.stat().st_size > 0", step["run"])


if __name__ == "__main__":
    unittest.main()
