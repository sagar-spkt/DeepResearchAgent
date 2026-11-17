"""Reviewable lifecycle artifacts. No AWS clients are created at import time."""

import hashlib
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path


class Refused(ValueError):
    pass


def now():
    return datetime.now(timezone.utc)


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def command(argv, *, capture=True):
    # Never use shell=True, string concatenation to a shell, or eval.
    return (
        subprocess.run(argv, check=True, capture_output=capture, text=True, timeout=3600).stdout
        if capture
        else subprocess.run(argv, check=True, timeout=3600).returncode
    )


def validate_config(config):
    expected = {
        "account_id",
        "region",
        "environment",
        "session_id",
        "cluster_name",
        "database_identifier",
        "document_bucket",
        "backup_bucket",
        "trace_bucket",
        "user_pool_id",
        "alert_topic_arn",
        "alert_kms_key_arn",
    }
    if not expected.issubset(config):
        raise Refused("Missing configuration: " + ", ".join(sorted(expected - config.keys())))
    if config["environment"] not in ("staging", "production"):
        raise Refused("Only staging/production accepted")
    if not re.fullmatch(r"\d{12}", config["account_id"]):
        raise Refused("Invalid AWS account ID")
    for key in ("region", "session_id", "cluster_name", "database_identifier"):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", config[key]):
            raise Refused("Invalid " + key)
    expected_name = "deepresearch-" + config["environment"]
    if config["cluster_name"] != expected_name or config["database_identifier"] != expected_name:
        raise Refused("Cluster/database must match the exact environment name")
    for key in ("document_bucket", "backup_bucket", "trace_bucket"):
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", config[key]):
            raise Refused("Invalid bucket " + key)
    for key, suffix in [
        ("document_bucket", "documents"),
        ("backup_bucket", "backups"),
        ("trace_bucket", "traces"),
    ]:
        if config[key] != f"deepresearch-{config['environment']}-{suffix}-{config['account_id']}":
            raise Refused("Bucket does not match retained foundation identity: " + key)
    if len({config[k] for k in ("document_bucket", "backup_bucket", "trace_bucket")}) != 3:
        raise Refused("Distinct retained buckets required")
    if not re.fullmatch(r"[a-z0-9-]+_[A-Za-z0-9]+", config["user_pool_id"]):
        raise Refused("Invalid Cognito pool ID")
    expected_topic = f"arn:aws:sns:{config['region']}:{config['account_id']}:deepresearch-{config['environment']}-alerts"
    if config["alert_topic_arn"] != expected_topic:
        raise Refused("Alert topic must match the exact environment foundation output")
    key_id = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    expected_key = rf"arn:aws:kms:{re.escape(config['region'])}:{config['account_id']}:key/{key_id}"
    if not re.fullmatch(expected_key, config["alert_kms_key_arn"]):
        raise Refused(
            "Alert KMS key must be a customer-managed key ARN in the selected account/region"
        )
    runner_key_arn = config.get("runner_token_kms_key_arn")
    if runner_key_arn not in (None, "") and not re.fullmatch(expected_key, runner_key_arn):
        raise Refused(
            "Runner token KMS key must be a customer-managed key ARN in the selected account/region"
        )
    return config


def backup_plan(config, instant=None):
    validate_config(config)
    instant = instant or now()
    return {
        "version": 1,
        "operation": "backup",
        "config": config,
        "created_at": instant.isoformat(),
        "expires_at": (instant + timedelta(hours=4)).isoformat(),
        "snapshot_identifier": f"{config['database_identifier']}-{config['session_id']}-backup",
        "backup_prefix": f"sessions/{config['environment']}/{config['session_id']}/",
        "mutations": [
            "scale application and Langfuse deployments to zero; queued DB work is retained",
            "create encrypted RDS snapshot and wait until available",
            "ClickHouse BACKUP to retained S3; Valkey SAVE and versioned upload",
        ],
        "retained": [
            "Cognito",
            "SNS alert topic and customer-managed KMS key",
            "S3 versions",
            "Secrets Manager",
            "ECR",
            "Terraform state",
            "EBS volumes",
        ],
    }


def approve(plan, approved_hash, instant=None):
    if digest(plan) != approved_hash:
        raise Refused("Reviewed plan hash mismatch")
    instant = instant or now()
    if timestamp(plan["expires_at"]) <= instant or timestamp(
        plan["created_at"]
    ) > instant + timedelta(minutes=2):
        raise Refused("Plan expired or from the future")
    validate_config(plan["config"])


