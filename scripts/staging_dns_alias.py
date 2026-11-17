"""Point the staging hostname at the validated staging ingress ALB."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

HOSTNAME = "staging.research.spktsagar.com."
ZONE_ID = "Z07904421IUDAJDY0WXZ8"
VPC_ID = "vpc-0629f366f13246de0"
CLUSTER_TAG = "elbv2.k8s.aws/cluster"
CLUSTER_NAME = "deepresearch-staging"


class DNSUpdateError(RuntimeError):
    pass


def run_json(command: list[str], runner=subprocess.run) -> dict:
    result = runner(command, check=False, capture_output=True, text=True)
    if result.returncode:
        raise DNSUpdateError("Staging ingress or DNS verification command failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DNSUpdateError("Staging ingress or DNS command returned invalid JSON") from exc


def run_ok(command: list[str], runner=subprocess.run) -> None:
    result = runner(command, check=False, capture_output=True, text=True)
    if result.returncode:
        raise DNSUpdateError("Staging ingress or DNS verification command failed")


def route53_change_batch(load_balancer: dict) -> dict:
    dns_name = load_balancer.get("DNSName", "")
    if (
        load_balancer.get("VpcId") != VPC_ID
        or load_balancer.get("Scheme") != "internet-facing"
        or load_balancer.get("Type") != "application"
        or load_balancer.get("State", {}).get("Code") != "active"
        or not dns_name.endswith(".elb.amazonaws.com")
    ):
        raise DNSUpdateError("Ingress load balancer is not the active public staging ALB")
    hosted_zone = load_balancer.get("CanonicalHostedZoneId")
    if not isinstance(hosted_zone, str) or not hosted_zone:
        raise DNSUpdateError("Ingress ALB is missing its canonical hosted-zone ID")
    return {
        "Comment": "Verified DeepResearch staging ingress",
        "Changes": [
            {
                "Action": "UPSERT",
                "ResourceRecordSet": {
                    "Name": HOSTNAME,
                    "Type": "A",
                    "AliasTarget": {
                        "HostedZoneId": hosted_zone,
                        "DNSName": dns_name
                        if dns_name.startswith("dualstack.")
                        else "dualstack." + dns_name,
                        "EvaluateTargetHealth": True,
                    },
                },
            }
        ],
    }


def update(runner=subprocess.run) -> str:
    ingress = run_json(
        ["kubectl", "-n", "research", "get", "ingress", "research", "-o", "json"], runner
    )
    try:
        ingress_host = ingress["status"]["loadBalancer"]["ingress"][0]["hostname"]
    except (KeyError, IndexError, TypeError) as exc:
        raise DNSUpdateError("Staging ingress has no controller-assigned hostname") from exc
    if not isinstance(ingress_host, str) or not ingress_host.endswith(".elb.amazonaws.com"):
        raise DNSUpdateError("Staging ingress hostname is invalid")

    lbs = run_json(["aws", "elbv2", "describe-load-balancers", "--output", "json"], runner)
    matches = [lb for lb in lbs.get("LoadBalancers", []) if lb.get("DNSName") == ingress_host]
    if len(matches) != 1:
        raise DNSUpdateError("Staging ingress must resolve to exactly one load balancer")
    lb = matches[0]
    tags = (
        run_json(
            [
                "aws",
                "elbv2",
                "describe-tags",
                "--resource-arns",
                lb["LoadBalancerArn"],
                "--output",
                "json",
            ],
            runner,
        )
        .get("TagDescriptions", [{}])[0]
        .get("Tags", [])
    )
    if not any(tag.get("Key") == CLUSTER_TAG and tag.get("Value") == CLUSTER_NAME for tag in tags):
        raise DNSUpdateError("Ingress load balancer is not tagged for this staging cluster")

    batch = route53_change_batch(lb)
    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as target:
            path = Path(target.name)
            target.write(json.dumps(batch))
        path.chmod(0o600)
        result = run_json(
            [
                "aws",
                "route53",
                "change-resource-record-sets",
                "--hosted-zone-id",
                ZONE_ID,
                "--change-batch",
                "file://" + str(path),
                "--output",
                "json",
            ],
            runner,
        )
    finally:
        if path is not None:
            path.unlink(missing_ok=True)
    change_id = result.get("ChangeInfo", {}).get("Id")
    if not isinstance(change_id, str) or not change_id:
        raise DNSUpdateError("Route 53 did not return a change ID")
    run_ok(["aws", "route53", "wait", "resource-record-sets-changed", "--id", change_id], runner)
    return ingress_host


if __name__ == "__main__":
    print("Staging Route 53 alias now targets " + update())
