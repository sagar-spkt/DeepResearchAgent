# Observability chart

Self-hosted Langfuse (web + worker) with single-node ClickHouse and Valkey on
encrypted gp3 volumes (reclaim policy `Delete`, so tearing down an environment
removes them). PostgreSQL is the environment's RDS instance, using a separate
`langfuse` database/user created by the research chart's bootstrap job.

The deploy workflow creates the `research-observability` Secret from Terraform
outputs before installing this chart. Image digests are pinned in
`values.yaml`. Langfuse has no public route; reach it with
`kubectl -n research port-forward svc/langfuse 3000:3000`.