def backup_execute(plan, approved_hash, adapter, instant=None):
    approve(plan, approved_hash, instant)
    if plan["operation"] != "backup":
        raise Refused("Not a backup plan")
    adapter.verify_scope(plan["config"])
    drain = adapter.quiesce()
    ch = adapter.backup_clickhouse(plan["backup_prefix"])
    redis = adapter.backup_valkey(plan["backup_prefix"])
    snapshot = adapter.snapshot_database(plan["snapshot_identifier"])
    inventory = adapter.inventory()
    receipt = {
        "version": 1,
        "operation": "backup-receipt",
        "config": plan["config"],
        "plan_hash": digest(plan),
        "created_at": now().isoformat(),
        "expires_at": (now() + timedelta(hours=24)).isoformat(),
        "drain": drain,
        "database_snapshot": snapshot,
        "clickhouse": ch,
        "valkey": redis,
        "object_inventory": inventory,
        "identities_retained": True,
        "restore_rehearsed": False,
    }
    adapter.verify_receipt(receipt, require_durable=False)
    receipt["durable_backup_receipt"] = adapter.publish_receipt("backup", receipt)
    adapter.verify_receipt(receipt)
    return receipt


# Exact current root runtime addresses; additions require explicit review here.
RUNTIME_ADDRESSES = {
    "aws_vpc.main": "aws_vpc",
    "aws_internet_gateway.main": "aws_internet_gateway",
    **{
        f"aws_subnet.{kind}[{index}]": "aws_subnet"
        for kind in ["public", "private"]
        for index in range(2)
    },
    "aws_subnet.gpu": "aws_subnet",
    "aws_subnet.gpu_fallback": "aws_subnet",
    "aws_route_table.public": "aws_route_table",
    **{
        f"aws_route_table_association.public[{index}]": "aws_route_table_association"
        for index in range(2)
    },
    "aws_route_table_association.gpu": "aws_route_table_association",
    "aws_route_table_association.gpu_fallback": "aws_route_table_association",
    "aws_security_group.database": "aws_security_group",
    "aws_vpc_security_group_ingress_rule.database": "aws_vpc_security_group_ingress_rule",
    "aws_db_subnet_group.database": "aws_db_subnet_group",
    "aws_db_instance.main": "aws_db_instance",
    **{
        f"aws_iam_role.{name}": "aws_iam_role"
        for name in [
            "cluster",
            "node",
            "application",
            "observability",
            "ebs_csi",
            "cluster_admin",
            "clickhouse_backup",
            "load_balancer_controller",
        ]
    },
    "aws_iam_role_policy_attachment.cluster": "aws_iam_role_policy_attachment",
    "aws_iam_role_policy_attachment.ebs_csi": "aws_iam_role_policy_attachment",
    **{
        f'aws_iam_role_policy_attachment.node["{policy}"]': "aws_iam_role_policy_attachment"
        for policy in [
            "AmazonEKSWorkerNodePolicy",
            "AmazonEC2ContainerRegistryPullOnly",
            "AmazonEKS_CNI_Policy",
        ]
    },
    **{
        f"aws_iam_role_policy.{name}": "aws_iam_role_policy"
        for name in ["application", "observability", "clickhouse_backup"]
    },
    "aws_eks_cluster.main": "aws_eks_cluster",
    "aws_cloudwatch_log_group.cluster": "aws_cloudwatch_log_group",
    "aws_eks_access_entry.administrator": "aws_eks_access_entry",
    "aws_eks_access_policy_association.administrator": "aws_eks_access_policy_association",
    "aws_eks_access_entry.github_actions[0]": "aws_eks_access_entry",
    "aws_eks_access_policy_association.github_actions[0]": "aws_eks_access_policy_association",
    "aws_iam_policy.load_balancer_controller": "aws_iam_policy",
    "aws_iam_role_policy_attachment.load_balancer_controller": "aws_iam_role_policy_attachment",
    **{f"aws_eks_node_group.{name}": "aws_eks_node_group" for name in ["cpu", "gpu"]},
    **{
        f"aws_eks_addon.{name}": "aws_eks_addon"
        for name in ["vpc_cni", "kube_proxy", "coredns", "pod_identity", "ebs_csi"]
    },
    **{
        f"aws_eks_pod_identity_association.{name}": "aws_eks_pod_identity_association"
        for name in [
            "application",
            "observability",
            "clickhouse_backup",
            "load_balancer_controller",
        ]
    },
}
UNTAGGED_TYPES = {
    "aws_route_table_association",
    "aws_iam_role_policy_attachment",
    "aws_iam_role_policy",
    "aws_eks_access_entry",
    "aws_eks_access_policy_association",
}
AWS_SOURCE = "registry.terraform.io/hashicorp/aws"
RUNTIME_VPC_CIDR = "10.60.0.0/16"
RUNTIME_SUBNET_CIDRS = {
    "aws_subnet.public[0]": "10.60.0.0/24",
    "aws_subnet.public[1]": "10.60.1.0/24",
    "aws_subnet.gpu": "10.60.2.0/24",
    "aws_subnet.gpu_fallback": "10.60.3.0/24",
    "aws_subnet.private[0]": "10.60.10.0/24",
    "aws_subnet.private[1]": "10.60.11.0/24",
}


