"""Offline scope checks for the generated expiry executor IAM policy."""

import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lifecycle.core import Refused, inspect_destroy
from lifecycle.policies import (
    MAX_IAM_MANAGED_POLICIES_PER_ROLE,
    MAX_IAM_MANAGED_POLICY_CHARS,
    _split_managed_policy,
    expiry_executor_policies,
)
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

    def policies(self):
        with patch("lifecycle.policies.inspect_destroy", return_value=["aws_db_instance.main", "aws_eks_cluster.main", "aws_vpc_security_group_ingress_rule.database"]) as inspect:
            policies = expiry_executor_policies(CONFIG, self.plan(), "state-bucket-123456789012")
            inspect.assert_called_once_with(self.plan(), CONFIG)
        return policies

    def statements(self):
        return [statement for policy in self.policies() for statement in policy["Statement"]]

    def test_split_documents_fit_aws_managed_policy_and_attachment_limits(self):
        policies = self.policies()
        self.assertGreater(len(policies), 1)
        self.assertLessEqual(len(policies), MAX_IAM_MANAGED_POLICIES_PER_ROLE)
        for policy in policies:
            document = json.dumps(policy, sort_keys=True, separators=(",", ":"))
            self.assertLessEqual(len(document), MAX_IAM_MANAGED_POLICY_CHARS)
        self.assertLessEqual(sum(len(json.dumps(p, sort_keys=True, separators=(",", ":"))) for p in policies),
                             MAX_IAM_MANAGED_POLICY_CHARS * MAX_IAM_MANAGED_POLICIES_PER_ROLE)

    def test_split_refuses_more_than_ten_managed_policies(self):
        statements = [
            {"Sid": f"Statement{index}", "Effect": "Allow", "Action": "sts:GetCallerIdentity",
             "Resource": "*", "Condition": {"StringEquals": {"example:key": "x" * 5800}}}
            for index in range(11)
        ]
        with self.assertRaisesRegex(Refused, "attached managed-policy count limit"):
            _split_managed_policy(statements)

    def test_destroy_and_protection_permissions_are_resource_scoped(self):
        statements = self.statements()
        database = "arn:aws:rds:us-east-1:123456789012:db:deepresearch-staging"
        cluster = "arn:aws:eks:us-east-1:123456789012:cluster/deepresearch-staging"
        db_mutations = [s for s in statements if "rds:ModifyDBInstance" in s["Action"] or "rds:DeleteDBInstance" in s["Action"]]
        self.assertTrue(db_mutations)
        self.assertTrue(all(s["Resource"] == [database] for s in db_mutations))
        self.assertTrue(any("eks:DeleteCluster" in s["Action"] and s["Resource"] == [cluster] for s in statements))
        sg = "arn:aws:ec2:us-east-1:123456789012:security-group/sg-0123456789abcdef0"
        sg_statement = next(s for s in statements if "ec2:RevokeSecurityGroupIngress" in s["Action"])
        self.assertEqual(sg_statement["Action"], ["ec2:RevokeSecurityGroupIngress"])
        self.assertEqual(sg_statement["Resource"], [sg])

    def test_state_body_permissions_exclude_deleting_the_state_object(self):
        statements = self.statements()
        state = "arn:aws:s3:::state-bucket-123456789012/deepresearch/staging/runtime.tfstate"
        state_access = next(s for s in statements if s["Sid"] == "MaintainExactRuntimeState")
        lock_access = next(s for s in statements if s["Sid"] == "MaintainExactRuntimeStateLock")
        self.assertEqual(state_access["Resource"], [state])
        self.assertNotIn("s3:DeleteObject", state_access["Action"])
        self.assertEqual(lock_access["Resource"], [state + ".tflock"])
        self.assertIn("s3:DeleteObject", lock_access["Action"])

    def test_iam_role_and_policy_reads_are_plan_scoped(self):
        statements = self.statements()
        role_statement = next(s for s in statements if s["Sid"] == "ReadRuntimeIAMRoles")
        self.assertEqual(set(role_statement["Resource"]), {
            "arn:aws:iam::123456789012:role/deepresearch-staging-node",
            "arn:aws:iam::123456789012:role/deepresearch-staging-expiry-session-1",
            "arn:aws:iam::123456789012:role/deepresearch-staging-expiry-session-1-states",
            "arn:aws:iam::123456789012:role/deepresearch-staging-expiry-session-1-scheduler",
        })
        policy_statement = next(s for s in statements if s["Sid"] == "ReadRuntimeIAMPolicies")
        self.assertEqual(set(policy_statement["Resource"]), {
            "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
            "arn:aws:iam::123456789012:policy/deepresearch-staging-load-balancer-controller",
        })
        wildcard = [s for s in statements if s["Resource"] == ["*"] and "Condition" not in s]
        self.assertEqual([s["Action"] for s in wildcard], [["sts:GetCallerIdentity"], ["iam:ListPolicies"]])

    def test_state_access_is_limited_to_exact_state_and_lock(self):
        policies = self.policies()
        statements = [statement for policy in policies for statement in policy["Statement"]]
        state = "arn:aws:s3:::state-bucket-123456789012/deepresearch/staging/runtime.tfstate"
        state_statements = [s for s in statements if s["Sid"] in {"MaintainExactRuntimeState", "MaintainExactRuntimeStateLock"}]
        self.assertEqual({r for statement in state_statements for r in statement["Resource"]}, {state, state + ".tflock"})
        self.assertNotIn("s3:ListAllMyBuckets", str(policies))

    def test_unknown_plan_deletion_refuses_policy_generation(self):
        plan = self.plan()
        plan["resource_changes"].append({"address": "aws_unknown.main", "mode": "managed", "type": "aws_unknown", "change": {"before": {}}})
        with patch("lifecycle.policies.inspect_destroy", return_value=["aws_unknown.main"]):
            with self.assertRaisesRegex(Refused, "No expiry delete permission mapping"):
                expiry_executor_policies(CONFIG, plan, "state-bucket-123456789012")

    def test_rejects_invalid_runtime_state_bucket(self):
        with self.assertRaisesRegex(Refused, "state bucket is invalid"):
            expiry_executor_policies(CONFIG, self.plan(), "not a bucket")

    @unittest.skipUnless(
        all(os.environ.get(name) for name in (
            "EXPIRY_REAL_RUNTIME_PLAN_JSON", "EXPIRY_REAL_INTENT_JSON", "EXPIRY_REAL_RUNTIME_BACKEND_HCL",
        )),
        "set EXPIRY_REAL_* paths to measure the current private staging plan",
    )
    def test_current_real_runtime_plan_fits_managed_policy_limits(self):
        plan_path = Path(os.environ["EXPIRY_REAL_RUNTIME_PLAN_JSON"])
        intent_path = Path(os.environ["EXPIRY_REAL_INTENT_JSON"])
        backend_path = Path(os.environ["EXPIRY_REAL_RUNTIME_BACKEND_HCL"])
        plan = json.loads(plan_path.read_text())
        intent = json.loads(intent_path.read_text())
        match = re.search(r'^\s*bucket\s*=\s*"([^"]+)"\s*$', backend_path.read_text(), re.M)
        self.assertIsNotNone(match)
        config = {**intent["config"], "github_runner_enabled": False}
        self.assertEqual(inspect_destroy(plan, config), intent["deletions"])
        policies = expiry_executor_policies(config, plan, match.group(1))
        lengths = [len(json.dumps(policy, sort_keys=True, separators=(",", ":"))) for policy in policies]
        self.assertTrue(lengths)
        self.assertLessEqual(len(policies), MAX_IAM_MANAGED_POLICIES_PER_ROLE)
        self.assertTrue(all(length <= MAX_IAM_MANAGED_POLICY_CHARS for length in lengths))


if __name__ == "__main__":
    unittest.main()
