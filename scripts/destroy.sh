#!/usr/bin/env bash
# Tear down one environment completely, including all of its data.
# Run by .github/workflows/destroy.yml. Usage: scripts/destroy.sh <staging|production>
set -euo pipefail

ENVIRONMENT="${1:?usage: destroy.sh <staging|production>}"
case "$ENVIRONMENT" in staging|production) ;; *) echo "unknown environment" >&2; exit 2 ;; esac
: "${TF_STATE_BUCKET:?}" "${AWS_REGION:?}"

CLUSTER="deepresearch-$ENVIRONMENT"
NAMESPACE=research
TFVARS="infra/aws/env/$ENVIRONMENT.tfvars"
APP_HOST=$(sed -n 's/^app_host *= *"\(.*\)"/\1/p' "$TFVARS")
ZONE_NAME=$(sed -n 's/^route53_zone_name *= *"\(.*\)"/\1/p' "$TFVARS")

# 1. Remove in-cluster resources that own AWS resources outside Terraform:
#    the ALB (owned by the Ingress) and EBS volumes (owned by PVCs).
if aws eks describe-cluster --name "$CLUSTER" >/dev/null 2>&1; then
  aws eks update-kubeconfig --region "$AWS_REGION" --name "$CLUSTER"
  helm uninstall research -n "$NAMESPACE" --wait --timeout 10m 2>/dev/null || true
  kubectl -n "$NAMESPACE" delete ingress --all --wait=true --timeout=10m 2>/dev/null || true
  helm uninstall research-observability -n "$NAMESPACE" --wait --timeout 10m 2>/dev/null || true
  kubectl -n "$NAMESPACE" delete pvc --all --wait=true --timeout=10m 2>/dev/null || true
  # Wait until the controller has deleted the ALB, then remove the controller.
  for _ in $(seq 60); do
    remaining=$(aws resourcegroupstaggingapi get-resources --resource-type-filters elasticloadbalancing:loadbalancer \
      --tag-filters "Key=elbv2.k8s.aws/cluster,Values=$CLUSTER" --query 'length(ResourceTagMappingList)' --output text)
    [ "$remaining" = 0 ] && break
    echo "waiting for $remaining load balancer(s) to be deleted"
    sleep 10
  done
  helm uninstall aws-load-balancer-controller -n kube-system --wait 2>/dev/null || true
  # Wait for the EBS CSI driver to delete the released volumes.
  for _ in $(seq 60); do
    [ -z "$(kubectl get pv -o name 2>/dev/null)" ] && break
    echo "waiting for persistent volumes to be deleted"
    sleep 10
  done
else
  echo "Cluster $CLUSTER not found; skipping Kubernetes cleanup."
fi

# 2. Remove the DNS record the deploy workflow created.
ZONE_ID=$(aws route53 list-hosted-zones-by-name --dns-name "$ZONE_NAME" --query 'HostedZones[0].Id' --output text)
record=$(aws route53 list-resource-record-sets --hosted-zone-id "$ZONE_ID" \
  --query "ResourceRecordSets[?Name=='$APP_HOST.' && Type=='A'] | [0]" --output json)
if [ "$record" != "null" ]; then
  printf '{"Changes":[{"Action":"DELETE","ResourceRecordSet":%s}]}' "$record" > "${RUNNER_TEMP:-/tmp}/dns-delete.json"
  aws route53 change-resource-record-sets --hosted-zone-id "$ZONE_ID" \
    --change-batch "file://${RUNNER_TEMP:-/tmp}/dns-delete.json" >/dev/null
  echo "Deleted DNS record $APP_HOST"
fi

# 3. Destroy everything Terraform created.
terraform -chdir=infra/aws init -input=false -reconfigure -lockfile=readonly \
  -backend-config="bucket=$TF_STATE_BUCKET" \
  -backend-config="key=deepresearch/$ENVIRONMENT/aws.tfstate"
terraform -chdir=infra/aws destroy -input=false -auto-approve -var-file="env/$ENVIRONMENT.tfvars"

echo "### $ENVIRONMENT destroyed" >> "${GITHUB_STEP_SUMMARY:-/dev/stdout}"