def inspect_destroy(plan_json, config):
    validate_config(config)
    variables = plan_json.get("variables", {})
    for key in ["region", "environment", "session_id", "account_id"]:
        if variables.get(key, {}).get("value") != config[key]:
            raise Refused("Terraform variable binding mismatch: " + key)
    configuration = plan_json.get("configuration", {})
    providers = configuration.get("provider_config", {})
    if set(providers) != {"aws"}:
        raise Refused("Unexpected Terraform provider alias/source")
    provider = providers["aws"]
    expressions = provider.get("expressions", {})
    if (
        provider.get("name") != "aws"
        or provider.get("full_name") != AWS_SOURCE
        or provider.get("alias")
        or provider.get("module_address")
    ):
        raise Refused("Unexpected Terraform provider alias/source")
    if (
        set(expressions) - {"region", "default_tags", "allowed_account_ids"}
        or expressions.get("region") != {"references": ["var.region"]}
        or expressions.get("allowed_account_ids") != {"references": ["var.account_id"]}
    ):
        raise Refused(
            "AWS provider region must reference var.region exactly; credentials/endpoint overrides forbidden"
        )
    root = configuration.get("root_module", {})
    if root.get("module_calls"):
        raise Refused("Runtime modules are not approved")
    for declaration in root.get("resources", []):
        if "provisioners" in declaration or "provisioner" in declaration:
            raise Refused("Terraform provisioners are forbidden in runtime destroy configuration")
    declarations = {
        item["address"]: item for item in root.get("resources", []) if item.get("mode") == "managed"
    }
    allowed_bases = {
        re.sub(r"\[.*\]$", "", address): kind for address, kind in RUNTIME_ADDRESSES.items()
    }
    for address, declaration in declarations.items():
        if (
            allowed_bases.get(re.sub(r"\[.*\]$", "", address)) != declaration.get("type")
            or declaration.get("provider_config_key") != "aws"
        ):
            raise Refused("Unapproved runtime declaration/provider: " + address)
    changes = plan_json.get("resource_changes", [])
    managed = [item for item in changes if item.get("mode") == "managed"]
    before = {item["address"]: item.get("change", {}).get("before") or {} for item in managed}
    if len(before) != len(managed):
        raise Refused("Duplicate runtime resource address")
    name = "deepresearch-" + config["environment"]
    account = config["account_id"]
    region = config["region"]

    def require(condition, detail):
        if not condition:
            raise Refused(detail)

    def value(address, field):
        if address not in before or field not in before[address]:
            raise Refused("Missing runtime identity reference: " + address + "." + field)
        return before[address][field]

    def subnet_ids(kind):
        return [value(f"aws_subnet.{kind}[{i}]", "id") for i in range(2)]

    def check_identity(address, kind, record):
        if kind not in UNTAGGED_TYPES:
            tags = record.get("tags_all")
            require(
                isinstance(tags, dict)
                and all(
                    tags.get(k) == v
                    for k, v in {
                        "Project": "deepresearch",
                        "Environment": config["environment"],
                        "Session": config["session_id"],
                    }.items()
                ),
                "Missing/mismatched required runtime Project/Environment/Session tags: " + address,
            )
        arn = record.get("arn")
        if arn:
            bits = arn.split(":", 5)
            require(
                len(bits) == 6
                and bits[0:2] == ["arn", "aws"]
                and bits[4] == account
                and bits[3] in ["", region],
                "Resource ARN account/region mismatch: " + address,
            )
        if kind == "aws_vpc":
            require(
                record.get("owner_id") == account
                and re.fullmatch(r"vpc-[a-f0-9]{8,17}", record.get("id", "")),
                "VPC owner/id mismatch",
            )
            require(
                record.get("cidr_block") == RUNTIME_VPC_CIDR,
                "Runtime VPC CIDR mismatch",
            )
        elif kind in [
            "aws_internet_gateway",
            "aws_subnet",
            "aws_route_table",
            "aws_security_group",
        ]:
            require(
                record.get("vpc_id") == value("aws_vpc.main", "id"),
                "Cross-VPC resource: " + address,
            )
            if "owner_id" in record:
                require(record["owner_id"] == account, "Network owner mismatch")
            if kind == "aws_subnet":
                expected_cidr = RUNTIME_SUBNET_CIDRS.get(address)
                require(expected_cidr is not None, "Unexpected subnet address: " + address)
                public = address.startswith(("aws_subnet.public[", "aws_subnet.gpu"))
                require(
                    value("aws_vpc.main", "cidr_block") == RUNTIME_VPC_CIDR
                    and record.get("cidr_block") == expected_cidr
                    and record.get("map_public_ip_on_launch") is public,
                    "Subnet CIDR/public mapping mismatch: " + address,
                )
            if kind == "aws_security_group":
                require(
                    record.get("name_prefix") == name + "-db-"
                    and record.get("name", "").startswith(name + "-db-"),
                    "Unexpected database security group name",
                )
        elif kind == "aws_route_table_association":
            public_match = re.fullmatch(r"aws_route_table_association\.public\[(\d+)\]", address)
            if public_match:
                subnet_address = f"aws_subnet.public[{public_match.group(1)}]"
            else:
                association_name = address.rsplit(".", 1)[-1]
                require(
                    association_name in {"gpu", "gpu_fallback"},
                    "Unexpected route association address: " + address,
                )
                subnet_address = f"aws_subnet.{association_name}"
            require(
                record.get("subnet_id") == value(subnet_address, "id")
                and record.get("route_table_id") == value("aws_route_table.public", "id"),
                "Route association identity mismatch",
            )
        elif kind == "aws_vpc_security_group_ingress_rule":
            require(
                record.get("security_group_id") == value("aws_security_group.database", "id"),
                "Security rule target mismatch",
            )
            require(
                record.get("referenced_security_group_id")
                == value("aws_eks_cluster.main", "vpc_config")[0]["cluster_security_group_id"],
                "Security rule source mismatch",
            )
        elif kind == "aws_db_subnet_group":
            require(
                record.get("name") == name
                and sorted(record.get("subnet_ids", [])) == sorted(subnet_ids("private")),
                "DB subnet group identity mismatch",
            )
        elif kind == "aws_db_instance":
            require(
                record.get("identifier") == config["database_identifier"]
                and record.get("arn") == f"arn:aws:rds:{region}:{account}:db:{name}",
                "Database identity mismatch",
            )
            require(
                record.get("db_subnet_group_name") == name
                and record.get("vpc_security_group_ids")
                == [value("aws_security_group.database", "id")],
                "Database network binding mismatch",
            )
            require(
                record.get("final_snapshot_identifier") == f"{name}-{config['session_id']}-final"
                and record.get("skip_final_snapshot") is False,
                "Database final snapshot contract mismatch",
            )
        elif kind == "aws_iam_role":
            suffix = address.split(".")[-1].replace("_", "-")
            expected = name + "-" + suffix
            require(
                record.get("name") == expected
                and record.get("arn") == f"arn:aws:iam::{account}:role/{expected}",
                "IAM role identity mismatch",
            )
            if address == "aws_iam_role.cluster_admin":
                operator = variables.get("operator_principal_arn", {}).get("value")
                require(
                    isinstance(operator, str)
                    and re.fullmatch(
                        rf"arn:aws:iam::{account}:(user|role)/[A-Za-z0-9+=,.@_/-]+",
                        operator,
                    ),
                    "EKS operator principal must be an IAM user or role in the selected account",
                )
                try:
                    trust = json.loads(record.get("assume_role_policy", ""))
                except (TypeError, json.JSONDecodeError):
                    raise Refused("EKS administrator role trust policy is invalid") from None
                statements = trust.get("Statement", []) if isinstance(trust, dict) else []
                if isinstance(statements, dict):
                    statements = [statements]
                require(
                    isinstance(statements, list)
                    and len(statements) == 1
                    and isinstance(statements[0], dict)
                    and statements[0].get("Effect") == "Allow"
                    and statements[0].get("Action") == "sts:AssumeRole"
                    and statements[0].get("Principal") == {"AWS": operator}
                    and not statements[0].get("Condition"),
                    "EKS administrator role trust must match the configured operator exactly",
                )
        elif kind == "aws_iam_role_policy_attachment":
            if address.endswith(".cluster"):
                role = name + "-cluster"
                policy = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
            elif address.endswith(".ebs_csi"):
                role = name + "-ebs-csi"
                policy = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
            elif address.endswith(".load_balancer_controller"):
                role = name + "-load-balancer-controller"
                policy = f"arn:aws:iam::{account}:policy/{name}-load-balancer-controller"
            else:
                role = name + "-node"
                policy = "arn:aws:iam::aws:policy/" + json.loads(
                    address[address.index("[") + 1 : -1]
                )
            require(
                record.get("role") == role and record.get("policy_arn") == policy,
                "IAM attachment identity mismatch",
            )
        elif kind == "aws_iam_policy":
            expected = name + "-load-balancer-controller"
            require(
                record.get("name") == expected
                and record.get("arn") == f"arn:aws:iam::{account}:policy/{expected}",
                "Managed IAM policy identity mismatch",
            )
        elif kind == "aws_iam_role_policy":
            role = name + "-" + address.split(".")[-1].replace("_", "-")
            require(
                record.get("role") == role
                and record.get("id") == role + ":" + record.get("name", ""),
                "Inline IAM policy identity mismatch",
            )
        elif kind == "aws_eks_cluster":
            require(
                record.get("name") == name
                and record.get("arn") == f"arn:aws:eks:{region}:{account}:cluster/{name}",
                "EKS cluster identity mismatch",
            )
            require(
                sorted(record.get("vpc_config", [{}])[0].get("subnet_ids", []))
                == sorted(subnet_ids("public")),
                "EKS VPC subnet binding mismatch",
            )
        elif kind == "aws_cloudwatch_log_group":
            require(
                record.get("name") == f"/aws/eks/{name}/cluster",
                "CloudWatch log group name mismatch",
            )
        elif kind in ["aws_eks_access_entry", "aws_eks_access_policy_association"]:
            github_entry = ".github_actions[" in address
            principal = (
                f"arn:aws:iam::{account}:role/deepresearch-staging-github-oidc"
                if github_entry
                else f"arn:aws:iam::{account}:role/{name}-cluster-admin"
            )
            role = before.get("aws_iam_role.cluster_admin", {})
            require(
                (github_entry or role.get("arn") == principal)
                and record.get("principal_arn") == principal
                and record.get("cluster_name") == name,
                "EKS administrator identity mismatch",
            )
            if kind == "aws_eks_access_entry" and github_entry:
                require(record.get("type") == "STANDARD", "Unexpected GitHub EKS access-entry type")
            if kind == "aws_eks_access_policy_association":
                if github_entry:
                    scope = record.get("access_scope", [{}])[0]
                    require(
                        record.get("policy_arn")
                        == "arn:aws:eks::aws:cluster-access-policy/AmazonEKSEditPolicy"
                        and scope.get("type") == "namespace"
                        and scope.get("namespaces") == ["research"],
                        "GitHub EKS access must remain namespace-scoped edit",
                    )
                else:
                    require(
                        record.get("policy_arn")
                        == "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy",
                        "Unexpected access policy",
                    )
        elif kind == "aws_eks_node_group":
            component = address.split(".")[-1]
            node_group_name = {"cpu": "cpu", "gpu": "gpu-flex"}.get(component)
            expected_subnets = subnet_ids("public")
            if component == "gpu":
                expected_subnets += [
                    value("aws_subnet.gpu", "id"),
                    value("aws_subnet.gpu_fallback", "id"),
                ]
            require(
                record.get("cluster_name") == name
                and record.get("node_group_name") == node_group_name
                and record.get("node_role_arn") == f"arn:aws:iam::{account}:role/{name}-node",
                "Node group identity mismatch",
            )
            require(
                sorted(record.get("subnet_ids", [])) == sorted(expected_subnets),
                "Node group subnet mismatch",
            )
        elif kind == "aws_eks_addon":
            addon = {
                "vpc_cni": "vpc-cni",
                "kube_proxy": "kube-proxy",
                "coredns": "coredns",
                "pod_identity": "eks-pod-identity-agent",
                "ebs_csi": "aws-ebs-csi-driver",
            }[address.split(".")[-1]]
            require(
                record.get("cluster_name") == name and record.get("addon_name") == addon,
                "EKS addon identity mismatch",
            )
        elif kind == "aws_eks_pod_identity_association":
            suffix = address.split(".")[-1].replace("_", "-")
            controller = suffix == "load-balancer-controller"
            require(
                record.get("cluster_name") == name
                and record.get("namespace") == ("kube-system" if controller else "research")
                and record.get("service_account")
                == ("aws-load-balancer-controller" if controller else "research-" + suffix)
                and record.get("role_arn") == f"arn:aws:iam::{account}:role/{name}-{suffix}",
                "Pod Identity binding mismatch",
            )
        else:
            raise Refused("Missing explicit identity validator: " + address)

    deletes = []
    for item in managed:
        address = item["address"]
        kind = item.get("type")
        actions = item.get("change", {}).get("actions")
        base = re.sub(r"\[.*\]$", "", address)
        require(
            RUNTIME_ADDRESSES.get(address) == kind
            and base in declarations
            and item.get("provider_name") == AWS_SOURCE
            and not item.get("module_address"),
            "Not an exact approved runtime address/provider: " + address,
        )
        require(actions in [["delete"], ["no-op"]], "Only deletes/no-ops allowed: " + address)
        check_identity(address, kind, before[address])
        if actions == ["delete"]:
            deletes.append(address)
    require(bool(deletes), "No runtime deletions")
    return deletes


