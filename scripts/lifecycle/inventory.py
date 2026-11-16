"""Read-only, fail-closed project teardown evidence and conditional admission release.

An inventory is a provider observation of the enumerated project scope, not a claim
that an entire AWS account has no charges. No resource is deleted by this module.
"""

import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone

from .core import Refused, approve, canonical, digest, validate_config

CLIENTS = (
    "sts",
    "ec2",
    "eks",
    "rds",
    "elbv2",
    "autoscaling",
    "logs",
    "s3",
    "cognito-idp",
    "secretsmanager",
    "ecr",
    "sqs",
    "iam",
    "codebuild",
    "stepfunctions",
    "scheduler",
    "dynamodb",
    "cloudwatch",
    "sns",
    "kms",
)


def tags(value):
    if isinstance(value, dict):
        return value
    return {
        item.get("Key", item.get("TagKey")): item.get("Value", item.get("TagValue"))
        for item in value or []
    }


def error_code(exc):
    return getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)


class InventoryCollector:
    def __init__(self, config, clients, clock=time.time):
        self.config = validate_config(config)
        self.clients, self.clock = clients, clock
        self.errors, self.runtime, self.retained = [], [], []
        self.name = "deepresearch-" + config["environment"]
        self.cluster = config["cluster_name"]
        self.account, self.region = config["account_id"], config["region"]
        self.vpcs, self.subnets, self.instances, self.interfaces = set(), set(), set(), set()
        self.control_verified = False

    def call(self, label, function, absent=()):
        try:
            result = function()
            if isinstance(result, dict) and not result:
                raise Refused("Empty provider response")
            return result
        except Exception as exc:
            code = error_code(exc)
            if code in absent:
                return None
            self.errors.append({"check": label, "error": code})
            return None

    def require(self, condition, label):
        if not condition:
            self.errors.append({"check": label, "error": "IdentityOrRetentionMismatch"})
        return condition

    def pages(self, service, operation, key, **parameters):
        def read():
            result = []
            for page in self.clients[service].get_paginator(operation).paginate(**parameters):
                result.extend(page[key])
            return result

        return self.call(service + ":" + operation, read) or []

    def arn(self, value, service, resource=None):
        prefix = (
            f"arn:aws:{service}:{'' if service in {'iam', 's3'} else self.region}:{self.account}:"
        )
        return (
            isinstance(value, str)
            and value.startswith(prefix)
            and (resource is None or value == prefix + resource)
        )

    def matches(self, record_tags, label, cluster_related=False):
        value = tags(record_tags)
        ours = value.get("Project") == "deepresearch"
        if cluster_related:
            ours = True
        if not ours:
            return False
        expected = {
            "Project": "deepresearch",
            "Environment": self.config["environment"],
            "Session": self.config["session_id"],
        }
        if not self.require(all(value.get(k) == v for k, v in expected.items()), label):
            return False
        return True

    def cluster_related(self, value):
        value = tags(value)
        return (
            any(
                value.get(k) == self.cluster
                for k in ("eks:cluster-name", "aws:eks:cluster-name", "elbv2.k8s.aws/cluster")
            )
            or f"kubernetes.io/cluster/{self.cluster}" in value
        )

    def record(self, target, kind, identifier, **details):
        target.append({"kind": kind, "id": identifier, **details})

    def collect(self):
        started = int(self.clock())
        identity = self.call("sts:identity", lambda: self.clients["sts"].get_caller_identity())
        if not identity or not self.require(identity.get("Account") == self.account, "sts:account"):
            return self.result(started)
        for service in CLIENTS:
            client = self.clients.get(service)
            self.require(client is not None, "client:" + service)
            if client is not None and service not in {"iam", "s3", "sts"}:
                self.require(
                    getattr(getattr(client, "meta", None), "region_name", None) == self.region,
                    "client-region:" + service,
                )
        if self.errors:
            return self.result(started)
        # Each stage is independently fail-closed; access denial never means absence.
        for stage in (
            self.control_retention,
            self.network,
            self.compute,
            self.database,
            self.load_balancers,
            self.other_runtime,
            self.foundation,
            self.expiry,
        ):
            self.call("stage:" + stage.__name__, stage)
        return self.result(started)

    def result(self, started):
        complete = not self.errors
        return {
            "version": 1,
            "operation": "teardown-inventory",
            "config": self.config,
            "collected_at_epoch": int(self.clock()),
            "started_at_epoch": started,
            "inventory_complete": complete,
            "runtime_gone": complete and not self.runtime,
            "retained_accounted": complete,
            "runtime_remaining": self.runtime,
            "retained_resources": self.retained,
            "errors": self.errors,
            "scope": "Enumerated deepresearch runtime/foundation/expiry resources in the selected account and region; not an account-wide zero-billing assertion",
            "charges_may_continue": True,
        }

    def network(self):
        ec2 = self.clients["ec2"]
        for vpc in self.pages(
            "ec2", "describe_vpcs", "Vpcs", Filters=[{"Name": "owner-id", "Values": [self.account]}]
        ):
            value = tags(vpc.get("Tags"))
            if value.get("Project") == "deepresearch":
                if self.matches(value, "vpc:" + vpc["VpcId"]):
                    self.require(vpc.get("OwnerId") == self.account, "vpc-owner")
                    self.vpcs.add(vpc["VpcId"])
                    self.record(self.runtime, "vpc", vpc["VpcId"], state=vpc.get("State"))
        for subnet in self.pages("ec2", "describe_subnets", "Subnets"):
            value = tags(subnet.get("Tags"))
            related = subnet.get("VpcId") in self.vpcs
            if related or value.get("Project") == "deepresearch":
                if self.matches(value, "subnet:" + subnet["SubnetId"], related):
                    self.require(subnet.get("OwnerId") == self.account, "subnet-owner")
                    self.subnets.add(subnet["SubnetId"])
                    self.record(self.runtime, "subnet", subnet["SubnetId"])
        for eni in self.pages("ec2", "describe_network_interfaces", "NetworkInterfaces"):
            value = tags(eni.get("TagSet"))
            related = eni.get("VpcId") in self.vpcs or self.cluster_related(value)
            if related or value.get("Project") == "deepresearch":
                self.require(eni.get("OwnerId") == self.account, "eni-owner")
                if value.get("Project") == "deepresearch" and not self.matches(value, "eni-tags"):
                    continue
                self.interfaces.add(eni["NetworkInterfaceId"])
                self.record(
                    self.runtime,
                    "network-interface",
                    eni["NetworkInterfaceId"],
                    state=eni.get("Status"),
                )
        for nat in self.pages("ec2", "describe_nat_gateways", "NatGateways"):
            value = tags(nat.get("Tags"))
            if nat.get("VpcId") in self.vpcs or value.get("Project") == "deepresearch":
                if value.get("Project") == "deepresearch":
                    self.matches(value, "nat-tags")
                if nat.get("State") != "deleted":
                    self.record(
                        self.runtime, "nat-gateway", nat["NatGatewayId"], state=nat.get("State")
                    )
        # This operation is not pageable in EC2. Enumerate all addresses explicitly.
        addresses = self.call("ec2:describe_addresses", lambda: ec2.describe_addresses())
        for address in (addresses or {}).get("Addresses", []):
            value = tags(address.get("Tags"))
            related = address.get("NetworkInterfaceId") in self.interfaces
            if related or value.get("Project") == "deepresearch":
                if value.get("Project") == "deepresearch":
                    self.matches(value, "eip-tags")
                self.record(self.runtime, "elastic-ip", address.get("AllocationId", "unknown"))
            elif not address.get("AssociationId"):
                self.errors.append(
                    {
                        "check": "elastic-ip:" + address.get("AllocationId", "unknown"),
                        "error": "UnscopedUnassociatedAddress",
                    }
                )

    def compute(self):
        cluster = self.call(
            "eks:describe_cluster",
            lambda: self.clients["eks"].describe_cluster(name=self.cluster),
            ("ResourceNotFoundException",),
        )
        if cluster is not None:
            value = cluster["cluster"]
            self.require(self.arn(value.get("arn"), "eks", "cluster/" + self.cluster), "eks-arn")
            self.matches(value.get("tags"), "eks-tags", True)
            self.record(self.runtime, "eks-cluster", self.cluster, state=value.get("status"))
        for candidate in self.pages("eks", "list_clusters", "clusters"):
            if candidate == self.cluster:
                continue
            result = self.call(
                "eks:describe_other_cluster",
                lambda candidate=candidate: self.clients["eks"].describe_cluster(name=candidate),
            )
            if result is not None:
                value = result["cluster"]
                if tags(value.get("tags")).get("Project") == "deepresearch" or candidate in {
                    "deepresearch-staging",
                    "deepresearch-production",
                }:
                    self.require(
                        self.arn(value.get("arn"), "eks", "cluster/" + candidate), "other-eks-arn"
                    )
                    self.errors.append(
                        {"check": "eks:" + candidate, "error": "OtherProjectRuntimePresent"}
                    )
                    self.record(self.runtime, "eks-cluster", candidate, state=value.get("status"))
        for reservation in self.pages("ec2", "describe_instances", "Reservations"):
            for instance in reservation["Instances"]:
                value = tags(instance.get("Tags"))
                related = instance.get("VpcId") in self.vpcs or self.cluster_related(value)
                if related or value.get("Project") == "deepresearch":
                    if value.get("Project") == "deepresearch":
                        self.matches(value, "instance-tags")
                    self.instances.add(instance["InstanceId"])
                    if instance["State"]["Name"] != "terminated":
                        self.record(
                            self.runtime,
                            "ec2-instance",
                            instance["InstanceId"],
                            state=instance["State"]["Name"],
                        )
        for group in self.pages("autoscaling", "describe_auto_scaling_groups", "AutoScalingGroups"):
            value = tags(group.get("Tags"))
            related = self.cluster_related(value) or bool(
                set(group.get("VPCZoneIdentifier", "").split(",")) & self.subnets
            )
            if related or value.get("Project") == "deepresearch":
                if value.get("Project") == "deepresearch":
                    self.matches(value, "autoscaling-tags")
                self.record(self.runtime, "autoscaling-group", group["AutoScalingGroupName"])
        for volume in self.pages("ec2", "describe_volumes", "Volumes"):
            value = tags(volume.get("Tags"))
            related = self.cluster_related(value) or any(
                a.get("InstanceId") in self.instances for a in volume.get("Attachments", [])
            )
            if self.retained_volume(volume, value):
                continue
            if value.get("Project") == "deepresearch" or related:
                if self.matches(value, "volume:" + volume["VolumeId"], related):
                    self.record(
                        self.runtime, "ebs-volume", volume["VolumeId"], state=volume.get("State")
                    )
            elif value.get("ebs.csi.aws.com/cluster") == "true":
                self.errors.append(
                    {"check": "volume:" + volume["VolumeId"], "error": "UnscopedCSIVolume"}
                )

    def control_retention(self):
        client = self.clients["dynamodb"]
        expected = f"arn:aws:dynamodb:{self.region}:{self.account}:table/deepresearch-control"
        table = self.call(
            "control:table", lambda: client.describe_table(TableName="deepresearch-control")
        )
        value = (table or {}).get("Table", {})
        valid = self.require(
            value.get("TableArn") == expected and value.get("TableStatus") == "ACTIVE",
            "control-table-identity",
        )
        result = self.call(
            "control:tags", lambda: client.list_tags_of_resource(ResourceArn=expected)
        )
        values = tags((result or {}).get("Tags"))
        valid = (
            self.require(
                values.get("Project") == "deepresearch" and values.get("Component") == "control",
                "control-table-tags",
            )
            and valid
        )
        self.control_verified = valid
        self.record(self.retained, "control-table", expected, billed=True)

    def retained_volume(self, volume, value):
        intentional = (
            value.get("Project") == "deepresearch"
            and value.get("Environment") in {"staging", "production"}
            and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", value.get("Session", ""))
            and value.get("Retention") == "data"
            and value.get("Component") in {"clickhouse", "valkey", "observability"}
            and volume.get("State") == "available"
            and volume.get("Attachments") == []
        )
        if not intentional:
            return False
        if (value["Environment"], value["Session"]) != (
            self.config["environment"],
            self.config["session_id"],
        ):
            key = "completion#" + value["Environment"] + "#" + value["Session"]
            result = self.call(
                "control:historical-retention",
                lambda: self.clients["dynamodb"].get_item(
                    TableName="deepresearch-control", Key={"key": {"S": key}}, ConsistentRead=True
                ),
            )
            encoded = (result or {}).get("Item", {}).get("receipt_json", {}).get("S", "{}")
            receipt = self.call("control:historical-receipt", lambda: json.loads(encoded)) or {}
            config = receipt.get("config", {})
            verified = (
                self.control_verified
                and receipt.get("operation") == "session-completion"
                and receipt.get("time_refunded") is False
                and all(
                    config.get(k) == v
                    for k, v in {
                        "account_id": self.account,
                        "environment": value["Environment"],
                        "session_id": value["Session"],
                    }.items()
                )
                and any(
                    row.get("kind") == "ebs-data-volume" and row.get("id") == volume["VolumeId"]
                    for row in receipt.get("retained_resources", [])
                )
            )
            if not self.require(verified, "historical-retained-volume:" + volume["VolumeId"]):
                return True  # Scope uncertainty blocks release through errors.
        self.record(
            self.retained,
            "ebs-data-volume",
            volume["VolumeId"],
            billed=True,
            component=value["Component"],
            environment=value["Environment"],
            session_id=value["Session"],
        )
        return True

    def database(self):
        rds = self.clients["rds"]
        result = self.call(
            "rds:describe_db_instances",
            lambda: rds.describe_db_instances(
                DBInstanceIdentifier=self.config["database_identifier"]
            ),
            ("DBInstanceNotFound", "DBInstanceNotFoundFault"),
        )
        if result is not None:
            database = result["DBInstances"][0]
            self.require(
                self.arn(database.get("DBInstanceArn"), "rds", "db:" + self.name), "rds-arn"
            )
            value = self.call(
                "rds:database_tags",
                lambda: rds.list_tags_for_resource(ResourceName=database["DBInstanceArn"]),
            )
            self.matches((value or {}).get("TagList"), "rds-tags", True)
            self.record(
                self.runtime, "rds-instance", self.name, state=database.get("DBInstanceStatus")
            )
        for other in self.pages("rds", "describe_db_instances", "DBInstances"):
            if other.get("DBInstanceIdentifier") == self.name:
                continue
            value = self.call(
                "rds:other_database_tags",
                lambda other=other: rds.list_tags_for_resource(ResourceName=other["DBInstanceArn"]),
            )
            values = tags((value or {}).get("TagList"))
            if values.get("Project") == "deepresearch" or other.get("DBInstanceIdentifier") in {
                "deepresearch-staging",
                "deepresearch-production",
            }:
                self.require(
                    self.arn(
                        other.get("DBInstanceArn"), "rds", "db:" + other["DBInstanceIdentifier"]
                    ),
                    "other-rds-arn",
                )
                self.errors.append(
                    {
                        "check": "rds:" + other["DBInstanceIdentifier"],
                        "error": "OtherProjectRuntimePresent",
                    }
                )
                self.record(
                    self.runtime,
                    "rds-instance",
                    other["DBInstanceIdentifier"],
                    state=other.get("DBInstanceStatus"),
                )
        expected = {
            f"{self.name}-{self.config['session_id']}-{suffix}" for suffix in ("backup", "final")
        }
        observed = set()
        for snapshot in self.pages(
            "rds",
            "describe_db_snapshots",
            "DBSnapshots",
            DBInstanceIdentifier=self.config["database_identifier"],
            IncludeShared=False,
            IncludePublic=False,
        ):
            if snapshot["DBSnapshotIdentifier"] not in expected:
                continue
            self.require(
                self.arn(
                    snapshot.get("DBSnapshotArn"),
                    "rds",
                    "snapshot:" + snapshot["DBSnapshotIdentifier"],
                ),
                "snapshot-arn",
            )
            self.require(
                snapshot.get("DBInstanceIdentifier") == self.name
                and snapshot.get("Status") == "available"
                and snapshot.get("Encrypted") is True,
                "snapshot-state",
            )
            observed.add(snapshot["DBSnapshotIdentifier"])
            self.record(self.retained, "rds-snapshot", snapshot["DBSnapshotArn"], billed=True)
        self.require(observed == expected, "required-backup-and-final-snapshots")

    def load_balancers(self):
        client = self.clients["elbv2"]
        for lb in self.pages("elbv2", "describe_load_balancers", "LoadBalancers"):
            value = self.call(
                "elbv2:tags",
                lambda lb=lb: client.describe_tags(ResourceArns=[lb["LoadBalancerArn"]]),
            )
            if value is None:
                continue
            values = tags(value["TagDescriptions"][0].get("Tags"))
            related = (
                values.get("elbv2.k8s.aws/cluster") == self.cluster or lb.get("VpcId") in self.vpcs
            )
            if related or values.get("Project") == "deepresearch":
                self.require(
                    self.arn(lb.get("LoadBalancerArn"), "elasticloadbalancing"), "load-balancer-arn"
                )
                if values.get("Project") == "deepresearch":
                    self.matches(values, "load-balancer-tags")
                self.record(self.runtime, "load-balancer", lb["LoadBalancerArn"])

    def other_runtime(self):
        for suffix in ("cluster", "node", "application", "observability", "ebs-csi"):
            result = self.call(
                "iam:get_role:" + suffix,
                lambda suffix=suffix: self.clients["iam"].get_role(
                    RoleName=self.name + "-" + suffix
                ),
                ("NoSuchEntity", "NoSuchEntityException"),
            )
            if result is not None:
                role = result["Role"]
                self.require(
                    self.arn(role.get("Arn"), "iam", "role/" + self.name + "-" + suffix),
                    "iam-role-arn",
                )
                self.matches(role.get("Tags"), "iam-role-tags", True)
                self.record(self.runtime, "iam-role", role["RoleName"])
        for group in self.pages(
            "logs",
            "describe_log_groups",
            "logGroups",
            logGroupNamePrefix=f"/aws/eks/{self.name}/cluster",
        ):
            if group["logGroupName"] == f"/aws/eks/{self.name}/cluster":
                self.require(self.arn(group.get("arn"), "logs"), "logs-arn")
                self.record(self.runtime, "cloudwatch-log-group", group["logGroupName"])

    def foundation(self):
        s3 = self.clients["s3"]
        for key in ("document_bucket", "backup_bucket", "trace_bucket"):
            bucket = self.config[key]
            params = {"Bucket": bucket, "ExpectedBucketOwner": self.account}
            value = self.call("s3:tags:" + key, lambda: s3.get_bucket_tagging(**params))
            values = tags((value or {}).get("TagSet"))
            self.require(
                values.get("Project") == "deepresearch"
                and values.get("Environment") == self.config["environment"],
                "retained-bucket-tags:" + key,
            )
            versioning = self.call(
                "s3:versioning:" + key, lambda: s3.get_bucket_versioning(**params)
            )
            self.require(
                (versioning or {}).get("Status") == "Enabled", "retained-bucket-versioning:" + key
            )
            encryption = self.call(
                "s3:encryption:" + key, lambda: s3.get_bucket_encryption(**params)
            )
            self.require(
                bool((encryption or {}).get("ServerSideEncryptionConfiguration", {}).get("Rules")),
                "retained-bucket-encryption:" + key,
            )
            public = self.call(
                "s3:public-block:" + key, lambda: s3.get_public_access_block(**params)
            )
            self.require(
                all(
                    (public or {}).get("PublicAccessBlockConfiguration", {}).get(flag) is True
                    for flag in (
                        "BlockPublicAcls",
                        "IgnorePublicAcls",
                        "BlockPublicPolicy",
                        "RestrictPublicBuckets",
                    )
                ),
                "retained-bucket-public-block:" + key,
            )
            self.record(self.retained, "s3-bucket", bucket, billed=True)
        result = self.call(
            "cognito:pool",
            lambda: self.clients["cognito-idp"].describe_user_pool(
                UserPoolId=self.config["user_pool_id"]
            ),
        )
        pool = (result or {}).get("UserPool", {})
        self.require(
            self.arn(pool.get("Arn"), "cognito-idp", "userpool/" + self.config["user_pool_id"]),
            "cognito-arn",
        )
        values = tags(pool.get("UserPoolTags"))
        self.require(
            values.get("Project") == "deepresearch"
            and values.get("Environment") == self.config["environment"],
            "cognito-tags",
        )
        self.record(self.retained, "cognito-user-pool", self.config["user_pool_id"], billed=True)
        for suffix in ("application", "vllm", "langfuse"):
            result = self.call(
                "secrets:describe:" + suffix,
                lambda suffix=suffix: self.clients["secretsmanager"].describe_secret(
                    SecretId=self.name + "/" + suffix
                ),
            )
            value = result or {}
            self.require(
                self.arn(value.get("ARN"), "secretsmanager")
                and value.get("Name") == self.name + "/" + suffix
                and not value.get("DeletedDate"),
                "retained-secret:" + suffix,
            )
            values = tags(value.get("Tags"))
            self.require(
                values.get("Project") == "deepresearch"
                and values.get("Environment") == self.config["environment"],
                "retained-secret-tags",
            )
            self.record(self.retained, "secret-container", value.get("ARN", suffix), billed=True)
        for suffix in ("backend", "web"):
            result = self.call(
                "ecr:describe:" + suffix,
                lambda suffix=suffix: self.clients["ecr"].describe_repositories(
                    repositoryNames=[self.name + "/" + suffix]
                ),
            )
            value = (result or {}).get("repositories", [{}])[0]
            self.require(
                self.arn(
                    value.get("repositoryArn"), "ecr", "repository/" + self.name + "/" + suffix
                ),
                "retained-ecr:" + suffix,
            )
            self.record(
                self.retained, "ecr-repository", value.get("repositoryArn", suffix), billed=True
            )
        for suffix in ("research", "dead"):
            result = self.call(
                "sqs:url:" + suffix,
                lambda suffix=suffix: self.clients["sqs"].get_queue_url(
                    QueueName=self.name + "-" + suffix, QueueOwnerAWSAccountId=self.account
                ),
            )
            if result:
                url = result["QueueUrl"]
                attrs = self.call(
                    "sqs:attributes:" + suffix,
                    lambda: self.clients["sqs"].get_queue_attributes(
                        QueueUrl=url, AttributeNames=["QueueArn"]
                    ),
                )
                self.require(
                    self.arn(
                        (attrs or {}).get("Attributes", {}).get("QueueArn"),
                        "sqs",
                        self.name + "-" + suffix,
                    ),
                    "retained-queue:" + suffix,
                )
                self.record(self.retained, "sqs-queue", url, billed=True)
        topic_arn = self.config["alert_topic_arn"]
        self.require(self.arn(topic_arn, "sns", self.name + "-alerts"), "retained-sns-topic-arn")
        topic = self.call(
            "sns:topic-attributes",
            lambda: self.clients["sns"].get_topic_attributes(TopicArn=topic_arn),
        )
        attributes = (topic or {}).get("Attributes", {})
        self.require(
            attributes.get("Owner") == self.account,
            "retained-sns-topic-owner",
        )
        if topic is not None:
            result = self.call(
                "sns:topic-tags",
                lambda: self.clients["sns"].list_tags_for_resource(ResourceArn=topic_arn),
            )
            values = tags((result or {}).get("Tags"))
            self.require(
                values.get("Project") == "deepresearch"
                and values.get("Environment") == self.config["environment"],
                "retained-sns-topic-tags",
            )
            self.record(self.retained, "sns-topic", topic_arn, billed=True)
        key_arn = self.config["alert_kms_key_arn"]
        key_result = self.call(
            "kms:alert-key",
            lambda: self.clients["kms"].describe_key(KeyId=key_arn),
        )
        metadata = (key_result or {}).get("KeyMetadata", {})
        key_id = metadata.get("KeyId")
        valid_key = (
            metadata.get("Arn") == key_arn
            and key_id == key_arn.rsplit("/", 1)[-1]
            and self.arn(metadata.get("Arn"), "kms")
            and metadata.get("KeyState") == "Enabled"
            and metadata.get("KeyManager") == "CUSTOMER"
            and metadata.get("KeyUsage") == "ENCRYPT_DECRYPT"
            and metadata.get("KeySpec") == "SYMMETRIC_DEFAULT"
            and metadata.get("Description") == f"Encryption key for {self.name} expiry alert topic"
        )
        self.require(valid_key, "retained-sns-kms-key")
        topic_key_id = attributes.get("KmsMasterKeyId")
        topic_key_result = (
            self.call(
                "kms:alert-topic-key-binding",
                lambda: self.clients["kms"].describe_key(KeyId=topic_key_id),
            )
            if isinstance(topic_key_id, str) and topic_key_id
            else None
        )
        topic_key = (topic_key_result or {}).get("KeyMetadata", {})
        self.require(
            topic_key.get("Arn") == key_arn and topic_key.get("KeyId") == key_id,
            "retained-sns-kms-binding",
        )
        rotation = self.call(
            "kms:alert-key-rotation",
            lambda: self.clients["kms"].get_key_rotation_status(KeyId=key_arn),
        )
        self.require(
            rotation is not None and rotation.get("KeyRotationEnabled") is True,
            "retained-sns-kms-rotation",
        )
        if key_result is not None and self.arn(metadata.get("Arn"), "kms"):
            key_tags_result = self.call(
                "kms:alert-key-tags",
                lambda: self.clients["kms"].list_resource_tags(KeyId=key_arn),
            )
            key_tags = tags((key_tags_result or {}).get("Tags"))
            self.require(
                key_tags.get("Project") == "deepresearch"
                and key_tags.get("Environment") == self.config["environment"]
                and key_tags.get("Component") == "expiry-alerts",
                "retained-sns-kms-tags",
            )
            self.record(self.retained, "kms-key", metadata["Arn"], billed=True)

    def expiry(self):
        """Account for optional separate-management expiry resources without deleting them."""
        name = self.name + "-expiry-" + self.config["session_id"]
        result = self.call(
            "codebuild:project", lambda: self.clients["codebuild"].batch_get_projects(names=[name])
        )
        if result is not None:
            projects = result["projects"]
            missing = result["projectsNotFound"]
            self.require(
                (len(projects) == 1 and missing == []) or (projects == [] and missing == [name]),
                "expiry-codebuild-result",
            )
            for project in projects:
                self.require(
                    self.arn(project.get("arn"), "codebuild", "project/" + name),
                    "expiry-codebuild-arn",
                )
                self.record(self.retained, "expiry-codebuild-project", project["arn"], billed=True)
        arn = f"arn:aws:states:{self.region}:{self.account}:stateMachine:{name}"
        result = self.call(
            "states:machine",
            lambda: self.clients["stepfunctions"].describe_state_machine(stateMachineArn=arn),
            ("StateMachineDoesNotExist",),
        )
        if result is not None:
            self.require(result.get("stateMachineArn") == arn, "expiry-state-machine-arn")
            self.record(self.retained, "expiry-state-machine", arn, billed=True)
        result = self.call(
            "scheduler:schedule",
            lambda: self.clients["scheduler"].get_schedule(Name=name),
            ("ResourceNotFoundException",),
        )
        if result is not None:
            self.require(
                self.arn(result.get("Arn"), "scheduler", "schedule/default/" + name),
                "expiry-schedule-arn",
            )
            self.record(self.retained, "expiry-schedule", result["Arn"], billed=True)
        for group in self.pages(
            "logs", "describe_log_groups", "logGroups", logGroupNamePrefix="/aws/codebuild/" + name
        ):
            if group.get("logGroupName") == "/aws/codebuild/" + name:
                self.require(self.arn(group.get("arn"), "logs"), "expiry-loggroup-arn")
                self.record(self.retained, "expiry-log-group", group["logGroupName"], billed=True)
        alarm_names = [name + "-failed", name + "-scheduler-dlq-visible"]
        alarm = self.call(
            "cloudwatch:expiry-alarm",
            lambda: self.clients["cloudwatch"].describe_alarms(AlarmNames=alarm_names),
        )
        self.require(alarm is not None and "MetricAlarms" in alarm, "expiry-alarm-response")
        for value in (alarm or {}).get("MetricAlarms", []):
            alarm_name = value.get("AlarmName")
            self.require(
                alarm_name in alarm_names
                and self.arn(value.get("AlarmArn"), "cloudwatch", "alarm:" + alarm_name),
                "expiry-alarm-arn",
            )
            self.record(self.retained, "expiry-alarm", value["AlarmArn"], billed=True)
        queue = self.call(
            "sqs:expiry-dead",
            lambda: self.clients["sqs"].get_queue_url(
                QueueName=name + "-scheduler-dead", QueueOwnerAWSAccountId=self.account
            ),
            ("AWS.SimpleQueueService.NonExistentQueue", "QueueDoesNotExist"),
        )
        if queue is not None:
            attrs = self.call(
                "sqs:expiry-dead-attributes",
                lambda: self.clients["sqs"].get_queue_attributes(
                    QueueUrl=queue["QueueUrl"], AttributeNames=["QueueArn"]
                ),
            )
            self.require(
                self.arn(
                    (attrs or {}).get("Attributes", {}).get("QueueArn"),
                    "sqs",
                    name + "-scheduler-dead",
                ),
                "expiry-dead-arn",
            )
            self.record(self.retained, "expiry-dead-letter-queue", queue["QueueUrl"], billed=True)
        for suffix in ("", "-states", "-scheduler"):
            result = self.call(
                "iam:expiry-role",
                lambda suffix=suffix: self.clients["iam"].get_role(RoleName=name + suffix),
                ("NoSuchEntity", "NoSuchEntityException"),
            )
            if result is not None:
                self.require(
                    self.arn(result["Role"].get("Arn"), "iam", "role/" + name + suffix),
                    "expiry-role-arn",
                )
                self.record(self.retained, "expiry-iam-role", result["Role"]["Arn"], billed=False)


