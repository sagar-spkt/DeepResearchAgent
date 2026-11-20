#!/usr/bin/env bash
# Provision (or update) one environment and deploy the current commit to it.
# Run by .github/workflows/deploy.yml with AWS credentials for the environment's
# deploy role. Usage: scripts/deploy.sh <staging|production>
set -euo pipefail

ENVIRONMENT="${1:?usage: deploy.sh <staging|production>}"
case "$ENVIRONMENT" in staging|production) ;; *) echo "unknown environment" >&2; exit 2 ;; esac
: "${TF_STATE_BUCKET:?}" "${AWS_REGION:?}" "${BOOTSTRAP_OWNER_EMAIL:?}"
RELEASE_ID="${GITHUB_SHA:-$(git rev-parse HEAD)}"
export RELEASE_ID

EMBEDDING_REVISION=5c38ec7c405ec4b44b94cc5a9bb96e735b38267a
LBC_CHART_VERSION=1.14.1
LBC_CHART_SHA256=5b9f5bf3d7295d0c61eda2a7aea8c7f3369d51eca406c3dc032b84eacc3893d3
LBC_IMAGE_TAG=v2.14.1@sha256:13abee6e31c2f25ee91f9ae91aabe4f5bcaf6a70eccd372b36835786d2e0813e
NAMESPACE=research
WORK="${RUNNER_TEMP:-$(mktemp -d)}/deploy-$ENVIRONMENT"
rm -rf "$WORK"
mkdir -m 700 -p "$WORK"
trap 'rm -rf "$WORK/secrets" "$WORK/tf.json"' EXIT

step() { echo "::group::$*" 2>/dev/null || true; echo "== $*"; }
endstep() { echo "::endgroup::" 2>/dev/null || true; }

step "Terraform apply ($ENVIRONMENT)"
terraform -chdir=infra/aws init -input=false -reconfigure -lockfile=readonly \
  -backend-config="bucket=$TF_STATE_BUCKET" \
  -backend-config="key=deepresearch/$ENVIRONMENT/aws.tfstate"
terraform -chdir=infra/aws apply -input=false -auto-approve -var-file="env/$ENVIRONMENT.tfvars"
terraform -chdir=infra/aws output -json > "$WORK/tf.json"
endstep