def prepare_shutdown(config, receipt_path, runtime_dir, var_file, output_plan, runner=command):
    validate_config(config)
    runtime_dir = Path(runtime_dir).resolve()
    if runtime_dir.name != "runtime" or runtime_dir.parent.name != "infra":
        raise Refused("Only infra/runtime may be destroyed")
    receipt_path = Path(receipt_path).resolve()
    receipt = json.loads(receipt_path.read_text())
    if receipt["config"] != config:
        raise Refused("Receipt/config mismatch")
    output_plan = Path(output_plan).resolve()
    runner(
        [
            "terraform",
            f"-chdir={runtime_dir}",
            "plan",
            "-destroy",
            f"-var-file={Path(var_file).resolve()}",
            f"-out={output_plan}",
            "-input=false",
            "-lock-timeout=60s",
        ]
    )
    plan_json = json.loads(
        runner(["terraform", f"-chdir={runtime_dir}", "show", "-json", str(output_plan)])
    )
    deletions = inspect_destroy(plan_json, config)
    instant = now()
    return {
        "version": 1,
        "operation": "shutdown",
        "config": config,
        "created_at": instant.isoformat(),
        "expires_at": (instant + timedelta(hours=1)).isoformat(),
        "terraform_directory": str(runtime_dir),
        "saved_plan": str(output_plan),
        "saved_plan_sha256": file_hash(output_plan),
        "receipt": str(receipt_path),
        "receipt_sha256": file_hash(receipt_path),
        "deletions": deletions,
        "mutations": [
            "delete research ingress and wait for its ALB removal",
            "disable RDS deletion protection",
            "apply exact reviewed saved runtime destroy plan",
        ],
        "retained": [
            "foundation resources",
            "database snapshots",
            "versioned objects",
            "EBS volumes; inspect remaining billed-resource inventory",
        ],
    }


