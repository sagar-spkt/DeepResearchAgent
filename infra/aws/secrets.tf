# Generated credentials live only in the encrypted Terraform state and in
# Kubernetes Secrets created by the deploy workflow. Only random_password is
# used so that no value appears as a resource ID in plan/apply logs.

resource "random_password" "database" {
  for_each = toset(["master", "app", "langfuse"])
  length   = 32
  special  = false
}

resource "random_password" "generated" {
  for_each = toset([
    "web_session", "nextauth", "salt", "clickhouse", "langfuse_admin",
    "langfuse_encryption", "langfuse_public_key", "langfuse_secret_key",
  ])
  length  = 40
  special = false
}
