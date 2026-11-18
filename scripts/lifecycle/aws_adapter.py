"""AWS/Kubernetes adapter; clients are injected for tests and created lazily by CLI."""

import base64
import hashlib
import json
import re
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from .core import Refused, canonical, command, now, timestamp, validate_config


class AWSAdapter:
    def __init__(self, config, clients, runner=command, sleep=time.sleep):
        self.config = validate_config(config)
        self.clients = clients
        self.runner = runner
        self.sleep = sleep
        self.exclusive_guard = None
        self.context = f"arn:aws:eks:{config['region']}:{config['account_id']}:cluster/{config['cluster_name']}"

    def kubectl(self, *args):
        if args and args[0] in {"scale", "exec", "cp", "delete"}:
            self.require_lease()
        return self.runner(["kubectl", "--context", self.context, "--namespace", "research", *args])

    def require_lease(self):
        if self.exclusive_guard is None:
            raise Refused("Mutating lifecycle operations require a global admission and lease")
        self.exclusive_guard()

    def identity(self):
        actual = self.clients["sts"].get_caller_identity()["Account"]
        if actual != self.config["account_id"]:
            raise Refused("Active AWS credentials belong to another account")

    def verify_retained(self):
        self.identity()
        expected = {"Project": "deepresearch", "Environment": self.config["environment"]}
        for key in ["document_bucket", "backup_bucket", "trace_bucket"]:
            bucket = self.config[key]
            tags = {
                x["Key"]: x["Value"]
                for x in self.clients["s3"].get_bucket_tagging(
                    Bucket=bucket, ExpectedBucketOwner=self.config["account_id"]
                )["TagSet"]
            }
            if any(tags.get(k) != v for k, v in expected.items()):
                raise Refused("Bucket tag mismatch: " + bucket)
            if (
                self.clients["s3"]
                .get_bucket_versioning(Bucket=bucket, ExpectedBucketOwner=self.config["account_id"])
                .get("Status")
                != "Enabled"
            ):
                raise Refused("Versioning must be enabled: " + bucket)
        pool = self.clients["cognito-idp"].describe_user_pool(
            UserPoolId=self.config["user_pool_id"]
        )["UserPool"]
        if any(pool.get("UserPoolTags", {}).get(k) != v for k, v in expected.items()):
            raise Refused("Cognito scope mismatch")
        self.verify_alert_channel()

    def verify_alert_channel(self):
        topic_arn = self.config["alert_topic_arn"]
        key_arn = self.config["alert_kms_key_arn"]
        topic = self.clients["sns"].get_topic_attributes(TopicArn=topic_arn).get("Attributes", {})
        key = self.clients["kms"].describe_key(KeyId=key_arn)["KeyMetadata"]
        topic_key_id = topic.get("KmsMasterKeyId")
        topic_key = (
            self.clients["kms"].describe_key(KeyId=topic_key_id).get("KeyMetadata", {})
            if isinstance(topic_key_id, str) and topic_key_id
            else {}
        )
        key_tags = {
            value["TagKey"]: value["TagValue"]
            for value in self.clients["kms"].list_resource_tags(KeyId=key_arn).get("Tags", [])
        }
        rotation = self.clients["kms"].get_key_rotation_status(KeyId=key_arn)
        if (
            topic.get("Owner") != self.config["account_id"]
            or topic_key.get("Arn") != key_arn
            or topic_key.get("KeyId") != key.get("KeyId")
            or key.get("Arn") != key_arn
            or key.get("KeyId") != key_arn.rsplit("/", 1)[-1]
            or key.get("KeyState") != "Enabled"
            or key.get("KeyManager") != "CUSTOMER"
            or key.get("KeyUsage") != "ENCRYPT_DECRYPT"
            or key.get("KeySpec") != "SYMMETRIC_DEFAULT"
            or key.get("Description")
            != f"Encryption key for deepresearch-{self.config['environment']} expiry alert topic"
            or rotation.get("KeyRotationEnabled") is not True
            or key_tags.get("Project") != "deepresearch"
            or key_tags.get("Environment") != self.config["environment"]
            or key_tags.get("Component") != "expiry-alerts"
        ):
            raise Refused("Alert topic or customer-managed encryption key is not ready")

    def verify_scope(self, config):
        if config != self.config:
            raise Refused("Adapter configuration mismatch")
        self.verify_retained()
        cluster = self.clients["eks"].describe_cluster(name=config["cluster_name"])["cluster"]
        if cluster["arn"] != self.context or any(
            cluster.get("tags", {}).get(k) != v
            for k, v in {
                "Project": "deepresearch",
                "Environment": config["environment"],
                "Session": config["session_id"],
            }.items()
        ):
            raise Refused("EKS cluster scope mismatch")
        kube = json.loads(
            self.runner(
                ["kubectl", "--context", self.context, "config", "view", "--minify", "-o", "json"]
            )
        )
        current = kube["clusters"][0]["cluster"]
        if (
            current.get("server") != cluster["endpoint"]
            or current.get("certificate-authority-data") != cluster["certificateAuthority"]["data"]
        ):
            raise Refused("Kubernetes context endpoint/CA does not match provider cluster")
        db = self.clients["rds"].describe_db_instances(
            DBInstanceIdentifier=config["database_identifier"]
        )["DBInstances"][0]
        tags = {
            x["Key"]: x["Value"]
            for x in self.clients["rds"].list_tags_for_resource(ResourceName=db["DBInstanceArn"])[
                "TagList"
            ]
        }
        if (
            tags.get("Project") != "deepresearch"
            or tags.get("Environment") != config["environment"]
            or tags.get("Session") != config["session_id"]
            or not db.get("StorageEncrypted")
        ):
            raise Refused("Database scope/encryption mismatch")

    def quiesce(self):
        for name in [
            "research-api",
            "research-web",
            "research-worker",
            "langfuse-web",
            "langfuse-worker",
        ]:
            self.kubectl("scale", "deployment/" + name, "--replicas=0")
        for _ in range(90):
            try:
                writers = self.verify_quiesced()
                return {
                    "namespace": "research",
                    "writers": writers,
                    "verified_at": now().isoformat(),
                }
            except Refused:
                self.sleep(2)
        raise Refused("Application pods did not quiesce; no backup receipt issued")

    def verify_quiesced(self, expected=None):
        writers = []
        for name in [
            "research-api",
            "research-web",
            "research-worker",
            "langfuse-web",
            "langfuse-worker",
        ]:
            dep = json.loads(self.kubectl("get", "deployment", name, "-o", "json"))
            metadata = dep.get("metadata", {})
            if not metadata.get("uid") or not isinstance(metadata.get("generation"), int):
                raise Refused("Missing writer UID/generation")
            writers.append(
                {"name": name, "uid": metadata["uid"], "generation": metadata["generation"]}
            )
            if dep.get("spec", {}).get("replicas", 1) != 0:
                raise Refused("Application deployment resumed: " + name)
        pods = json.loads(self.kubectl("get", "pods", "-o", "json"))["items"]
        for pod in pods:
            labels = pod["metadata"].get("labels", {})
            component = labels.get("component")
            if (labels.get("app") == "research" and component in ["api", "web", "worker"]) or (
                labels.get("app") == "observability"
                and component in ["langfuse-web", "langfuse-worker"]
            ):
                if pod.get("status", {}).get("phase") not in ["Succeeded", "Failed"]:
                    raise Refused("A writer pod is still present")
        if expected is not None and expected.get("writers") != writers:
            raise Refused(
                "Writer deployment generation/UID changed since backup; fresh backup required"
            )
        return writers

    def ch(self, query):
        output = self.kubectl(
            "exec",
            "observability-clickhouse-0",
            "--container",
            "clickhouse",
            "--",
            "clickhouse-client",
            "--config-file=/etc/clickhouse-client/config.xml",
            "--format",
            "JSONEachRow",
            "--query",
            query,
        )
        return [json.loads(line) for line in output.splitlines() if line.strip()]

    def backup_clickhouse(self, prefix):
        # Bucket/prefix are generated from validated identifiers, never arbitrary SQL.
        url = f"https://{self.config['backup_bucket']}.s3.{self.config['region']}.amazonaws.com/{prefix}clickhouse/"
        result = self.ch("BACKUP DATABASE default TO S3('" + url + "')")
        if len(result) != 1 or result[0].get("status") != "BACKUP_CREATED":
            raise Refused("ClickHouse backup did not complete")
        backup_id = result[0]["id"]
        if not re.fullmatch(r"[a-f0-9-]{36}", backup_id):
            raise Refused("Unexpected ClickHouse backup identifier")
        records = self.ch(
            "SELECT id, status, error FROM system.backups WHERE id='" + backup_id + "'"
        )
        if (
            len(records) != 1
            or records[0].get("status") != "BACKUP_CREATED"
            or records[0].get("error")
        ):
            raise Refused("ClickHouse system.backups verification failed")
        objects = self.versions(self.config["backup_bucket"], prefix + "clickhouse/")
        if not objects["versions"]:
            raise Refused("No versioned ClickHouse backup objects found")
        return {"id": backup_id, "url": url, "prefix": prefix + "clickhouse/", "objects": objects}

    def backup_valkey(self, prefix):
        self.kubectl(
            "exec", "observability-valkey-0", "--container", "valkey", "--", "valkey-cli", "SAVE"
        )
        with tempfile.TemporaryDirectory(prefix="research-valkey-") as tmp:
            dest = Path(tmp) / "dump.rdb"
            self.kubectl(
                "cp", "observability-valkey-0:/data/dump.rdb", str(dest), "--container", "valkey"
            )
            checksum = hashlib.sha256(dest.read_bytes()).hexdigest()
            with dest.open("rb") as stream:
                self.require_lease()
                result = self.clients["s3"].put_object(
                    ExpectedBucketOwner=self.config["account_id"],
                    Bucket=self.config["backup_bucket"],
                    Key=prefix + "valkey/dump.rdb",
                    Body=stream,
                    ServerSideEncryption="AES256",
                    ChecksumSHA256=base64.b64encode(bytes.fromhex(checksum)).decode(),
                    Metadata={"sha256": checksum, "session": self.config["session_id"]},
                )
        if not result.get("VersionId") or result["VersionId"] == "null":
            raise Refused("Valkey backup did not receive a stable S3 version ID")
        return {
            "bucket": self.config["backup_bucket"],
            "key": prefix + "valkey/dump.rdb",
            "version_id": result["VersionId"],
            "sha256": checksum,
        }

    def snapshot_database(self, identifier):
        self.require_lease()
        rds = self.clients["rds"]
        rds.create_db_snapshot(
            DBSnapshotIdentifier=identifier,
            DBInstanceIdentifier=self.config["database_identifier"],
            Tags=[
                {"Key": "Project", "Value": "deepresearch"},
                {"Key": "Environment", "Value": self.config["environment"]},
                {"Key": "Session", "Value": self.config["session_id"]},
            ],
        )["DBSnapshot"]
        rds.get_waiter("db_snapshot_available").wait(
            DBSnapshotIdentifier=identifier, WaiterConfig={"Delay": 30, "MaxAttempts": 60}
        )
        snapshot = rds.describe_db_snapshots(DBSnapshotIdentifier=identifier)["DBSnapshots"][0]
        if snapshot["Status"] != "available" or not snapshot.get("Encrypted"):
            raise Refused("RDS snapshot unavailable or unencrypted")
        return {
            "identifier": identifier,
            "arn": snapshot["DBSnapshotArn"],
            "created_at": snapshot["SnapshotCreateTime"].isoformat(),
        }

    def versions(self, bucket, prefix=""):
        versions = []
        markers = []
        pages = (
            self.clients["s3"]
            .get_paginator("list_object_versions")
            .paginate(Bucket=bucket, Prefix=prefix, ExpectedBucketOwner=self.config["account_id"])
        )
        for page in pages:
            for entry in page.get("Versions", []):
                if entry["VersionId"] == "null":
                    raise Refused("Unversioned object in backup inventory")
                versions.append(
                    {key: entry[key] for key in ["Key", "VersionId", "ETag", "Size", "IsLatest"]}
                )
            for entry in page.get("DeleteMarkers", []):
                markers.append({key: entry[key] for key in ["Key", "VersionId", "IsLatest"]})
        return {"bucket": bucket, "versions": versions, "delete_markers": markers}

    def inventory(self):
        return [
            self.versions(self.config[key])
            for key in ["document_bucket", "trace_bucket", "backup_bucket"]
        ]

    def verify_objects(self, inventory):
        for entry in inventory["versions"]:
            if not isinstance(entry.get("Key"), str) or not entry.get("VersionId") or entry["VersionId"] == "null":
                raise Refused("Retained object receipt is missing an exact version identity")
            current = self.versions(inventory["bucket"], prefix=entry["Key"])
            exact = [
                candidate for candidate in current["versions"]
                if candidate["Key"] == entry["Key"] and candidate["VersionId"] == entry["VersionId"]
            ]
            if len(exact) != 1 or exact[0].get("ETag") != entry.get("ETag") or exact[0].get("Size") != entry.get("Size"):
                raise Refused("Versioned backup object differs from receipt")

    def verify_receipt(
        self, receipt, require_quiesced=True, allow_expired=False, require_durable=True
    ):
        if (
            receipt.get("operation") != "backup-receipt"
            or receipt.get("config") != self.config
            or not receipt.get("identities_retained")
        ):
            raise Refused("Invalid backup receipt binding")
        if (not allow_expired and timestamp(receipt["expires_at"]) <= now()) or timestamp(
            receipt["created_at"]
        ) > now() + timedelta(minutes=2):
            raise Refused("Backup receipt expired or from the future")
        self.verify_retained()
        self.verify_backup_snapshot(receipt)
        expected_prefix = f"sessions/{self.config['environment']}/{self.config['session_id']}/"
        if receipt["clickhouse"]["prefix"] != expected_prefix + "clickhouse/":
            raise Refused("Unexpected ClickHouse backup prefix")
        if (
            receipt["clickhouse"]["objects"]["bucket"] != self.config["backup_bucket"]
            or not receipt["clickhouse"]["objects"]["versions"]
        ):
            raise Refused("Missing ClickHouse versioned backup objects")
        self.verify_objects(receipt["clickhouse"]["objects"])
        valkey = receipt["valkey"]
        if (
            valkey["bucket"] != self.config["backup_bucket"]
            or valkey["key"] != expected_prefix + "valkey/dump.rdb"
            or valkey["version_id"] == "null"
        ):
            raise Refused("Valkey backup binding mismatch")
        body = self.clients["s3"].get_object(
            ExpectedBucketOwner=self.config["account_id"],
            Bucket=valkey["bucket"],
            Key=valkey["key"],
            VersionId=valkey["version_id"],
        )["Body"]
        checksum = hashlib.sha256()
        while chunk := body.read(1024 * 1024):
            checksum.update(chunk)
        body.close()
        if checksum.hexdigest() != valkey["sha256"]:
            raise Refused("Valkey checksum mismatch")
        inventories = receipt["object_inventory"]
        if {x["bucket"] for x in inventories} != {
            self.config[k] for k in ["document_bucket", "trace_bucket", "backup_bucket"]
        }:
            raise Refused("Incomplete retained object inventory")
        for inventory in inventories:
            self.verify_objects(inventory)
        if require_durable:
            self.verify_durable_receipt(receipt)
        if require_quiesced:
            self.verify_quiesced(receipt["drain"])

    def verify_backup_snapshot(self, receipt):
        """Provider-verify the exact encrypted, session-tagged snapshot in a receipt."""
        snapshot = receipt["database_snapshot"]
        descriptions = self.clients["rds"].describe_db_snapshots(
            DBSnapshotIdentifier=snapshot["identifier"]
        )["DBSnapshots"]
        if len(descriptions) != 1:
            raise Refused("Backup snapshot lookup was not unique")
        actual = descriptions[0]
        expected_arn = f"arn:aws:rds:{self.config['region']}:{self.config['account_id']}:snapshot:"
        tags = {
            v["Key"]: v["Value"]
            for v in self.clients["rds"].list_tags_for_resource(
                ResourceName=actual["DBSnapshotArn"]
            )["TagList"]
        }
        if (
            actual["Status"] != "available"
            or not actual.get("Encrypted")
            or actual["DBSnapshotIdentifier"] != snapshot["identifier"]
            or actual["DBSnapshotArn"] != snapshot["arn"]
            or not snapshot["arn"].startswith(expected_arn)
            or actual["DBInstanceIdentifier"] != self.config["database_identifier"]
            or tags.get("Project") != "deepresearch"
            or tags.get("Environment") != self.config["environment"]
            or tags.get("Session") != self.config["session_id"]
        ):
            raise Refused("Provider snapshot verification failed")
        self.verify_snapshot_timing(actual, receipt)
        return actual

    def verify_private_restore_subnet_group(self, subnet_group_name):
        groups = (
            self.clients["rds"]
            .describe_db_subnet_groups(DBSubnetGroupName=subnet_group_name)
            .get("DBSubnetGroups", [])
        )
        if (
            len(groups) != 1
            or groups[0].get("DBSubnetGroupName") != subnet_group_name
            or groups[0].get("SubnetGroupStatus") != "Complete"
        ):
            raise Refused("Restore DB subnet group is missing, ambiguous, or incomplete")
        group = groups[0]
        subnet_entries = group.get("Subnets", [])
        subnet_ids = [item.get("SubnetIdentifier") for item in subnet_entries]
        vpc_id = group.get("VpcId")
        if (
            not isinstance(vpc_id, str)
            or not re.fullmatch(r"vpc-[0-9a-f]{8,17}", vpc_id)
            or len(subnet_ids) < 2
            or len(set(subnet_ids)) != len(subnet_ids)
            or any(not subnet_id for subnet_id in subnet_ids)
            or any(item.get("SubnetStatus") != "Active" for item in subnet_entries)
        ):
            raise Refused("Restore DB subnet group does not contain active distinct VPC subnets")
        subnet_group_tags = {
            item["Key"]: item["Value"]
            for item in self.clients["rds"].list_tags_for_resource(
                ResourceName=group["DBSubnetGroupArn"]
            )["TagList"]
        }
        if any(
            subnet_group_tags.get(key) != value
            for key, value in {
                "Project": "deepresearch",
                "Environment": self.config["environment"],
                "Session": self.config["session_id"],
            }.items()
        ):
            raise Refused("Restore DB subnet group tags do not match the session")
        subnets = self.clients["ec2"].describe_subnets(SubnetIds=subnet_ids).get("Subnets", [])
        if len(subnets) != len(subnet_ids) or any(
            item.get("SubnetId") not in subnet_ids
            or item.get("VpcId") != vpc_id
            or item.get("MapPublicIpOnLaunch") is not False
            for item in subnets
        ):
            raise Refused("Restore DB subnet group includes a missing or public-address subnet")
        route_tables = (
            self.clients["ec2"]
            .describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
            .get("RouteTables", [])
        )
        for subnet_id in subnet_ids:
            explicit = [
                table
                for table in route_tables
                if any(
                    association.get("SubnetId") == subnet_id
                    for association in table.get("Associations", [])
                )
            ]
            if len(explicit) > 1:
                raise Refused("Restore subnet has ambiguous route table associations")
            associated = explicit or [
                table
                for table in route_tables
                if any(
                    association.get("Main") is True for association in table.get("Associations", [])
                )
            ]
            if len(associated) != 1:
                raise Refused("Restore subnet has no unique route table")
            if any(
                isinstance(route.get("GatewayId"), str) and route["GatewayId"].startswith("igw-")
                for route in associated[0].get("Routes", [])
            ):
                raise Refused("Restore DB subnet group routes directly to an internet gateway")
        return vpc_id

    def ensure_restore_security_group(self, vpc_id, plan):
        """Create or verify a session-scoped SG with no ingress or egress rules."""
        name = plan["restore_security_group_name"]
        tags = {
            **plan["tags"],
            "Component": "database-restore-security-group",
        }
        response = self.clients["ec2"].describe_security_groups(
            Filters=[
                {"Name": "group-name", "Values": [name]},
                {"Name": "vpc-id", "Values": [vpc_id]},
            ]
        )
        groups = response.get("SecurityGroups", [])
        if len(groups) > 1:
            raise Refused("Restore security group lookup was ambiguous")
        if not groups:
            self.require_lease()
            created = self.clients["ec2"].create_security_group(
                GroupName=name,
                Description=f"Isolated RDS restore for {self.config['session_id']}",
                VpcId=vpc_id,
                TagSpecifications=[
                    {
                        "ResourceType": "security-group",
                        "Tags": [
                            {"Key": key, "Value": value} for key, value in sorted(tags.items())
                        ],
                    }
                ],
            )
            group_id = created.get("GroupId")
            if not isinstance(group_id, str) or not re.fullmatch(r"sg-[0-9a-f]{8,17}", group_id):
                raise Refused("Restore security group creation returned no valid ID")
            groups = (
                self.clients["ec2"]
                .describe_security_groups(GroupIds=[group_id])
                .get("SecurityGroups", [])
            )
        if len(groups) != 1:
            raise Refused("Restore security group could not be uniquely inspected")
        group = groups[0]
        group_tags = {item["Key"]: item["Value"] for item in group.get("Tags", [])}
        if (
            not isinstance(group.get("GroupId"), str)
            or not re.fullmatch(r"sg-[0-9a-f]{8,17}", group["GroupId"])
            or group.get("GroupName") != name
            or group.get("VpcId") != vpc_id
            or group_tags != tags
            or group.get("IpPermissions") != []
        ):
            raise Refused("Restore security group is not exclusively scoped and ingress-isolated")
        group_id = group.get("GroupId")
        egress = group.get("IpPermissionsEgress", [])
        if egress:
            self.require_lease()
            self.clients["ec2"].revoke_security_group_egress(
                GroupId=group["GroupId"], IpPermissions=egress
            )
        verified = (
            self.clients["ec2"]
            .describe_security_groups(GroupIds=[group["GroupId"]])
            .get("SecurityGroups", [])
        )
        if len(verified) != 1:
            raise Refused("Restore security group could not be uniquely inspected")
        group = verified[0]
        group_tags = {item["Key"]: item["Value"] for item in group.get("Tags", [])}
        if (
            group.get("GroupId") != group_id
            or group.get("GroupName") != name
            or group.get("VpcId") != vpc_id
            or group_tags != tags
            or group.get("IpPermissions") != []
            or group.get("IpPermissionsEgress") != []
        ):
            raise Refused("Restore security group is not exclusively scoped and rule-free")
        return group["GroupId"]

    def restore_database_snapshot(self, plan, receipt):
        """Restore a verified receipt snapshot to a new private, tagged DB instance."""
        self.require_lease()
        if plan.get("config") != self.config:
            raise Refused("Restore plan configuration does not match this adapter")
        self.verify_receipt(receipt, require_quiesced=False, allow_expired=True)
        snapshot = receipt["database_snapshot"]
        if (
            snapshot.get("identifier") != plan.get("source_snapshot_identifier")
            or snapshot.get("arn") != plan.get("source_snapshot_arn")
            or receipt["config"]["database_identifier"] != plan.get("source_database_identifier")
            or plan.get("target_database_identifier") == plan.get("source_database_identifier")
        ):
            raise Refused("Restore source or target does not match the approved receipt")
        vpc_id = self.verify_private_restore_subnet_group(plan["db_subnet_group_name"])
        target = plan["target_database_identifier"]
        try:
            response = self.clients["rds"].describe_db_instances(DBInstanceIdentifier=target)
        except self.clients["rds"].exceptions.DBInstanceNotFoundFault:
            response = None
        if response is not None:
            raise Refused("Restore target already exists or lookup is ambiguous")
        security_group_id = self.ensure_restore_security_group(vpc_id, plan)
        self.require_lease()
        self.clients["rds"].restore_db_instance_from_db_snapshot(
            DBInstanceIdentifier=target,
            DBSnapshotIdentifier=plan["source_snapshot_identifier"],
            DBInstanceClass=plan["db_instance_class"],
            DBSubnetGroupName=plan["db_subnet_group_name"],
            VpcSecurityGroupIds=[security_group_id],
            MultiAZ=False,
            PubliclyAccessible=False,
            CopyTagsToSnapshot=False,
            Tags=[{"Key": key, "Value": value} for key, value in sorted(plan["tags"].items())],
        )
        self.clients["rds"].get_waiter("db_instance_available").wait(
            DBInstanceIdentifier=target, WaiterConfig={"Delay": 15, "MaxAttempts": 40}
        )
        self.require_lease()
        instances = (
            self.clients["rds"]
            .describe_db_instances(DBInstanceIdentifier=target)
            .get("DBInstances", [])
        )
        if len(instances) != 1:
            raise Refused("Restored DB instance lookup was not unique")
        instance = instances[0]
        expected_arn = (
            f"arn:aws:rds:{self.config['region']}:{self.config['account_id']}:db:{target}"
        )
        tags = {
            item["Key"]: item["Value"]
            for item in self.clients["rds"].list_tags_for_resource(
                ResourceName=instance["DBInstanceArn"]
            )["TagList"]
        }
        expected_tags = plan["tags"]
        if (
            instance.get("DBInstanceIdentifier") != target
            or instance.get("DBInstanceArn") != expected_arn
            or instance.get("DBInstanceStatus") != "available"
            or instance.get("DBInstanceClass") != plan["db_instance_class"]
            or instance.get("StorageEncrypted") is not True
            or instance.get("PubliclyAccessible") is not False
            or instance.get("DBSubnetGroup", {}).get("DBSubnetGroupName")
            != plan["db_subnet_group_name"]
            or instance.get("VpcSecurityGroups")
            != [{"VpcSecurityGroupId": security_group_id, "Status": "active"}]
            or any(tags.get(key) != value for key, value in expected_tags.items())
        ):
            raise Refused(
                "Restored DB provider state does not match the approved private encrypted target"
            )
        return {
            "target_database_identifier": target,
            "target_database_arn": expected_arn,
            "source_snapshot_identifier": plan["source_snapshot_identifier"],
            "source_snapshot_arn": plan["source_snapshot_arn"],
            "provider_verified": True,
        }

    def cluster_albs(self):
        result = []
        for page in self.clients["elbv2"].get_paginator("describe_load_balancers").paginate():
            for lb in page["LoadBalancers"]:
                tags = self.clients["elbv2"].describe_tags(ResourceArns=[lb["LoadBalancerArn"]])[
                    "TagDescriptions"
                ][0]["Tags"]
                if any(
                    t["Key"] == "elbv2.k8s.aws/cluster"
                    and t["Value"] == self.config["cluster_name"]
                    for t in tags
                ):
                    result.append(lb["LoadBalancerArn"])
        return result

    def remove_ingress(self):
        self.kubectl(
            "delete",
            "ingress",
            "research",
            "research-console",
            "--ignore-not-found=true",
            "--wait=true",
            "--timeout=180s",
        )
        for _ in range(120):
            if not self.cluster_albs():
                return
            self.sleep(5)
        raise Refused("Cluster load balancers remain; refusing runtime destruction")

    def disable_database_protection(self):
        self.require_lease()
        self.clients["rds"].modify_db_instance(
            DBInstanceIdentifier=self.config["database_identifier"],
            DeletionProtection=False,
            ApplyImmediately=True,
        )
        self.clients["rds"].get_waiter("db_instance_available").wait(
            DBInstanceIdentifier=self.config["database_identifier"],
            WaiterConfig={"Delay": 15, "MaxAttempts": 40},
        )

    def disable_database_protection_if_present(self, expected_to_delete=False):
        self.require_lease()
        try:
            instances = self.clients["rds"].describe_db_instances(
                DBInstanceIdentifier=self.config["database_identifier"]
            )["DBInstances"]
        except self.clients["rds"].exceptions.DBInstanceNotFoundFault:
            return False
        if len(instances) != 1:
            raise Refused("Bootstrap database lookup was ambiguous")
        instance = instances[0]
        tags = {
            item["Key"]: item["Value"]
            for item in self.clients["rds"].list_tags_for_resource(
                ResourceName=instance["DBInstanceArn"]
            )["TagList"]
        }
        if (
            instance.get("DBInstanceIdentifier") != self.config["database_identifier"]
            or tags.get("Project") != "deepresearch"
            or tags.get("Environment") != self.config["environment"]
            or tags.get("Session") != self.config["session_id"]
        ):
            raise Refused("Refusing to change protection on an unbound database")
        if not expected_to_delete:
            raise Refused("A session database exists outside the approved bootstrap destroy plan")
        if instance.get("DeletionProtection"):
            self.disable_database_protection()
        return True

    def restore_database_protection_if_present(self):
        self.require_lease()
        try:
            instances = self.clients["rds"].describe_db_instances(
                DBInstanceIdentifier=self.config["database_identifier"]
            )["DBInstances"]
        except self.clients["rds"].exceptions.DBInstanceNotFoundFault:
            return
        if len(instances) != 1:
            raise Refused("Bootstrap database lookup was ambiguous while restoring protection")
        instance = instances[0]
        tags = {
            item["Key"]: item["Value"]
            for item in self.clients["rds"].list_tags_for_resource(
                ResourceName=instance["DBInstanceArn"]
            )["TagList"]
        }
        if (
            instance.get("DBInstanceIdentifier") != self.config["database_identifier"]
            or tags.get("Project") != "deepresearch"
            or tags.get("Environment") != self.config["environment"]
            or tags.get("Session") != self.config["session_id"]
            or instance.get("DBInstanceStatus") != "available"
        ):
            raise Refused("Could not safely restore deletion protection to the session database")
        if not instance.get("DeletionProtection"):
            self.clients["rds"].modify_db_instance(
                DBInstanceIdentifier=self.config["database_identifier"],
                DeletionProtection=True,
                ApplyImmediately=True,
            )
            self.clients["rds"].get_waiter("db_instance_available").wait(
                DBInstanceIdentifier=self.config["database_identifier"],
                WaiterConfig={"Delay": 15, "MaxAttempts": 40},
            )

    def wait_bootstrap_final_snapshot(self):
        self.require_lease()
        identifier = f"{self.config['database_identifier']}-{self.config['session_id']}-final"
        self.clients["rds"].get_waiter("db_snapshot_available").wait(
            DBSnapshotIdentifier=identifier,
            WaiterConfig={"Delay": 15, "MaxAttempts": 80},
        )
        snapshot = self.clients["rds"].describe_db_snapshots(DBSnapshotIdentifier=identifier)[
            "DBSnapshots"
        ]
        if (
            len(snapshot) != 1
            or snapshot[0].get("Status") != "available"
            or snapshot[0].get("Encrypted") is not True
            or snapshot[0].get("DBSnapshotArn")
            != f"arn:aws:rds:{self.config['region']}:{self.config['account_id']}:snapshot:{identifier}"
        ):
            raise Refused("Bootstrap final snapshot is missing, unencrypted, or out of scope")

    def verify_bootstrap_empty(self):
        """Allow first-provision cleanup only before application installation."""
        self.require_lease()
        self.identity()
        self.verify_retained()
        marker_key = f"app-writers#{self.config['environment']}#{self.config['session_id']}"
        marker = self.clients["dynamodb"].get_item(
            TableName="deepresearch-control",
            Key={"key": {"S": marker_key}},
            ConsistentRead=True,
        ).get("Item", {})
        if marker and (
            marker.get("key", {}).get("S") != marker_key
            or marker.get("session_id", {}).get("S") != self.config["session_id"]
            or marker.get("environment", {}).get("S") != self.config["environment"]
            or not marker.get("started_at", {}).get("N")
            or not marker.get("commit_sha", {}).get("S")
            or not marker.get("workflow_run_id", {}).get("N")
        ):
            raise Refused("Could not verify durable application deployment history")
        if marker:
            raise Refused(
                "Application deployment was started for this session; bootstrap cleanup is prohibited"
            )
        try:
            cluster = self.clients["eks"].describe_cluster(name=self.config["cluster_name"])[
                "cluster"
            ]
        except self.clients["eks"].exceptions.ResourceNotFoundException:
            cluster = None
        if cluster is not None:
            if (
                cluster.get("status") not in {"ACTIVE", "FAILED"}
                or cluster.get("arn") != self.context
                or any(
                    cluster.get("tags", {}).get(k) != v
                    for k, v in {
                        "Project": "deepresearch",
                        "Environment": self.config["environment"],
                        "Session": self.config["session_id"],
                    }.items()
                )
            ):
                raise Refused("Partial EKS cluster is not terminal or does not match session")
            if cluster.get("status") == "ACTIVE":
                self.runner(
                    [
                        "aws",
                        "eks",
                        "update-kubeconfig",
                        "--region",
                        self.config["region"],
                        "--name",
                        self.config["cluster_name"],
                        "--alias",
                        self.context,
                        "--role-arn",
                        f"arn:aws:iam::{self.config['account_id']}:role/{self.config['cluster_name']}-cluster-admin",
                    ]
                )
                result = self.runner(
                    [
                        "kubectl",
                        "--context",
                        self.context,
                        "get",
                        "namespace",
                        "research",
                        "-o",
                        "json",
                        "--ignore-not-found=true",
                    ]
                )
                if result.strip():
                    namespace = json.loads(result)
                    if namespace.get("metadata", {}).get("name") != "research":
                        raise Refused("Could not verify bootstrap namespace state")
                    raise Refused(
                        "Research namespace exists; bootstrap cleanup requires app installation never started"
                    )
        try:
            result = self.clients["rds"].describe_db_instances(
                DBInstanceIdentifier=self.config["database_identifier"]
            )["DBInstances"][0]
        except self.clients["rds"].exceptions.DBInstanceNotFoundFault:
            result = None
        if result is not None:
            tags = {
                item["Key"]: item["Value"]
                for item in self.clients["rds"].list_tags_for_resource(
                    ResourceName=result["DBInstanceArn"]
                )["TagList"]
            }
            if (
                result.get("DBInstanceStatus") != "available"
                or result.get("DBInstanceIdentifier") != self.config["database_identifier"]
                or tags.get("Project") != "deepresearch"
                or tags.get("Environment") != self.config["environment"]
                or tags.get("Session") != self.config["session_id"]
            ):
                raise Refused("Database is not available or does not match bootstrap session")
        return {"cluster_present": cluster is not None, "database_present": result is not None}

    def remaining_inventory(self):
        from .inventory import InventoryCollector

        return InventoryCollector(self.config, self.clients).collect()

    def publish_receipt(self, kind, value):
        self.require_lease()
        if kind not in ["backup", "shutdown"]:
            raise Refused("Invalid receipt kind")
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        checksum = hashlib.sha256(payload).hexdigest()
        key = f"sessions/{self.config['environment']}/{self.config['session_id']}/receipts/{kind}.json"
        result = self.clients["s3"].put_object(
            ExpectedBucketOwner=self.config["account_id"],
            Bucket=self.config["backup_bucket"],
            Key=key,
            Body=payload,
            ServerSideEncryption="AES256",
            Metadata={"sha256": checksum, "session": self.config["session_id"]},
        )
        if not result.get("VersionId") or result["VersionId"] == "null":
            raise Refused("Receipt is not durably versioned")
        return {
            "bucket": self.config["backup_bucket"],
            "key": key,
            "version_id": result["VersionId"],
            "sha256": checksum,
        }

    def verify_durable_receipt(self, receipt):
        pointer = receipt.get("durable_backup_receipt") or {}
        expected = f"sessions/{self.config['environment']}/{self.config['session_id']}/receipts/backup.json"
        if (
            pointer.get("bucket") != self.config["backup_bucket"]
            or pointer.get("key") != expected
            or not pointer.get("version_id")
            or pointer["version_id"] == "null"
        ):
            raise Refused("Missing immutable retained backup receipt")
        data = self.clients["s3"].get_object(
            ExpectedBucketOwner=self.config["account_id"],
            Bucket=pointer["bucket"],
            Key=pointer["key"],
            VersionId=pointer["version_id"],
        )["Body"]
        try:
            payload = data.read(16 * 1024 * 1024 + 1)
        finally:
            data.close()
        if len(payload) > 16 * 1024 * 1024 or hashlib.sha256(payload).hexdigest() != pointer.get(
            "sha256"
        ):
            raise Refused("Durable backup receipt checksum mismatch")
        expected_content = {k: v for k, v in receipt.items() if k != "durable_backup_receipt"}
        if canonical(json.loads(payload)) != canonical(expected_content):
            raise Refused("Local receipt differs from retained immutable receipt")

    def verify_snapshot_timing(self, snapshot, receipt):
        created = snapshot["SnapshotCreateTime"]
        created = created if hasattr(created, "tzinfo") else timestamp(created)
        if created < timestamp(receipt["drain"]["verified_at"]) or created > timestamp(
            receipt["created_at"]
        ):
            raise Refused("Snapshot was not created after this quiesce and before receipt issuance")
        if receipt["database_snapshot"].get("created_at") != created.isoformat():
            raise Refused("Snapshot creation-time receipt mismatch")
