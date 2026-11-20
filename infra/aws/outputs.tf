output "cluster_name" {
  value = aws_eks_cluster.main.name
}

output "vpc_id" {
  value = aws_vpc.main.id
}

output "route53_zone_id" {
  value = data.aws_route53_zone.main.zone_id
}

output "certificate_arn" {
  value = aws_acm_certificate_validation.app.certificate_arn
}

output "ecr_repositories" {
  value = { for k, v in aws_ecr_repository.images : k => v.repository_url }
}

# Everything the deploy workflow needs to render Kubernetes Secrets.
output "app_config" {
  sensitive = true
  value = {
    app_url              = "https://${var.app_host}"
    region               = var.region
    bedrock_model_id     = var.bedrock_model_id
    documents_bucket     = aws_s3_bucket.data["documents"].id
    traces_bucket        = aws_s3_bucket.data["traces"].id
    queue_url            = aws_sqs_queue.research.url
    cognito_issuer       = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.users.id}"
    cognito_client_id    = aws_cognito_user_pool_client.web.id
    cognito_domain       = "https://${aws_cognito_user_pool_domain.users.domain}.auth.${var.region}.amazoncognito.com"
    database_host        = aws_db_instance.main.address
    database_port        = tostring(aws_db_instance.main.port)
    database_master_user = aws_db_instance.main.username
    database_master_pw   = random_password.database["master"].result
    database_app_pw      = random_password.database["app"].result
    database_langfuse_pw = random_password.database["langfuse"].result
    web_session_secret   = random_password.generated["web_session"].result
    langfuse_nextauth    = random_password.generated["nextauth"].result
    langfuse_salt        = random_password.generated["salt"].result
    langfuse_encryption  = sha256(random_password.generated["langfuse_encryption"].result)
    langfuse_admin_pw    = random_password.generated["langfuse_admin"].result
    langfuse_public_key  = "pk-lf-${lower(random_password.generated["langfuse_public_key"].result)}"
    langfuse_secret_key  = "sk-lf-${lower(random_password.generated["langfuse_secret_key"].result)}"
    clickhouse_password  = random_password.generated["clickhouse"].result
  }
}
