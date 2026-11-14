variable "domain" {
  type = string
}

variable "callback_urls" {
  type = list(string)
}

resource "aws_s3_bucket" "data" {

  for_each      = toset(["documents", "backups", "traces"])
  bucket        = "${local.name}-${each.key}-${data.aws_caller_identity.current.account_id}"
  force_destroy = false
  lifecycle {
    prevent_destroy = true
  }


}

resource "aws_s3_bucket_public_access_block" "data" {

  for_each                = aws_s3_bucket.data
  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true

}

resource "aws_s3_bucket_versioning" "data" {

  for_each = aws_s3_bucket.data
  bucket   = each.value.id
  versioning_configuration {
    status = "Enabled"
  }


}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {

  for_each = aws_s3_bucket.data
  bucket   = each.value.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }

  }


}

resource "aws_s3_bucket_policy" "tls" {

  for_each = aws_s3_bucket.data
  bucket   = each.value.id
  policy = jsonencode({
    Version = "2012-10-17", Statement = [{
      Effect = "Deny", Principal = "*", Action = "s3:*", Resource = [each.value.arn, "${each.value.arn}/*"], Condition = {
        Bool = {
          "aws:SecureTransport" = "false"
        }

      }

      }
    ]
    }
  )

}

resource "aws_ecr_repository" "images" {

  for_each             = toset(["backend", "web"])
  name                 = "${local.name}/${each.key}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }

  lifecycle {
    prevent_destroy = true
  }


}

resource "aws_secretsmanager_secret" "application" {

  for_each                = toset(["application", "vllm", "langfuse"])
  name                    = "${local.name}/${each.key}"
  recovery_window_in_days = 30
  lifecycle {
    prevent_destroy = true
  }


}

resource "aws_sqs_queue" "dead" {

  name                      = "${local.name}-dead"
  sqs_managed_sse_enabled   = true
  message_retention_seconds = 1209600

}

resource "aws_sqs_queue" "research" {

  name                       = "${local.name}-research"
  sqs_managed_sse_enabled    = true
  visibility_timeout_seconds = 960
  receive_wait_time_seconds  = 20
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dead.arn, maxReceiveCount = 3
    }
  )

}

resource "aws_cognito_user_pool" "users" {

  name                     = local.name
  deletion_protection      = "ACTIVE"
  auto_verified_attributes = ["email"]
  username_attributes      = ["email"]
  password_policy {
    minimum_length    = 12
    require_lowercase = true
    require_uppercase = true
    require_numbers   = true
    require_symbols   = true
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }

  }

  lifecycle {
    prevent_destroy = true
  }


}

resource "aws_cognito_user_pool_client" "web" {

  name                                 = "web"
  user_pool_id                         = aws_cognito_user_pool.users.id
  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = var.callback_urls
  logout_urls                          = ["https://${var.domain}"]
  prevent_user_existence_errors        = "ENABLED"
  explicit_auth_flows                  = ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]

}

resource "aws_cognito_user_pool_domain" "users" {

  domain       = "${local.name}-${data.aws_caller_identity.current.account_id}"
  user_pool_id = aws_cognito_user_pool.users.id

}

output "foundation" {

  value = {

    buckets = {
      for k, v in aws_s3_bucket.data : k => v.id
    }

    queue_url = aws_sqs_queue.research.url
    queue_arn = aws_sqs_queue.research.arn
    secrets = {
      for k, v in aws_secretsmanager_secret.application : k => v.arn
    }

    user_pool_id = aws_cognito_user_pool.users.id
    client_id    = aws_cognito_user_pool_client.web.id
    repositories = {
      for k, v in aws_ecr_repository.images : k => v.repository_url
    }


  }


}

