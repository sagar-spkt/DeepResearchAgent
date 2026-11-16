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