def shutdown_execute(plan, approved_hash, adapter, runner=command):
    approve(plan, approved_hash)
    if plan["operation"] != "shutdown":
        raise Refused("Not a shutdown plan")
    if (
        file_hash(plan["saved_plan"]) != plan["saved_plan_sha256"]
        or file_hash(plan["receipt"]) != plan["receipt_sha256"]
    ):
        raise Refused("Saved artifact changed after review")
    receipt = json.loads(Path(plan["receipt"]).read_text())
    if receipt["config"] != plan["config"]:
        raise Refused("Receipt/config mismatch")
    adapter.verify_scope(plan["config"])
    adapter.verify_receipt(receipt)
    adapter.verify_quiesced()
    actual = json.loads(
        runner(
            [
                "terraform",
                f"-chdir={plan['terraform_directory']}",
                "show",
                "-json",
                plan["saved_plan"],
            ]
        )
    )
    if inspect_destroy(actual, plan["config"]) != plan["deletions"]:
        raise Refused("Destroy plan inspection mismatch")

    def recheck_destructive_boundary():
        approve(plan, approved_hash)
        if (
            file_hash(plan["saved_plan"]) != plan["saved_plan_sha256"]
            or file_hash(plan["receipt"]) != plan["receipt_sha256"]
        ):
            raise Refused("Saved artifact changed while waiting for shutdown")
        adapter.verify_receipt(receipt)
        adapter.verify_quiesced(receipt["drain"])
        # Provider checks may take minutes. Recheck approval and local artifacts
        # after them as well; an exclusive lifecycle lock is still required.
        approve(plan, approved_hash)
        if (
            file_hash(plan["saved_plan"]) != plan["saved_plan_sha256"]
            or file_hash(plan["receipt"]) != plan["receipt_sha256"]
        ):
            raise Refused("Saved artifact changed during provider verification")

    adapter.remove_ingress()
    recheck_destructive_boundary()
    adapter.disable_database_protection()
    recheck_destructive_boundary()
    runner(
        [
            "terraform",
            f"-chdir={plan['terraform_directory']}",
            "apply",
            "-input=false",
            plan["saved_plan"],
        ],
        capture=False,
    )
    result = {
        "operation": "shutdown-receipt",
        "config": plan["config"],
        "created_at": now().isoformat(),
        "plan_hash": digest(plan),
        "executed": True,
        "remaining_resources": adapter.remaining_inventory(),
    }
    if plan.get("parent_approved_intent_hash"):
        result["parent_approved_intent_hash"] = plan["parent_approved_intent_hash"]
    result["durable_shutdown_receipt"] = adapter.publish_receipt("shutdown", result)
    return result


