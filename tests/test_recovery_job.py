import copy
import unittest

from scripts.lifecycle.aws_adapter import AWSAdapter
from scripts.lifecycle.core import Refused, digest, prepare_database_restore
from scripts.lifecycle.recovery_job import run_restore_verifier
from tests.test_database_restore import CONFIG, backup_receipt

BACKUP = backup_receipt()
PLAN = prepare_database_restore(BACKUP)
RESTORE = {
    "operation": "database-restore-receipt",
    "config": CONFIG,
    "plan_hash": digest(PLAN),
    "provider_verified": True,
    "database_restore_verified": True,
    "source_snapshot_arn": PLAN["source_snapshot_arn"],
    "target_database_identifier": PLAN["target_database_identifier"],
    "target_database_arn": f"arn:aws:rds:{CONFIG['region']}:{CONFIG['account_id']}:db:{PLAN['target_database_identifier']}",
    "target_database_endpoint": "restore.abcdefghijkl.us-east-1.rds.amazonaws.com",
    "target_database_port": 5432,
}
USER_EMAIL = "recovery+session-1@example.test"
CONVERSATION_ID = "11111111-1111-4111-8111-111111111111"
QUERY = "blue heron archive"


class FakeAdapter:
    def __init__(self, fail_at=None):
        self.fail_at = fail_at
        self.calls = []
        self.create_count = 0

    def open_restore_verifier_ingress(self, plan, restore_receipt):
        self.calls.append("open")
        return {
            "source_security_group_ids": ["sg-12345678"],
            "source_database_endpoint": "source.abcdefghijkl.us-east-1.rds.amazonaws.com",
        }

    def close_restore_verifier_ingress(self, access):
        self.calls.append("close")
        if self.fail_at == "close":
            raise RuntimeError("close failed")

    def kubectl(self, *args):
        self.calls.append(args[0])
        if args[:3] == ("get", "deployment", "research-api"):
            return '{"spec":{"template":{"spec":{"serviceAccountName":"research-application","containers":[{"image":"registry.test/backend@sha256:' + ('a' * 64) + '","envFrom":[{"secretRef":{"name":"research-app"}}]}]}}}}'
        if args[:2] == ("create", "-f"):
            self.create_count += 1
            if self.fail_at == "create" or self.fail_at == f"create-{self.create_count}":
                raise RuntimeError("create failed")
            return ""
        if args[0] == "wait" and self.fail_at == "wait":
            raise RuntimeError("job failed")
        if args[0] == "exec":
            return '{"operation":"application-recovery-verification-receipt","restore_receipt_sha256":"' + digest(RESTORE) + '","source_database_endpoint":"source.abcdefghijkl.us-east-1.rds.amazonaws.com","target_database_endpoint":"restore.abcdefghijkl.us-east-1.rds.amazonaws.com","fixture_manifest_sha256":"' + ('a' * 64) + '","application_data_verified":true,"retrieval_verified":true}'
        return ""


