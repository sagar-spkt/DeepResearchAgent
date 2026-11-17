import json
import subprocess
import unittest
from pathlib import Path

from scripts.staging_dns_alias import DNSUpdateError, route53_change_batch, update


class StagingDNSAliasTests(unittest.TestCase):
    def test_alias_targets_only_active_public_staging_alb(self):
        lb = {
            "DNSName": "dualstack.staging-alb-123.us-east-1.elb.amazonaws.com",
            "CanonicalHostedZoneId": "Z35SXDOTRQ7X7K",
            "VpcId": "vpc-0629f366f13246de0",
            "Scheme": "internet-facing",
            "Type": "application",
            "State": {"Code": "active"},
        }
        batch = route53_change_batch(lb)
        record = batch["Changes"][0]["ResourceRecordSet"]
        self.assertEqual("staging.research.spktsagar.com.", record["Name"])
        self.assertEqual("A", record["Type"])
        self.assertEqual("UPSERT", batch["Changes"][0]["Action"])
        self.assertEqual(lb["DNSName"], record["AliasTarget"]["DNSName"])

    def test_wrong_or_internal_load_balancer_is_refused(self):
        lb = {
            "DNSName": "internal-staging-alb.us-east-1.elb.amazonaws.com",
            "CanonicalHostedZoneId": "zone",
            "VpcId": "vpc-0629f366f13246de0",
            "Scheme": "internal",
            "Type": "application",
            "State": {"Code": "active"},
        }
        with self.assertRaisesRegex(DNSUpdateError, "not the active public staging ALB"):
            route53_change_batch(lb)

    def test_update_requires_cluster_tag_before_route53_write(self):
        calls = []
        load_balancer = {
            "DNSName": "staging-alb.us-east-1.elb.amazonaws.com",
            "CanonicalHostedZoneId": "Z35SXDOTRQ7X7K",
            "VpcId": "vpc-0629f366f13246de0",
            "Scheme": "internet-facing",
            "Type": "application",
            "State": {"Code": "active"},
            "LoadBalancerArn": "arn:aws:elasticloadbalancing:us-east-1:088759500358:loadbalancer/app/staging/123",
        }

        def runner(command, **kwargs):
            calls.append(command)
            if command[0] == "kubectl":
                value = {
                    "status": {
                        "loadBalancer": {"ingress": [{"hostname": load_balancer["DNSName"]}]}
                    }
                }
            elif command[1:3] == ["elbv2", "describe-load-balancers"]:
                value = {"LoadBalancers": [load_balancer]}
            elif command[1:3] == ["elbv2", "describe-tags"]:
                value = {
                    "TagDescriptions": [
                        {"Tags": [{"Key": "elbv2.k8s.aws/cluster", "Value": "another-cluster"}]}
                    ]
                }
            else:
                self.fail("Route 53 must not be changed for an unverified load balancer")
            return subprocess.CompletedProcess(command, 0, json.dumps(value), "")

        with self.assertRaisesRegex(DNSUpdateError, "not tagged"):
            update(runner)
        self.assertFalse(
            any(command[1:3] == ["route53", "change-resource-record-sets"] for command in calls)
        )

    def test_update_upserts_only_the_verified_staging_alias_and_waits(self):
        calls = []
        dns = "staging-alb.us-east-1.elb.amazonaws.com"
        lb = {
            "DNSName": dns,
            "CanonicalHostedZoneId": "Z35SXDOTRQ7X7K",
            "VpcId": "vpc-0629f366f13246de0",
            "Scheme": "internet-facing",
            "Type": "application",
            "State": {"Code": "active"},
            "LoadBalancerArn": "arn:aws:elasticloadbalancing:us-east-1:088759500358:loadbalancer/app/staging/123",
        }

        def runner(command, **kwargs):
            calls.append(command)
            if command[0] == "kubectl":
                value = {"status": {"loadBalancer": {"ingress": [{"hostname": dns}]}}}
            elif command[1:3] == ["elbv2", "describe-load-balancers"]:
                value = {"LoadBalancers": [lb]}
            elif command[1:3] == ["elbv2", "describe-tags"]:
                value = {
                    "TagDescriptions": [
                        {
                            "Tags": [
                                {"Key": "elbv2.k8s.aws/cluster", "Value": "deepresearch-staging"}
                            ]
                        }
                    ]
                }
            elif command[1:3] == ["route53", "change-resource-record-sets"]:
                batch_path = command[command.index("--change-batch") + 1].removeprefix("file://")
                batch = json.loads(Path(batch_path).read_text())
                record = batch["Changes"][0]["ResourceRecordSet"]
                self.assertEqual("staging.research.spktsagar.com.", record["Name"])
                self.assertEqual("dualstack." + dns, record["AliasTarget"]["DNSName"])
                self.assertEqual(
                    "Z07904421IUDAJDY0WXZ8", command[command.index("--hosted-zone-id") + 1]
                )
                value = {"ChangeInfo": {"Id": "/change/C123"}}
            elif command[1:3] == ["route53", "wait"]:
                self.assertEqual("/change/C123", command[command.index("--id") + 1])
                return subprocess.CompletedProcess(command, 0, "", "")
            else:
                self.fail("Unexpected AWS or Kubernetes request")
            return subprocess.CompletedProcess(command, 0, json.dumps(value), "")

        self.assertEqual(dns, update(runner))
        self.assertTrue(any(command[1:3] == ["route53", "wait"] for command in calls))


if __name__ == "__main__":
    unittest.main()
