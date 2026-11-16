variable "availability_zones" {

  type = list(string)
  validation {
    condition     = length(var.availability_zones) == 2
    error_message = "Exactly two AZs are required."
  }


}

variable "administrator_cidrs" {
  type        = list(string)
  nullable    = false
  description = "Distinct IPv4 /32 source addresses allowed to reach the public EKS API, including the session management NAT address and any approved operator address."
  validation {
    condition = length(var.administrator_cidrs) > 0 && length(distinct(var.administrator_cidrs)) == length(var.administrator_cidrs) && alltrue([
      for cidr in var.administrator_cidrs : try(
        cidrnetmask(cidr) == "255.255.255.255" && cidr == "${cidrhost(cidr, 0)}/32",
        false
      )
    ])
    error_message = "EKS public access requires a nonempty, unique list of canonical IPv4 /32 host CIDRs; network ranges, IPv6 and malformed entries are not allowed."
  }
}

variable "operator_principal_arn" {
  type        = string
  description = "Same-account IAM user or role trusted to assume the short-lived EKS administrator role."
  validation {
    condition     = can(regex("^arn:aws:iam::${var.account_id}:(user|role)/.+$", var.operator_principal_arn))
    error_message = "operator_principal_arn must be an IAM user or role ARN in account_id."
  }
}

variable "kubernetes_version" {
  type        = string
  description = "EKS Kubernetes minor version. Use an available standard-support version and verify regional add-on compatibility before apply."
}

variable "session_id" {
  type = string
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,15}$", var.session_id))
    error_message = "Session ID must be at most sixteen safe lowercase letters, digits or hyphens."
  }
}

variable "database_snapshot_identifier" {
  type    = string
  default = null
}

variable "database_instance_class" {
  type    = string
  default = "db.t4g.small"
}

variable "foundation" {

  type = object({
    buckets           = map(string), queue_url = string, queue_arn = string, alert_topic_arn = string
    alert_kms_key_arn = string, secrets = map(string)
    }
  )

}