def prepare_bootstrap_cleanup(config, runtime_dir, var_file, output_plan, runner=command):
    """Prepare a destroy plan for a failed first provision before app installation."""
    validate_config(config)
    runtime_dir = Path(runtime_dir).resolve()
    if runtime_dir.name != "runtime" or runtime_dir.parent.name != "infra":
        raise Refused("Only infra/runtime may be bootstrap-cleaned")
    output_plan = Path(output_plan).resolve()
    runner(
        [
            "terraform",
            f"-chdir={runtime_dir}",
            "plan",
            "-destroy",
            f"-var-file={Path(var_file).resolve()}",
            f"-out={output_plan}",
            "-input=false",
            "-lock-timeout=60s",
        ]
    )
    raw = json.loads(
        runner(["terraform", f"-chdir={runtime_dir}", "show", "-json", str(output_plan)])
    )
    deletions = inspect_destroy(raw, config)
    instant = now()
    return {
        "version": 1,
        "operation": "bootstrap-cleanup",
        "config": config,
        "created_at": instant.isoformat(),
        "expires_at": (instant + timedelta(minutes=30)).isoformat(),
        "terraform_directory": str(runtime_dir),
        "saved_plan": str(output_plan),
        "saved_plan_sha256": file_hash(output_plan),
        "deletions": deletions,
        "preconditions": [
            "first staging provision only; application release has not been installed",
            "research namespace must be absent",
            "runtime resources must match the exact account, environment, and session tags",
            "RDS final snapshot is retained; foundation and management network are outside this plan",
        ],
        "retained": [
            "foundation identity/data buckets/secrets/ECR/queues/KMS",
            "RDS final snapshot",
        ],
    }


