#!/usr/bin/env bash
# Prepare the cluster-scoped prerequisites for the namespaced GitHub deployer.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
session=${1:?Provide the current staging session ID}
[[ "$session" =~ ^[a-z0-9][a-z0-9-]{0,15}$ ]]
export AWS_REGION=us-east-1
account=088759500358
cluster=deepresearch-staging
test "$(aws sts get-caller-identity --query Account --output text)" = "$account"
test "$(aws eks describe-cluster --name "$cluster" --query 'cluster.tags.[Project,Environment,Session]' --output text)" = "$(printf 'deepresearch\tstaging\t%s' "$session")"
for addon in aws-ebs-csi-driver eks-pod-identity-agent; do
  test "$(aws eks describe-addon --cluster-name "$cluster" --addon-name "$addon" --query addon.status --output text)" = ACTIVE
done
private_dir=$(mktemp -d)
export KUBECONFIG="$private_dir/kubeconfig"
lease() {
  python3 scripts/staging_lifecycle_lease.py "$1" --session-id "$session" --owner-file "$private_dir/owner"
}
cleanup() {
  if [[ -f "$private_dir/owner" ]]; then lease release || echo "The staging lease could not be released and will expire." >&2; fi
  rm -rf "$private_dir"
}
trap cleanup EXIT
aws eks update-kubeconfig --name "$cluster" --role-arn "arn:aws:iam::$account:role/$cluster-cluster-admin" --kubeconfig "$KUBECONFIG"
helm template research-observability helm/observability --namespace research \
  -f deploy/staging/observability-images.json --set environment=staging --set-string "sessionId=$session" \
  --show-only templates/storageclass.yaml > "$private_dir/storage.yaml"
kubectl create namespace research --dry-run=client -o yaml > "$private_dir/namespace.yaml"
lease acquire
for manifest in namespace storage; do
  lease assert
  kubectl --request-timeout=15s apply --server-side --field-manager=staging-bootstrap -f "$private_dir/$manifest.yaml"
done
