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

# They grant nothing without a JWT that AWS signed for a principal the rule
# names, but they're account-specific, so they stay out of the repo and out of
# the public CI logs: CI passes them from the ANTHROPIC_FEDERATION secret, and
# a local plan from a gitignored infra/anthropic.auto.tfvars.
variable "anthropic_federation" {
  description = "The Claude Console's IDs for the runtime's federation rule. Null: the agent reads the API key."
  type = object({
    organization_id    = string
    service_account_id = string
    rule_id            = string
  })
  default   = null
  sensitive = true

  # A typo fails the plan, so the runtime that works keeps running.
  validation {
    condition = var.anthropic_federation == null ? true : (
      can(regex("^(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", var.anthropic_federation.organization_id))
      && startswith(var.anthropic_federation.service_account_id, "svac_")
      && startswith(var.anthropic_federation.rule_id, "fdrl_")
    )
    error_message = "anthropic_federation takes the organization's UUID, a service account ID (svac_), and a rule ID (fdrl_)."
  }
}