def bootstrap_cleanup_execute(plan, approved_hash, adapter, runner=command):
    approve(plan, approved_hash)
    if plan.get("operation") != "bootstrap-cleanup":
        raise Refused("Not a bootstrap cleanup plan")
    if file_hash(plan["saved_plan"]) != plan["saved_plan_sha256"]:
        raise Refused("Saved cleanup plan changed after review")
    raw = json.loads(
        runner(
            [
                "terraform",
                f"-chdir={plan['terraform_directory']}",
                "show",
                "-json",
                plan["saved_plan"],
            ]
        )
    )
    if inspect_destroy(raw, plan["config"]) != plan["deletions"]:
        raise Refused("Bootstrap cleanup destroy scope changed")
    adapter.verify_bootstrap_empty()
    database_planned_for_deletion = "aws_db_instance.main" in plan["deletions"]
    adapter.disable_database_protection_if_present(database_planned_for_deletion)
    adapter.verify_bootstrap_empty()
    approve(plan, approved_hash)
    if file_hash(plan["saved_plan"]) != plan["saved_plan_sha256"]:
        raise Refused("Saved cleanup plan changed during verification")
    try:
        runner(
            [
                "terraform",
                f"-chdir={plan['terraform_directory']}",
                "apply",
                "-input=false",
                plan["saved_plan"],
            ],
            capture=False,
        )
    except Exception as apply_error:
        try:
            adapter.restore_database_protection_if_present()
        except Exception as protection_error:
            raise Refused(
                "Bootstrap cleanup failed and database deletion protection could not be restored; preserve the journal and reconcile immediately"
            ) from protection_error
        raise apply_error
    if "aws_db_instance.main" in plan["deletions"]:
        adapter.wait_bootstrap_final_snapshot()
    from .inventory import InventoryCollector

    inventory = InventoryCollector(
        plan["config"],
        adapter.clients,
        bootstrap_cleanup=True,
        bootstrap_final_required="aws_db_instance.main" in plan["deletions"],
    ).collect()
    if not inventory["inventory_complete"] or not inventory["runtime_gone"]:
        try:
            adapter.restore_database_protection_if_present()
        except Exception as protection_error:
            raise Refused(
                "Bootstrap cleanup inventory is incomplete and RDS deletion protection could not be restored"
            ) from protection_error
        raise Refused(
            "Bootstrap cleanup plan applied but provider inventory still finds runtime resources"
        )
    return {
        "operation": "bootstrap-cleanup-receipt",
        "config": plan["config"],
        "created_at": now().isoformat(),
        "plan_hash": digest(plan),
        "executed": True,
        "remaining_resources": inventory["runtime_remaining"],
        "management_resources_remain": inventory.get("management_remaining", []),
        "retained_data": True,
    }


def restore_prepare(receipt, adapter):
    adapter.verify_receipt(receipt, require_quiesced=False, allow_expired=True)
    return {
        "environment": receipt["config"]["environment"],
        "database_snapshot_identifier": receipt["database_snapshot"]["arn"],
        "clickhouse_backup_url": receipt["clickhouse"]["url"],
        "valkey_backup": receipt["valkey"],
        "snapshot_provider_verified": True,
        "database_restore_verified": False,
        "application_restore_rehearsed": False,
        "remaining_steps": [
            "terraform plan runtime with snapshot identifier; review before apply",
            "restore ClickHouse using same tested server version and named credential configuration",
            "download Valkey version and verify sha256 before restoring dump.rdb",
            "recreate Kubernetes secrets, migrate, verify identity/ownership/documents/history/traces before opening ingress",
        ],
    }