tf() { python3 -c 'import json,sys; v=json.load(open(sys.argv[1]))[sys.argv[2]]["value"]; print(v if isinstance(v,str) else v[sys.argv[3]])' "$WORK/tf.json" "$@"; }
CLUSTER=$(tf cluster_name)
VPC_ID=$(tf vpc_id)
CERT_ARN=$(tf certificate_arn)
ZONE_ID=$(tf route53_zone_id)
BACKEND_REPO=$(tf ecr_repositories backend)
WEB_REPO=$(tf ecr_repositories web)
APP_URL=$(tf app_config app_url)
APP_HOST=${APP_URL#https://}
BEDROCK_MODEL_ID=$(tf app_config bedrock_model_id)

step "Build and push images"
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "${BACKEND_REPO%%/*}"
docker buildx build --platform linux/amd64 -f Dockerfile.backend --target embeddings \
  --build-arg "EMBEDDING_REVISION=$EMBEDDING_REVISION" \
  -t "$BACKEND_REPO:$RELEASE_ID" --push .
docker buildx build --platform linux/amd64 -f web/Dockerfile \
  -t "$WEB_REPO:$RELEASE_ID" --push web
digest() {
  aws ecr describe-images --repository-name "${1#*/}" --image-ids "imageTag=$RELEASE_ID" \
    --query 'imageDetails[0].imageDigest' --output text
}
BACKEND_IMAGE="$BACKEND_REPO@$(digest "$BACKEND_REPO")"
WEB_IMAGE="$WEB_REPO@$(digest "$WEB_REPO")"
endstep

step "Kubernetes secrets"
aws eks update-kubeconfig --region "$AWS_REGION" --name "$CLUSTER"
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
# Pod Identity binds to this ServiceAccount; keep it stable across releases.
kubectl -n "$NAMESPACE" create serviceaccount research-application --dry-run=client -o yaml | kubectl apply -f -
python3 scripts/render_k8s_secrets.py "$WORK/tf.json" "$WORK/secrets"
apply_secret() {
  kubectl -n "$NAMESPACE" create secret generic "$1" --from-env-file "$2" --dry-run=client -o yaml \
    | kubectl apply -f - >/dev/null
  echo "applied secret $1"
}
apply_secret research-application "$WORK/secrets/application.env"
apply_secret research-web-env "$WORK/secrets/web.env"
apply_secret research-observability "$WORK/secrets/observability.env"
apply_secret research-db-bootstrap "$WORK/secrets/bootstrap.env"
OBSERVABILITY_REVISION=$(sha256sum "$WORK/secrets/observability.env" | cut -d' ' -f1)
endstep

step "AWS Load Balancer Controller"
helm pull aws-load-balancer-controller --repo https://aws.github.io/eks-charts \
  --version "$LBC_CHART_VERSION" --destination "$WORK"
echo "$LBC_CHART_SHA256  $WORK/aws-load-balancer-controller-$LBC_CHART_VERSION.tgz" | sha256sum -c -
helm upgrade --install aws-load-balancer-controller "$WORK/aws-load-balancer-controller-$LBC_CHART_VERSION.tgz" \
  --namespace kube-system --wait --timeout 10m \
  --set clusterName="$CLUSTER" --set region="$AWS_REGION" --set vpcId="$VPC_ID" \
  --set serviceAccount.create=true --set serviceAccount.name=aws-load-balancer-controller \
  --set replicaCount=1 --set-string image.tag="$LBC_IMAGE_TAG"
endstep

step "Application"
helm upgrade --install research helm/research \
  --namespace "$NAMESPACE" --wait --timeout 20m \
  --set environment="$ENVIRONMENT" \
  --set-string backendImage="$BACKEND_IMAGE" --set-string webImage="$WEB_IMAGE" \
  --set-string model.bedrockModelId="$BEDROCK_MODEL_ID" \
  --set ingress.enabled=true --set-string ingress.host="$APP_HOST" \
  --set-string ingress.certificateArn="$CERT_ARN"
endstep

step "Langfuse"
helm upgrade --install research-observability helm/observability \
  --namespace "$NAMESPACE" --wait --timeout 15m \
  --set environment="$ENVIRONMENT" --set-string secretRevision="$OBSERVABILITY_REVISION"
endstep

step "DNS"
ALB_DNS=""
for _ in $(seq 60); do
  ALB_DNS=$(kubectl -n "$NAMESPACE" get ingress research -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' || true)
  [ -n "$ALB_DNS" ] && break
  sleep 10
done
[ -n "$ALB_DNS" ] || { echo "Ingress never received a load balancer" >&2; exit 1; }
ALB_ZONE=$(aws elbv2 describe-load-balancers --query "LoadBalancers[?DNSName=='$ALB_DNS'].CanonicalHostedZoneId | [0]" --output text)
cat > "$WORK/dns.json" <<EOF
{"Changes":[{"Action":"UPSERT","ResourceRecordSet":{"Name":"$APP_HOST","Type":"A",
 "AliasTarget":{"HostedZoneId":"$ALB_ZONE","DNSName":"dualstack.$ALB_DNS","EvaluateTargetHealth":false}}}]}
EOF
change_id=$(aws route53 change-resource-record-sets --hosted-zone-id "$ZONE_ID" --change-batch "file://$WORK/dns.json" \
  --query 'ChangeInfo.Id' --output text)
aws route53 wait resource-record-sets-changed --id "$change_id"
echo "$APP_HOST -> $ALB_DNS"
endstep

step "Bootstrap owner"
# Cloud sign-ups are never auto-promoted. Once BOOTSTRAP_OWNER_EMAIL has signed
# up and verified in Cognito, make that verified identity the first owner.
# Re-run Deploy after the first sign-up; later runs are no-ops.
POOL_ID=$(tf app_config cognito_issuer); POOL_ID=${POOL_ID##*/}
OWNER_SUB=$(aws cognito-idp list-users --user-pool-id "$POOL_ID" \
  --filter "email = \"$BOOTSTRAP_OWNER_EMAIL\"" \
  --query 'Users[?UserStatus==`CONFIRMED`] | [0].Attributes[?Name==`sub`].Value | [0]' --output text)
if [ -z "$OWNER_SUB" ] || [ "$OWNER_SUB" = None ]; then
  echo "Owner has not signed up yet; sign up as $BOOTSTRAP_OWNER_EMAIL and re-run Deploy."
elif result=$(kubectl -n "$NAMESPACE" exec deploy/research-api -- python -m research_app.manage \
    bootstrap-owner --subject "$OWNER_SUB" --email "$BOOTSTRAP_OWNER_EMAIL" 2>&1); then
  echo "$result"
else
  echo "$result" | grep -q "Owner already exists" || { echo "$result" >&2; exit 1; }
  echo "Owner already exists."
fi
endstep

step "Smoke tests"
kubectl -n "$NAMESPACE" exec deploy/research-api -- python -c \
  'import urllib.request; r=urllib.request.urlopen("http://127.0.0.1:8000/readyz", timeout=10); assert r.status == 200; print("api readyz ok")'
kubectl -n "$NAMESPACE" exec deploy/research-worker -- python -c '
import boto3, os
r = boto3.client("bedrock-runtime", region_name=os.environ["AWS_REGION"]).converse(
    modelId=os.environ["BEDROCK_MODEL_ID"],
    messages=[{"role": "user", "content": [{"text": "Reply with OK"}]}],
    inferenceConfig={"maxTokens": 5})
print("bedrock ok:", r["output"]["message"]["content"][0]["text"])'
kubectl -n "$NAMESPACE" exec deploy/research-api -- python -c \
  'import urllib.request; print("langfuse:", urllib.request.urlopen("http://langfuse:3000/api/public/health", timeout=10).read().decode())'
ok=""
for _ in $(seq 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$APP_URL/" || true)
  case "$code" in 200|302|303|307) ok=1; break ;; esac
  sleep 10
done
[ -n "$ok" ] || { echo "$APP_URL did not respond successfully (last: $code)" >&2; exit 1; }
echo "web ok: $APP_URL (HTTP $code)"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://$APP_HOST/" || true)
[ "$code" = 301 ] || { echo "HTTP did not redirect to HTTPS (got $code)" >&2; exit 1; }
endstep

{
  echo "### $ENVIRONMENT deployed"
  echo "- URL: $APP_URL"
  echo "- Commit: \`$RELEASE_ID\`"
  echo "- Backend: \`$BACKEND_IMAGE\`"
  echo "- Web: \`$WEB_IMAGE\`"
} >> "${GITHUB_STEP_SUMMARY:-/dev/stdout}"
