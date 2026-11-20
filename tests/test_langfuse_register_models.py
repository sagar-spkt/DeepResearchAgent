import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("register", ROOT / "scripts/langfuse_register_models.py")
register = importlib.util.module_from_spec(spec)
spec.loader.exec_module(register)
NOVA = register.MODELS[0]


class PlanTest(unittest.TestCase):
    def test_creates_missing_model(self):
        self.assertEqual(([], [NOVA]), register.plan([{"id": "m1", "modelName": "gpt-4o"}], [NOVA]))

    def test_keeps_identical_model(self):
        existing = [{"id": "c1", **NOVA}]
        self.assertEqual(([], []), register.plan(existing, [NOVA]))

    def test_replaces_custom_model_with_changed_price_and_duplicates(self):
        existing = [{"id": "old", **NOVA, "inputPrice": 1e-6}, {"id": "keep", **NOVA}, {"id": "dup", **NOVA}]
        self.assertEqual((["old", "dup"], []), register.plan(existing, [NOVA]))
        self.assertEqual((["old"], [NOVA]), register.plan(existing[:1], [NOVA]))

    def test_never_deletes_langfuse_managed_models(self):
        managed = [{"id": "lf", **NOVA, "inputPrice": 1e-6, "isLangfuseManaged": True}]
        self.assertEqual(([], [NOVA]), register.plan(managed, [NOVA]))

    def test_prices_are_per_token_and_pattern_matches_bedrock_ids(self):
        import re

        self.assertAlmostEqual(NOVA["inputPrice"] * 1_000_000, 0.06)
        self.assertAlmostEqual(NOVA["outputPrice"] * 1_000_000, 0.24)
        pattern = re.compile(NOVA["matchPattern"].replace("(?i)", ""), re.IGNORECASE)
        for model_id in ["amazon.nova-lite-v1:0", "us.amazon.nova-lite-v1:0", "AMAZON.NOVA-LITE-V1:0"]:
            self.assertTrue(pattern.match(model_id), model_id)
        self.assertIsNone(pattern.match("amazon.nova-pro-v1:0"))


if __name__ == "__main__":
    unittest.main()
