variable "name" {
  description = "Function, role, and log group name."
  type        = string

  validation {
    condition     = startswith(var.name, "sentinelops-")
    error_message = "The deploy role may only create roles named sentinelops-*."
  }
}

variable "description" {
  type    = string
  default = null
}

variable "source_dir" {
  description = "Directory zipped as the deployment package."
  type        = string
}

variable "excludes" {
  description = "Paths in source_dir to leave out of the package: tests, lockfiles, local tooling."
  type        = list(string)
  default     = []
}

variable "handler" {
  type    = string
  default = "handler.handler"
}

variable "memory_size" {
  type    = number
  default = 128
}

variable "timeout" {
  type    = number
  default = 5
}

variable "reserved_concurrent_executions" {
  description = "The most instances that may run at once, reserved from the account's quota; -1 for no limit of its own."
  type        = number
  default     = -1
}

variable "environment" {
  type    = map(string)
  default = {}
}

variable "policy_json" {
  description = "Permissions beyond writing to its own log group, as an IAM policy document."
  type        = string
  default     = null
}

variable "build_dir" {
  type = string
}

variable "log_retention_days" {
  type = number
}

variable "permissions_boundary_arn" {
  type = string
}

variable "assume_role_policy_json" {
  type = string
}
