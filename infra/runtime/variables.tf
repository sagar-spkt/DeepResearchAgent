variable "availability_zones" {

  type = list(string)
  validation {
    condition     = length(var.availability_zones) == 2
    error_message = "Exactly two AZs are required."
  }


}

variable "administrator_cidrs" {
  type = list(string)
}

variable "administrator_role_arn" {
  type = string
}

variable "kubernetes_version" {
  type = string
}

variable "session_id" {
  type = string
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
    buckets = map(string), queue_url = string, queue_arn = string, secrets = map(string)
    }
  )

}