def release_plan(config, instant=None):
    validate_config(config)
    instant = instant or datetime.now(timezone.utc)
    return {
        "version": 1,
        "operation": "release",
        "config": config,
        "created_at": instant.isoformat(),
        "expires_at": (instant + timedelta(minutes=15)).isoformat(),
        "mutations": [
            "Collect fresh provider inventory under the current lifecycle lease",
            "Atomically archive completion and remove only the matching active-session admission",
        ],
        "retained": [
            "Monthly reservation counters are never refunded",
            "Explicit retained data and management resources may continue to incur charges",
        ],
    }


def release_admission(store, config, lease, clients, plan, approved_hash):
    """Collect fresh provider truth, then archive+release atomically. Never refund time.

    Called only from an explicitly authorized CLI operation. A user-supplied JSON
    inventory is not accepted. Dependencies are injected for offline testing, just
    as AWS clients are injected into the existing lifecycle adapter.
    """
    validate_config(config)
    approve(plan, approved_hash, datetime.fromtimestamp(store.clock(), timezone.utc))
    if plan.get("operation") != "release" or plan.get("config") != config:
        raise Refused("Not an approved matching admission-release plan")
    if lease.store is not store or lease.config != config:
        raise Refused("Release lease/config mismatch")
    store.assert_admitted(config)
    lease.assert_owned()
    evidence = InventoryCollector(config, clients, clock=store.clock).collect()
    lease.assert_owned()
    if not (
        evidence["inventory_complete"]
        and evidence["runtime_gone"]
        and evidence["retained_accounted"]
    ):
        raise Refused(
            "Fresh provider inventory is incomplete or runtime resources remain; admission retained"
        )
    instant = int(store.clock())
    if evidence["collected_at_epoch"] > instant or instant - evidence["started_at_epoch"] > 300:
        raise Refused("Provider inventory is stale; collect again under the current lease")
    approve(plan, approved_hash, datetime.fromtimestamp(store.clock(), timezone.utc))
    completion = {
        "version": 1,
        "operation": "session-completion",
        "config": config,
        "completed_at_epoch": instant,
        "inventory_sha256": digest(evidence),
        "approved_release_plan_sha256": approved_hash,
        "retained_resources": evidence["retained_resources"],
        "time_refunded": False,
        "scope": evidence["scope"],
        "charges_may_continue": True,
    }
    if len(canonical(completion)) > 300_000:
        raise Refused("Completion evidence exceeds safe DynamoDB item size; admission retained")
    key = "completion#" + config["environment"] + "#" + config["session_id"]
    transaction = [
        {
            "ConditionCheck": {
                "TableName": store.table,
                "Key": {"key": {"S": "lifecycle-lock"}},
                "ConditionExpression": "owner_id = :owner AND session_id = :session AND expires_at > :now",
                "ExpressionAttributeValues": {
                    ":owner": {"S": lease.owner},
                    ":session": {"S": config["session_id"]},
                    ":now": {"N": str(instant)},
                },
            }
        },
        {
            "Put": {
                "TableName": store.table,
                "Item": {"key": {"S": key}, "receipt_json": {"S": canonical(completion).decode()}},
                "ConditionExpression": "attribute_not_exists(#key)",
                "ExpressionAttributeNames": {"#key": "key"},
            }
        },
        {
            "Delete": {
                "TableName": store.table,
                "Key": {"key": {"S": "active-session"}},
                "ConditionExpression": "account_id = :account AND environment = :environment AND session_id = :session",
                "ExpressionAttributeValues": {
                    ":account": {"S": config["account_id"]},
                    ":environment": {"S": config["environment"]},
                    ":session": {"S": config["session_id"]},
                },
            }
        },
    ]
    token = hashlib.sha256((key + ":" + digest(completion)).encode()).hexdigest()[:36]
    try:
        store.client.transact_write_items(TransactItems=transaction, ClientRequestToken=token)
    except Exception as exc:
        raise Refused(
            "Completion transaction failed or result is uncertain; reconcile the retained completion key before retrying"
        ) from exc
    return {**completion, "admission_released": True, "completion_key": key, "inventory": evidence}
