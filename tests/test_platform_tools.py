import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "release_manifest", ROOT / "scripts/validate_release_manifest.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PlatformToolsTest(unittest.TestCase):
    def test_mutable_or_injected_images_rejected(self):
        for value in ["image:latest", "image@sha256:" + "a" * 64 + "; echo unsafe"]:
            with self.assertRaises(ValueError):
                module.validate(
                    {
                        "backend_image": value,
                        "web_image": value,
                        "source_commit": "a" * 40,
                        "staging_validated": True,
                    }
                )

    def test_valid_manifest(self):
        data = {
            "backend_image": "example/backend@sha256:" + "a" * 64,
            "web_image": "example/web@sha256:" + "b" * 64,
            "source_commit": "c" * 40,
            "staging_validated": True,
        }
        self.assertEqual(data, module.validate(data))

    def test_lifecycle_execute_fails_closed(self):
        for operation in ["deploy", "backup-shutdown", "restore", "purge"]:
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/platform_lifecycle.py"),
                    operation,
                    "--environment",
                    "staging",
                    "--execute",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(2, result.returncode)
            self.assertIn('"executed": false', result.stdout)


if __name__ == "__main__":
    unittest.main()