def private_json(path, value):
    import os

    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def workspace_path(workspace, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise Refused("Expiry artifact paths must be workspace-relative")
    root = Path(workspace).resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise Refused("Expiry artifact escaped workspace")
    return target


def prepare_expiry(
    config,
    workspace,
    var_file,
    saved_plan="artifacts/expiry.tfplan",
    runner=command,
    admission_start=None,
):
    """Bind the reviewed destroy intent to the original admitted session clock."""
    validate_config(config)
    if len(config["session_id"]) > 16:
        raise Refused("Expiry session ID must be at most 16 characters")
    start = (
        datetime.fromtimestamp(admission_start, timezone.utc)
        if admission_start is not None
        else now()
    )
    if start > now() + timedelta(minutes=2) or start + timedelta(hours=6) <= now():
        raise Refused("Admission start is invalid or its expiry window has elapsed")
    root = Path(workspace).resolve()
    saved = workspace_path(root, saved_plan)
    saved.parent.mkdir(parents=True, exist_ok=True)
    vars_path = workspace_path(root, var_file)
    runner(
        [
            "terraform",
            f"-chdir={root / 'infra/runtime'}",
            "plan",
            "-destroy",
            f"-var-file={vars_path}",
            f"-out={saved}",
            "-input=false",
            "-lock-timeout=60s",
        ]
    )
    raw = json.loads(
        runner(["terraform", f"-chdir={root / 'infra/runtime'}", "show", "-json", str(saved)])
    )
    instant = now()
    if start + timedelta(hours=6) <= instant:
        raise Refused("Admission start is invalid or its expiry window has elapsed")
    nested = backup_plan(config, instant)
    nested["expires_at"] = (start + timedelta(hours=6)).isoformat()
    return {
        "version": 1,
        "operation": "expiry",
        "config": config,
        "created_at": instant.isoformat(),
        "not_before": (start + timedelta(hours=4)).isoformat(),
        "expires_at": (start + timedelta(hours=6)).isoformat(),
        "backup_plan": nested,
        "saved_plan": saved_plan,
        "saved_plan_sha256": file_hash(saved),
        "deletions": inspect_destroy(raw, config),
        "receipt": "artifacts/expiry-backup-receipt.json",
        "mutations": [
            "execute nested exact approved backup plan after four hours",
            "verify backups provider-side",
            "remove ingress; wait load balancers gone",
            "disable deletion protection and apply exact saved runtime destroy plan",
        ],
        "retained": ["foundation and backups", "CSI data volumes pending audited cleanup"],
        "authorization": "Reviewing this intent authorizes the nested backup and exact binary teardown only; derived receipt hashes do not expand its resource scope.",
    }


def execute_expiry(intent, approved_hash, workspace, adapter, runner=command, instant=None):
    approve(intent, approved_hash, instant)
    if intent["operation"] != "expiry" or timestamp(intent["not_before"]) > (instant or now()):
        raise Refused("Session has not reached its approved expiry")
    if intent["backup_plan"]["config"] != intent["config"]:
        raise Refused("Nested backup scope mismatch")
    root = Path(workspace).resolve()
    saved = workspace_path(root, intent["saved_plan"])
    receipt_path = workspace_path(root, intent["receipt"])
    if receipt_path.exists():
        raise Refused("Expiry receipt already exists; reconcile previous attempt instead of replay")
    if file_hash(saved) != intent["saved_plan_sha256"]:
        raise Refused("Preapproved saved destroy plan changed")
    raw = json.loads(
        runner(["terraform", f"-chdir={root / 'infra/runtime'}", "show", "-json", str(saved)])
    )
    if inspect_destroy(raw, intent["config"]) != intent["deletions"]:
        raise Refused("Expiry destroy scope changed")
    receipt = backup_execute(intent["backup_plan"], digest(intent["backup_plan"]), adapter)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    private_json(receipt_path, receipt)
    durable_receipt = receipt["durable_backup_receipt"]
    approve(intent, approved_hash)
    instant = now()
    candidate = {
        "version": 1,
        "operation": "shutdown",
        "config": intent["config"],
        "created_at": instant.isoformat(),
        "expires_at": min(
            instant + timedelta(hours=1), timestamp(intent["expires_at"])
        ).isoformat(),
        "terraform_directory": str(root / "infra/runtime"),
        "saved_plan": str(saved),
        "saved_plan_sha256": intent["saved_plan_sha256"],
        "receipt": str(receipt_path),
        "receipt_sha256": file_hash(receipt_path),
        "deletions": intent["deletions"],
        "parent_approved_intent_hash": approved_hash,
    }
    result = shutdown_execute(candidate, digest(candidate), adapter, runner)
    result["parent_approved_intent_hash"] = approved_hash
    result["durable_backup_receipt"] = durable_receipt
    return result