class RestoreVerifierJobTests(unittest.TestCase):
    def test_job_and_network_access_are_cleaned_after_success(self):
        adapter = FakeAdapter()
        result = run_restore_verifier(adapter, PLAN, BACKUP, RESTORE, USER_EMAIL, CONVERSATION_ID, QUERY)
        self.assertTrue(result["temporary_ingress_removed"])
        self.assertEqual(["get", "open", "create", "create", "wait", "exec", "delete", "delete", "close"], adapter.calls)

    def test_ingress_is_removed_when_job_setup_fails(self):
        adapter = FakeAdapter(fail_at="create")
        with self.assertRaisesRegex(RuntimeError, "create failed"):
            run_restore_verifier(adapter, PLAN, BACKUP, RESTORE, USER_EMAIL, CONVERSATION_ID, QUERY)
        self.assertIn("delete", adapter.calls)
        self.assertEqual("close", adapter.calls[-1])

    def test_job_failure_cleans_job_secret_and_ingress(self):
        adapter = FakeAdapter(fail_at="wait")
        with self.assertRaisesRegex(RuntimeError, "job failed"):
            run_restore_verifier(adapter, PLAN, BACKUP, RESTORE, USER_EMAIL, CONVERSATION_ID, QUERY)
        self.assertEqual(2, adapter.calls.count("delete"))
        self.assertEqual("close", adapter.calls[-1])

    def test_cleanup_failure_is_reported_with_primary_failure(self):
        adapter = FakeAdapter(fail_at="create")
        adapter.close_restore_verifier_ingress = lambda access: (_ for _ in ()).throw(RuntimeError("revoke failed"))
        with self.assertRaisesRegex(Exception, "cleanup failed: could not revoke verifier ingress"):
            run_restore_verifier(adapter, PLAN, BACKUP, RESTORE, USER_EMAIL, CONVERSATION_ID, QUERY)

    def test_artifact_mismatch_fails_before_network_open(self):
        adapter = FakeAdapter()
        with self.assertRaisesRegex(Exception, "does not match its retained backup"):
            run_restore_verifier(adapter, PLAN, {**BACKUP, "extra": True}, RESTORE, USER_EMAIL, CONVERSATION_ID, QUERY)
        self.assertEqual([], adapter.calls)

    def test_non_synthetic_request_fails_before_network_open(self):
        adapter = FakeAdapter()
        with self.assertRaisesRegex(Exception, "reserved synthetic user"):
            run_restore_verifier(adapter, PLAN, BACKUP, RESTORE, "person@example.com", CONVERSATION_ID, QUERY)
        self.assertEqual([], adapter.calls)


class FakeNetwork:
    def __init__(self, plan):
        self.plan = plan
        self.target = {
            "GroupId": "sg-abcd1234",
            "VpcId": "vpc-12345678",
            "Tags": [{"Key": k, "Value": v} for k, v in {**plan["tags"], "Component": "database-restore-security-group"}.items()],
            "IpPermissions": [],
            "IpPermissionsEgress": [],
        }
        self.authorized = []
        self.revoked = []
        self.source_db_access = True
        self.rules = []
        self.lose_authorize_response = False

    def describe_security_groups(self, **kwargs):
        if kwargs.get("GroupIds"):
            return {
                "SecurityGroups": [
                    copy.deepcopy(self.target)
                    if group_id == self.target["GroupId"]
                    else {
                        "GroupId": group_id,
                        "VpcId": "vpc-12345678",
                        "OwnerId": "123456789012",
                        "IpPermissions": (
                            [{"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432,
                              "UserIdGroupPairs": [{"GroupId": "sg-a1111111"}, {"GroupId": "sg-b2222222"}]}]
                            if group_id == "sg-c3333333" and self.source_db_access else []
                        ),
                    }
                    for group_id in kwargs["GroupIds"]
                ]
            }
        return {"SecurityGroups": [copy.deepcopy(self.target)]}

    def describe_instances(self, **kwargs):
        return {"Reservations": [{"Instances": [
                {"InstanceId": "i-11111111", "VpcId": "vpc-12345678", "State": {"Name": "running"}, "SecurityGroups": [{"GroupId": "sg-a1111111"}]},
                {"InstanceId": "i-22222222", "VpcId": "vpc-12345678", "State": {"Name": "running"}, "SecurityGroups": [{"GroupId": "sg-b2222222"}]},
        ]}]}

    def authorize_security_group_ingress(self, **kwargs):
        self.authorized.append(kwargs)
        self.target["IpPermissions"].extend(copy.deepcopy(kwargs["IpPermissions"]))
        rule = {
            "SecurityGroupRuleId": f"sgr-{len(self.authorized):08d}",
            "GroupId": kwargs["GroupId"],
            "IsEgress": False,
            "IpProtocol": "tcp",
            "FromPort": 5432,
            "ToPort": 5432,
            "ReferencedGroupInfo": kwargs["IpPermissions"][0]["UserIdGroupPairs"][0],
            "Description": kwargs["IpPermissions"][0]["UserIdGroupPairs"][0]["Description"],
        }
        self.rules.append(rule)
        if self.lose_authorize_response:
            self.lose_authorize_response = False
            raise RuntimeError("authorize response lost")
        return {"SecurityGroupRules": [rule]}

    def describe_security_group_rules(self, **kwargs):
        return {"SecurityGroupRules": copy.deepcopy(self.rules)}

    def revoke_security_group_ingress(self, **kwargs):
        self.revoked.append(kwargs)
        self.rules = [
            rule
            for rule in self.rules
            if rule["SecurityGroupRuleId"] not in kwargs["SecurityGroupRuleIds"]
        ]
        self.target["IpPermissions"] = []


