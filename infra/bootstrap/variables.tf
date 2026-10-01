variable "region" {
  type    = string
  default = "us-east-1"
}

variable "budget_email" {
  description = "Where budget alerts go. Set in terraform.tfvars (gitignored)."
  type        = string
}

variable "github_repo" {
  description = "owner/name of the repo allowed to deploy via OIDC."
  type        = string
  default     = "BigMuscleSmoe/sentinelops"
}

variable "activate_cost_tag" {
  description = "Set true ~24h after the first apply, once AWS has seen the cost_center tag."
  type        = bool
  default     = false
}
