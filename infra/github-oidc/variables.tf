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

variable "staging_state_bucket" {
  type        = string
  description = "Existing versioned private Terraform state bucket used by staging runtime and management roots."
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.staging_state_bucket))
    error_message = "Specify the exact existing staging Terraform state bucket."
  }
}

variable "staging_runner_ami_id" {
  type        = string
  description = "Exact immutable x86_64 runner base AMI permitted by the staging management provisioner."
  validation {
    condition     = can(regex("^ami-[0-9a-f]{8,17}$", var.staging_runner_ami_id))
    error_message = "Specify the exact approved runner AMI ID."
  }
}


variable "enable_staging_database_bootstrap" {
  type        = bool
  default     = false
  description = "Bind staging deployment to the current managed master secret of the deepresearch-staging RDS instance during database bootstrap."
}
