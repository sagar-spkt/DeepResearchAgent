import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("docker_sync", ROOT / "scripts/docker_sync.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DockerSyncTests(unittest.TestCase):
    def test_empty_extras_install_locked_runtime_dependencies(self):
        self.assertEqual(
            ["uv", "sync", "--locked", "--no-dev", "--group", "build"],
            module.command(""),
        )
        self.assertEqual(
            ["uv", "sync", "--locked", "--no-dev", "--no-editable"],
            module.runtime_command(""),
        )

    def test_supported_extras_are_passed_as_distinct_arguments(self):
        self.assertEqual(
            [
                "uv",
                "sync",
                "--locked",
                "--no-dev",
                "--group",
                "build",
                "--extra",
                "bedrock",
                "--extra",
                "embeddings",
            ],
            module.command("bedrock,embeddings"),
        )
        self.assertEqual(
            [
                "uv",
                "sync",
                "--locked",
                "--no-dev",
                "--no-editable",
                "--extra",
                "bedrock",
                "--extra",
                "embeddings",
            ],
            module.runtime_command("bedrock,embeddings"),
        )

    def test_unknown_or_duplicate_extras_fail_closed(self):
        for value in ("all", "bedrock,bedrock", "embeddings; echo unsafe"):
            with self.assertRaises(ValueError):
                module.command(value)
            with self.assertRaises(ValueError):
                module.runtime_command(value)


if __name__ == "__main__":
    unittest.main()