class FakeEKS:
    def __init__(self, config):
        self.cluster = {"arn": "arn:aws:eks:us-east-1:123456789012:cluster/deepresearch-staging", "status": "ACTIVE", "tags": {"Project": "deepresearch", "Environment": "staging", "Session": config["session_id"]}, "resourcesVpcConfig": {"vpcId": "vpc-12345678"}}

    def describe_cluster(self, **kwargs):
        return {"cluster": self.cluster}

    def describe_nodegroup(self, **kwargs):
        return {"nodegroup": {"clusterName": "deepresearch-staging", "nodegroupName": "cpu", "status": "ACTIVE", "resources": {"autoScalingGroups": [{"name": "asg-cpu"}]}}}


class FakeASG:
    def describe_auto_scaling_groups(self, **kwargs):
        return {
            "AutoScalingGroups": [
                {
                    "Instances": [
                        {"InstanceId": "i-11111111", "LifecycleState": "InService"},
                        {"InstanceId": "i-22222222", "LifecycleState": "InService"},
                    ]
                }
            ]
        }


class FakeSourceRDS:
    source_endpoint = "source.abcdefghijkl.us-east-1.rds.amazonaws.com"

    def describe_db_instances(self, **kwargs):
        if kwargs.get("DBInstanceIdentifier", "").endswith("-restore-session-1"):
            return {
                "DBInstances": [{
                    "DBInstanceIdentifier": "deepresearch-staging-restore-session-1",
                    "DBInstanceArn": "arn:aws:rds:us-east-1:123456789012:db:deepresearch-staging-restore-session-1",
                    "DBInstanceStatus": "available",
                    "PubliclyAccessible": False,
                    "StorageEncrypted": True,
                    "DBSubnetGroup": {"DBSubnetGroupName": "deepresearch-staging"},
                    "VpcSecurityGroups": [{"VpcSecurityGroupId": "sg-abcd1234", "Status": "active"}],
                    "Endpoint": {"Address": "restore.abcdefghijkl.us-east-1.rds.amazonaws.com", "Port": 5432},
                }]
            }
        return {
            "DBInstances": [{
                "DBInstanceIdentifier": "deepresearch-staging",
                "DBInstanceArn": "arn:aws:rds:us-east-1:123456789012:db:deepresearch-staging",
                "DBInstanceStatus": "available",
                "PubliclyAccessible": False,
                "StorageEncrypted": True,
                "DBSubnetGroup": {"VpcId": "vpc-12345678"},
                "VpcSecurityGroups": [{"VpcSecurityGroupId": "sg-c3333333", "Status": "active"}],
                "Endpoint": {"Address": self.source_endpoint, "Port": 5432},
            }]
        }

    def list_tags_for_resource(self, **kwargs):
        if kwargs["ResourceName"].endswith("-restore-session-1"):
            return {"TagList": [{"Key": k, "Value": v} for k, v in PLAN["tags"].items()]}
        return {"TagList": [
            {"Key": "Project", "Value": "deepresearch"},
            {"Key": "Environment", "Value": "staging"},
            {"Key": "Session", "Value": "session-1"},
        ]}


