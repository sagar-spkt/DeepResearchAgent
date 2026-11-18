"""Offline scope checks for the generated expiry executor IAM policy."""

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
            {"address": "aws_vpc_security_group_ingress_rule.database", "mode": "managed", "type": "aws_vpc_security_group_ingress_rule",
             "change": {"before": {"security_group_id": "sg-0123456789abcdef0", "id": "sgr-0123456789abcdef0"}}},
            {"address": "aws_iam_role.node", "mode": "managed", "type": "aws_iam_role",
             "change": {"before": {"arn": "arn:aws:iam::123456789012:role/deepresearch-staging-node"}}},
            {"address": "aws_iam_role_policy_attachment.node", "mode": "managed", "type": "aws_iam_role_policy_attachment",
             "change": {"before": {"role": "deepresearch-staging-node", "policy_arn": "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"}}},
            {"address": "aws_iam_policy.load_balancer_controller", "mode": "managed", "type": "aws_iam_policy",
             "change": {"before": {"arn": "arn:aws:iam::123456789012:policy/deepresearch-staging-load-balancer-controller"}}},
        ]}

    def policy(self):
        with patch("lifecycle.policies.inspect_destroy", return_value=["aws_db_instance.main", "aws_eks_cluster.main", "aws_vpc_security_group_ingress_rule.database"]) as inspect:
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
        sg = "arn:aws:ec2:us-east-1:123456789012:security-group/sg-0123456789abcdef0"
        sg_statement = next(s for s in policy["Statement"] if "ec2:RevokeSecurityGroupIngress" in s["Action"])
        self.assertEqual(sg_statement["Action"], ["ec2:RevokeSecurityGroupIngress"])
        self.assertEqual(sg_statement["Resource"], [sg])

    def test_state_body_permissions_exclude_deleting_the_state_object(self):
        policy = self.policy()
        state = "arn:aws:s3:::state-bucket-123456789012/deepresearch/staging/runtime.tfstate"
        state_access = next(s for s in policy["Statement"] if s["Sid"] == "MaintainExactRuntimeState")
        lock_access = next(s for s in policy["Statement"] if s["Sid"] == "MaintainExactRuntimeStateLock")
        self.assertEqual(state_access["Resource"], [state])
        self.assertNotIn("s3:DeleteObject", state_access["Action"])
        self.assertEqual(lock_access["Resource"], [state + ".tflock"])
        self.assertIn("s3:DeleteObject", lock_access["Action"])

    def test_iam_role_and_policy_reads_are_plan_scoped(self):
        policy = self.policy()
        role_statement = next(s for s in policy["Statement"] if s["Sid"] == "ReadRuntimeIAMRoles")
        self.assertEqual(set(role_statement["Resource"]), {
            "arn:aws:iam::123456789012:role/deepresearch-staging-node",
            "arn:aws:iam::123456789012:role/deepresearch-staging-expiry-session-1",
            "arn:aws:iam::123456789012:role/deepresearch-staging-expiry-session-1-states",
            "arn:aws:iam::123456789012:role/deepresearch-staging-expiry-session-1-scheduler",
        })
        policy_statement = next(s for s in policy["Statement"] if s["Sid"] == "ReadRuntimeIAMPolicies")
        self.assertEqual(set(policy_statement["Resource"]), {
            "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
            "arn:aws:iam::123456789012:policy/deepresearch-staging-load-balancer-controller",
        })
        wildcard = [s for s in policy["Statement"] if s["Resource"] == ["*"] and "Condition" not in s]
        self.assertEqual([s["Action"] for s in wildcard], [["sts:GetCallerIdentity"], ["iam:ListPolicies"]])

    def test_state_access_is_limited_to_exact_state_and_lock(self):
        policy = self.policy()
        state = "arn:aws:s3:::state-bucket-123456789012/deepresearch/staging/runtime.tfstate"
        statements = [s for s in policy["Statement"] if s["Sid"] in {"MaintainExactRuntimeState", "MaintainExactRuntimeStateLock"}]
        self.assertEqual({r for statement in statements for r in statement["Resource"]}, {state, state + ".tflock"})
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
