# Helm charts

- `research`: API, worker and web Deployments, the DB bootstrap and migration
  hook Jobs, and the ALB Ingress. Uses Amazon Bedrock for the model.
- `observability`: self-hosted Langfuse with ClickHouse and Valkey.

Both are installed by `scripts/deploy.sh`; see `infra/README.md`.
