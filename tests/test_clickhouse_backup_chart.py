"""Rendered backup configuration and service-account isolation regressions."""

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("helm"), "Helm is required for chart rendering")
class ClickHouseBackupChartTests(unittest.TestCase):
    def render(self, *extra):
        image = "example.invalid/test@sha256:" + "0" * 64
        return subprocess.run(
            [
                "helm", "template", "test", str(ROOT / "helm/observability"),
                "--namespace", "research", "--set", "environment=staging",
                "--set", "sessionId=test", "--set", "webImage=" + image,
                "--set", "workerImage=" + image, "--set", "clickhouseImage=" + image,
                "--set", "valkeyImage=" + image, *extra,
            ],
            capture_output=True, text=True, check=False,
        )

    def test_only_clickhouse_receives_backup_identity_and_client_secret(self):
        result = self.render()
        self.assertEqual(0, result.returncode, result.stderr)
        documents = result.stdout.split("---")
        clickhouse = next(d for d in documents if "kind: StatefulSet" in d and "name: observability-clickhouse" in d)
        valkey = next(d for d in documents if "kind: StatefulSet" in d and "name: observability-valkey" in d)
        self.assertIn("serviceAccountName: research-clickhouse-backup", clickhouse)
        self.assertIn("secretName: \"research-clickhouse-client\"", clickhouse)
        self.assertIn("mountPath: /etc/clickhouse-client, readOnly: true", clickhouse)
        self.assertIn("items: [{ key: config.xml, path: config.xml }]", clickhouse)
        self.assertIn("AWS_EC2_METADATA_DISABLED, value: \"true\"", clickhouse)
        self.assertIn("mountPath: /etc/clickhouse-server/config.d/research-backup.xml", clickhouse)
        self.assertNotIn("research-clickhouse-backup", valkey)
        self.assertNotIn("client-config", valkey)
        for deployment in (d for d in documents if "kind: Deployment" in d):
            self.assertNotIn("serviceAccountName: research-clickhouse-backup", deployment)
            self.assertNotIn("client-config", deployment)
        self.assertIn("<use_environment_credentials>true</use_environment_credentials>", result.stdout)
        self.assertNotIn("<password>", result.stdout)

    def test_client_config_secret_cannot_be_empty_or_injected(self):
        for value in ["", "bad/name", "bad name"]:
            with self.subTest(value=value):
                self.assertNotEqual(0, self.render("--set-string", "clickhouseClientSecretName=" + value).returncode)
