variable "account_id" {
  type        = string
  description = "AWS account that owns the existing GitHub Actions OIDC provider."

  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Specify the intended twelve-digit AWS account explicitly."
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "staging_rds_master_secret_arn" {
  type        = string
  description = "Exact Secrets Manager ARN for the staging RDS managed master password, read by the one-time GitHub deployment bootstrap job."
  validation {
    condition     = can(regex("^arn:aws:secretsmanager:${var.region}:${var.account_id}:secret:rds!db-[A-Za-z0-9-]+$", var.staging_rds_master_secret_arn))
    error_message = "Use the exact RDS-managed staging master secret ARN in the configured account and Region."
  }
}

