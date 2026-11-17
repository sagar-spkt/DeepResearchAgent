import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lifecycle.core import Refused
from lifecycle.management_finalizer import inspect_management_destroy


CONFIG = {
    "account_id": "123456789012",
    "region": "us-east-1",
    "environment": "staging",
    "session_id": "stg-20261117",
}
TAGS = {
    "Project": "deepresearch",
    "Environment": "staging",
    "Session": "stg-20261117",
    "Component": "management-network",
    "ManagedBy": "terraform",
}


def destroy_plan(address="aws_vpc.management", action=None, before=None):
    kind = {
        "aws_vpc.management": "aws_vpc",
        "aws_nat_gateway.management": "aws_nat_gateway",
        "aws_route.executor_egress": "aws_route",
        "aws_kms_key.runner_token[0]": "aws_kms_key",
        "aws_iam_role.runner[0]": "aws_iam_role",
    }[address]
    resource_address = address.split("[", 1)[0]
    current = before or {
        "id": "vpc-0123456789abcdef0",
        "tags_all": TAGS,
    }
    if before is None and address in {
        "aws_iam_role.runner[0]",
        "aws_instance.runner[0]",
    }:
        current = {**current, "tags_all": {**TAGS, "Component": "github-actions-runner"}}
    if before is None and address == "aws_kms_key.runner_token[0]":
        current = {**current, "tags_all": {**TAGS, "Component": "github-runner-token-key"}}
    return {
        "variables": {
            key: {"value": CONFIG[key]}
            for key in ("account_id", "region", "environment", "session_id")
        },
        "configuration": {
            "provider_config": {
                "aws": {
                    "name": "aws",
                    "full_name": "registry.terraform.io/hashicorp/aws",
                    "expressions": {
                        "region": {"references": ["var.region"]},
                        "allowed_account_ids": {"references": ["var.account_id"]},
                    },
                }
            },
            "root_module": {
                "module_calls": {},
                "resources": [
                    {
                        "address": resource_address,
                        "type": kind,
                        "mode": "managed",
                        "provider_config_key": "aws",
                    }
                ],
            },
        },
        "resource_changes": [
            {
                "address": address,
                "type": kind,
                "mode": "managed",
                "change": {
                    "actions": action or ["delete"],
                    "before": current,
                    "after": None if action in (None, ["delete"]) else {},
                },
            }
        ],
    }


class ManagementFinalizerPlanTests(unittest.TestCase):
    def test_accepts_exact_session_scoped_delete(self):
        self.assertEqual(
            ["aws_vpc.management"],
            inspect_management_destroy(destroy_plan(), CONFIG),
        )

    def test_rejects_create_update_replace_and_unscoped_resources(self):
        for actions in (["create"], ["update"], ["delete", "create"]):
            with self.subTest(actions=actions), self.assertRaises(Refused):
                inspect_management_destroy(destroy_plan(action=actions), CONFIG)
        unexpected = destroy_plan()
        unexpected["resource_changes"][0]["address"] = "aws_db_instance.main"
        with self.assertRaisesRegex(Refused, "Unapproved"):
            inspect_management_destroy(unexpected, CONFIG)

    def test_rejects_wrong_session_provider_or_module(self):
        wrong_session = destroy_plan()
        wrong_session["resource_changes"][0]["change"]["before"]["tags_all"] = {
            **TAGS,
            "Session": "stg-other",
        }
        with self.assertRaisesRegex(Refused, "tags"):
            inspect_management_destroy(wrong_session, CONFIG)

        wrong_provider = destroy_plan()
        wrong_provider["configuration"]["provider_config"]["aws"]["expressions"]["region"] = {
            "constant_value": "us-west-2"
        }
        with self.assertRaisesRegex(Refused, "provider/account"):
            inspect_management_destroy(wrong_provider, CONFIG)

        module = destroy_plan()
        module["configuration"]["root_module"]["module_calls"] = {"unexpected": {}}
        with self.assertRaisesRegex(Refused, "modules"):
            inspect_management_destroy(module, CONFIG)

    def test_rejects_unsafe_route_or_kms_deletion_parameters(self):
        route = destroy_plan(
            address="aws_route.executor_egress",
            before={"route_table_id": "rtb-wrong"},
        )
        with self.assertRaisesRegex(Refused, "expected session table"):
            inspect_management_destroy(route, CONFIG)

        key = destroy_plan(
            address="aws_kms_key.runner_token[0]",
            before={
                "tags_all": {**TAGS, "Component": "github-runner-token-key"},
                "deletion_window_in_days": 30,
            },
        )
        with self.assertRaisesRegex(Refused, "seven-day"):
            inspect_management_destroy(key, CONFIG)

    def test_rejects_wrong_management_component_or_managed_by_tags(self):
        for change in (
            {**TAGS, "Component": "runtime-network"},
            {**TAGS, "ManagedBy": "manual"},
        ):
            plan = destroy_plan(before={"id": "vpc-0123456789abcdef0", "tags_all": change})
            with self.subTest(tags=change), self.assertRaisesRegex(Refused, "tags"):
                inspect_management_destroy(plan, CONFIG)

        runner = destroy_plan(
            "aws_iam_role.runner[0]",
            before={
                "name": "deepresearch-staging-management-stg-20261117-runner",
                "tags_all": {**TAGS, "Component": "management-network"},
            },
        )
        with self.assertRaisesRegex(Refused, "tags"):
            inspect_management_destroy(runner, CONFIG)

    def test_rejects_unexpected_changes_even_when_only_one_resource_is_session_tagged(self):
        plan = destroy_plan()
        extra = destroy_plan("aws_nat_gateway.management")
        plan["resource_changes"].extend(extra["resource_changes"])
        plan["configuration"]["root_module"]["resources"].extend(
            extra["configuration"]["root_module"]["resources"]
        )
        plan["resource_changes"][1]["change"]["actions"] = ["delete", "update"]
        with self.assertRaisesRegex(Refused, "only exact deletes"):
            inspect_management_destroy(plan, CONFIG)


if __name__ == "__main__":
    unittest.main()
