variable "region" {
  type    = string
  default = "us-east-1"
}

variable "log_retention_days" {
  description = "Applied to every log group. Don't override per group."
  type        = number
  default     = 7
}

variable "traffic_enabled" {
  description = "Run checkout-api's baseline traffic: 12 requests a minute, ~$3.70/month for the generator."
  type        = bool
  default     = true
}
