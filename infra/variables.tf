variable "region" {
  type    = string
  default = "us-east-1"
}

variable "log_retention_days" {
  description = "Applied to every log group. Don't override per group."
  type        = number
  default     = 7
}