class FakeNetworkAdapter(AWSAdapter):
    def __init__(self, plan):
        self.network = FakeNetwork(plan)
        config = plan["config"]
        clients = {
            "ec2": self.network,
            "eks": FakeEKS(config),
            "autoscaling": FakeASG(),
            "rds": FakeSourceRDS(),
        }
        super().__init__(config, clients)
        self.exclusive_guard = lambda: None

    def verify_private_restore_subnet_group(self, subnet_group_name):
        return "vpc-12345678"


class RestoreVerifierIngressTests(unittest.TestCase):
    def setUp(self):
        self.plan = prepare_database_restore(backup_receipt())
        self.adapter = FakeNetworkAdapter(self.plan)

    def test_opens_only_live_node_security_groups_on_postgres(self):
        access = self.adapter.open_restore_verifier_ingress(self.plan, RESTORE)
        self.assertEqual(["sg-a1111111", "sg-b2222222"], access["source_security_group_ids"])
        self.assertEqual(FakeSourceRDS.source_endpoint, access["source_database_endpoint"])
        self.assertEqual(2, len(self.adapter.network.authorized))
        for request in self.adapter.network.authorized:
            permission = request["IpPermissions"][0]
            self.assertEqual(("tcp", 5432, 5432), (permission["IpProtocol"], permission["FromPort"], permission["ToPort"]))
            self.assertNotIn("IpRanges", permission)

    def test_refuses_cluster_session_mismatch_before_ingress(self):
        self.adapter.clients["eks"].cluster["tags"]["Session"] = "other-session"
        with self.assertRaisesRegex(Refused, "cluster identity"):
            self.adapter.open_restore_verifier_ingress(self.plan, RESTORE)
        self.assertEqual([], self.adapter.network.authorized)

    def test_lease_loss_during_multi_group_open_rolls_back_created_rule(self):
        calls = 0

        def fail_before_second_rule():
            nonlocal calls
            calls += 1
            if calls == 3:
                raise Refused("lease lost")

        self.adapter.exclusive_guard = fail_before_second_rule
        with self.assertRaisesRegex(Refused, "lease lost"):
            self.adapter.open_restore_verifier_ingress(self.plan, RESTORE)
        self.assertEqual(1, len(self.adapter.network.authorized))
        self.assertEqual(["sgr-00000001"], self.adapter.network.revoked[0]["SecurityGroupRuleIds"])
        self.assertEqual([], self.adapter.network.target["IpPermissions"])

    def test_lost_authorize_response_is_found_and_rolled_back_by_description(self):
        self.adapter.network.lose_authorize_response = True
        with self.assertRaisesRegex(RuntimeError, "response lost"):
            self.adapter.open_restore_verifier_ingress(self.plan, RESTORE)
        self.assertEqual(1, len(self.adapter.network.revoked))
        self.assertEqual([], self.adapter.network.target["IpPermissions"])

    def test_refuses_when_paused_source_db_does_not_allow_verifier_nodes(self):
        self.adapter.network.source_db_access = False
        with self.assertRaisesRegex(Refused, "does not admit every verifier node"):
            self.adapter.open_restore_verifier_ingress(self.plan, RESTORE)
        self.assertEqual([], self.adapter.network.authorized)

    def test_refuses_provider_verified_source_endpoint_equal_to_clone(self):
        self.adapter.clients["rds"].source_endpoint = RESTORE["target_database_endpoint"]
        with self.assertRaisesRegex(Refused, "aliases the restored clone"):
            self.adapter.open_restore_verifier_ingress(self.plan, RESTORE)
        self.assertEqual([], self.adapter.network.authorized)

    def test_close_revokes_exact_rule_ids_and_checks_closed_sg(self):
        access = self.adapter.open_restore_verifier_ingress(self.plan, RESTORE)
        self.adapter.close_restore_verifier_ingress(access)
        self.assertEqual(access["security_group_rule_ids"], self.adapter.network.revoked[0]["SecurityGroupRuleIds"])
        self.assertEqual([], self.adapter.network.target["IpPermissions"])


if __name__ == "__main__":
    unittest.main()
