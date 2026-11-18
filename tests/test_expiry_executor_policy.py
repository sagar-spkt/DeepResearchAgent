"""Offline scope checks for the generated expiry executor IAM policy."""

import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lifecycle.core import Refused
from lifecycle.policies import expiry_executor_policy

from test_backup_policy import CONFIG


class ExpiryExecutorPolicyTests(unittest.TestCase):
    def plan(self):
        return {"resource_changes": [
            {"address": "aws_db_instance.main", "mode": "managed", "type": "aws_db_instance",
             "change": {"before": {"arn": "arn:aws:rds:us-east-1:123456789012:db:deepresearch-staging"}}},
            {"address": "aws_eks_cluster.main", "mode": "managed", "type": "aws_eks_cluster",
             "change": {"before": {"arn": "arn:aws:eks:us-east-1:123456789012:cluster/deepresearch-staging"}}},
        ]}

    def policy(self):
        with patch("lifecycle.policies.inspect_destroy", return_value=["aws_db_instance.main", "aws_eks_cluster.main"]) as inspect:
            policy = expiry_executor_policy(CONFIG, self.plan(), "state-bucket-123456789012")
            inspect.assert_called_once_with(self.plan(), CONFIG)
        return policy

    def test_destroy_and_protection_permissions_are_resource_scoped(self):
        policy = self.policy()
        database = "arn:aws:rds:us-east-1:123456789012:db:deepresearch-staging"
        cluster = "arn:aws:eks:us-east-1:123456789012:cluster/deepresearch-staging"
        db_mutations = [s for s in policy["Statement"] if "rds:ModifyDBInstance" in s["Action"] or "rds:DeleteDBInstance" in s["Action"]]
        self.assertTrue(db_mutations)
        self.assertTrue(all(s["Resource"] == [database] for s in db_mutations))
        self.assertTrue(any("eks:DeleteCluster" in s["Action"] and s["Resource"] == [cluster] for s in policy["Statement"]))

    def test_state_access_is_limited_to_exact_state_and_lock(self):
        policy = self.policy()
        state = "arn:aws:s3:::state-bucket-123456789012/deepresearch/staging/runtime.tfstate"
        statement = next(s for s in policy["Statement"] if s["Sid"] == "MaintainExactRuntimeStateAndLock")
        self.assertEqual(set(statement["Resource"]), {state, state + ".tflock"})
        self.assertNotIn("s3:ListAllMyBuckets", str(policy))

    def test_unknown_plan_deletion_refuses_policy_generation(self):
        plan = self.plan()
        plan["resource_changes"].append({"address": "aws_unknown.main", "mode": "managed", "type": "aws_unknown", "change": {"before": {}}})
        with patch("lifecycle.policies.inspect_destroy", return_value=["aws_unknown.main"]):
            with self.assertRaisesRegex(Refused, "No expiry delete permission mapping"):
                expiry_executor_policy(CONFIG, plan, "state-bucket-123456789012")

    def test_rejects_invalid_runtime_state_bucket(self):
        with self.assertRaisesRegex(Refused, "state bucket is invalid"):
            expiry_executor_policy(CONFIG, self.plan(), "not a bucket")


if __name__ == "__main__":
    unittest.main()
