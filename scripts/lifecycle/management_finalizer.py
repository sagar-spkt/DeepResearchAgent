"""Exact, session-bound destroy-plan validation for external management finalization."""

from .core import Refused

ALLOWED_MANAGEMENT_RESOURCES = {
    "aws_eip.nat": "aws_eip",
    "aws_iam_instance_profile.runner[0]": "aws_iam_instance_profile",
    "aws_iam_role.runner[0]": "aws_iam_role",
    "aws_iam_role_policy.runner[0]": "aws_iam_role_policy",
    "aws_instance.runner[0]": "aws_instance",
    "aws_internet_gateway.management": "aws_internet_gateway",
    "aws_kms_key.runner_token[0]": "aws_kms_key",
    "aws_nat_gateway.management": "aws_nat_gateway",
    "aws_route.executor_egress": "aws_route",
    "aws_route.internet": "aws_route",
    "aws_route_table.executor": "aws_route_table",
    "aws_route_table.public": "aws_route_table",
    "aws_route_table_association.executor": "aws_route_table_association",
    "aws_route_table_association.public": "aws_route_table_association",
    "aws_security_group.executor": "aws_security_group",
    "aws_subnet.executor": "aws_subnet",
    "aws_subnet.public": "aws_subnet",
    "aws_vpc.management": "aws_vpc",
    "aws_vpc_endpoint.s3": "aws_vpc_endpoint",
    "aws_vpc_security_group_egress_rule.https": "aws_vpc_security_group_egress_rule",
}
UNTAGGED_MANAGEMENT_RESOURCES = {
    "aws_iam_role_policy.runner[0]",
    "aws_route.executor_egress",
    "aws_route.internet",
    "aws_route_table_association.executor",
    "aws_route_table_association.public",
}
MANAGEMENT_COMPONENTS = {
    "aws_iam_role.runner[0]": "github-actions-runner",
    "aws_instance.runner[0]": "github-actions-runner",
    "aws_kms_key.runner_token[0]": "github-runner-token-key",
}


def _session_tags(value, config, address):
    tags = value.get("tags_all")
    if not isinstance(tags, dict):
        return False
    expected = {
        "Project": "deepresearch",
        "Environment": config["environment"],
        "Session": config["session_id"],
        "ManagedBy": "terraform",
        "Component": MANAGEMENT_COMPONENTS.get(address, "management-network"),
    }
    return all(
        tags.get(key) == expected
        for key, expected in expected.items()
    )


def inspect_management_destroy(plan, config):
    """Accept deletes only for the known management root and exact session resources."""
    if config.get("environment") != "staging":
        raise Refused("Management finalization is staging-only")
    variables = plan.get("variables", {})
    for key in ("account_id", "region", "environment", "session_id"):
        if variables.get(key, {}).get("value") != config.get(key):
            raise Refused("Management destroy variable binding mismatch: " + key)

    provider_configs = plan.get("configuration", {}).get("provider_config", {})
    if set(provider_configs) != {"aws"}:
        raise Refused("Management destroy plan must use only the default AWS provider")
    provider = provider_configs["aws"]
    expressions = provider.get("expressions", {})
    if (
        provider.get("name") != "aws"
        or provider.get("full_name") != "registry.terraform.io/hashicorp/aws"
        or provider.get("alias")
        or set(expressions) - {"region", "default_tags", "allowed_account_ids"}
        or expressions.get("region") != {"references": ["var.region"]}
        or expressions.get("allowed_account_ids") != {"references": ["var.account_id"]}
    ):
        raise Refused("Management destroy provider/account/endpoint binding is invalid")

    root = plan.get("configuration", {}).get("root_module", {})
    if root.get("module_calls"):
        raise Refused("Management finalizer does not accept Terraform modules")
    declarations = {
        item["address"]: item
        for item in root.get("resources", [])
        if item.get("mode") == "managed"
    }
    for address, declaration in declarations.items():
        allowed_type = ALLOWED_MANAGEMENT_RESOURCES.get(address)
        if allowed_type is None:
            allowed_type = ALLOWED_MANAGEMENT_RESOURCES.get(address + "[0]")
        if (
            allowed_type != declaration.get("type")
            or declaration.get("provider_config_key") != "aws"
            or "provisioner" in declaration
            or "provisioners" in declaration
        ):
            raise Refused("Unapproved management resource declaration: " + address)

    changes = [item for item in plan.get("resource_changes", []) if item.get("mode") == "managed"]
    seen = set()
    before = {}
    for item in changes:
        address = item.get("address")
        kind = item.get("type")
        if address in seen or ALLOWED_MANAGEMENT_RESOURCES.get(address) != kind:
            raise Refused("Unapproved or duplicate management destroy resource: " + str(address))
        seen.add(address)
        change = item.get("change", {})
        if change.get("actions") != ["delete"] or change.get("after") is not None:
            raise Refused("Management finalizer accepts only exact deletes: " + str(address))
        current = change.get("before") or {}
        before[address] = current
        if address not in UNTAGGED_MANAGEMENT_RESOURCES and not _session_tags(
            current, config, address
        ):
            raise Refused("Management resource tags do not match the active session: " + address)

    if not seen:
        raise Refused("Management destroy plan contains no session resources")

    vpc_id = before.get("aws_vpc.management", {}).get("id")
    subnet_executor = before.get("aws_subnet.executor", {}).get("id")
    subnet_public = before.get("aws_subnet.public", {}).get("id")
    route_executor = before.get("aws_route_table.executor", {}).get("id")
    route_public = before.get("aws_route_table.public", {}).get("id")
    relationships = {
        "aws_route.executor_egress": ("route_table_id", route_executor),
        "aws_route.internet": ("route_table_id", route_public),
        "aws_route_table_association.executor": ("route_table_id", route_executor),
        "aws_route_table_association.public": ("route_table_id", route_public),
    }
    for address, (attribute, expected) in relationships.items():
        if address in before and (not expected or before[address].get(attribute) != expected):
            raise Refused("Management route is not attached to the expected session table: " + address)
    for address, subnet in (
        ("aws_route_table_association.executor", subnet_executor),
        ("aws_route_table_association.public", subnet_public),
    ):
        if address in before and (not subnet or before[address].get("subnet_id") != subnet):
            raise Refused("Management association is not attached to the expected session subnet: " + address)
    if vpc_id:
        for address in ("aws_subnet.executor", "aws_subnet.public"):
            if address in before and before[address].get("vpc_id") != vpc_id:
                raise Refused("Management subnet belongs to a different VPC: " + address)
    if "aws_iam_role_policy.runner[0]" in before:
        expected_role = before.get("aws_iam_role.runner[0]", {}).get("name")
        if not expected_role or before["aws_iam_role_policy.runner[0]"].get("role") != expected_role:
            raise Refused("Runner IAM policy is not attached to the session runner role")
    if "aws_iam_instance_profile.runner[0]" in before:
        role = before.get("aws_iam_role.runner[0]", {}).get("name")
        if before["aws_iam_instance_profile.runner[0]"].get("role") != role:
            raise Refused("Runner instance profile is not scoped to the session runner role")
    key = before.get("aws_kms_key.runner_token[0]")
    if key and key.get("deletion_window_in_days") != 7:
        raise Refused("Runner token key must use the configured seven-day deletion delay")
    return sorted(seen)
