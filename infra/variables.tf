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
  description = "The Claude Console's IDs for the runtime's federation rule, the agent's only way to sign in."
  type = object({
    organization_id    = string
    service_account_id = string
    rule_id            = string
  })
  nullable  = false
  sensitive = true

  # Required, so a missing secret fails the plan, and a typo too: either way
  # the runtime that works keeps running.
  validation {
    condition = (
      can(regex("^(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", var.anthropic_federation.organization_id))
      && startswith(var.anthropic_federation.service_account_id, "svac_")
      && startswith(var.anthropic_federation.rule_id, "fdrl_")
    )
    error_message = "anthropic_federation takes the organization's UUID, a service account ID (svac_), and a rule ID (fdrl_)."
  }
}

# The GitHub App's client ID and its installation's ID on this repository.
# Neither signs anything (the key stays in KMS), but they name the App, so they
# stay out of the repo and the public CI logs, as the federation IDs do: CI
# passes them from the REMEDIATION_APP secret, a local plan from a gitignored
# infra/remediation_app.auto.tfvars.
variable "remediation_app" {
  description = "The GitHub App's client ID and installation ID, for the remediation flow."
  type = object({
    client_id       = string
    installation_id = number
  })
  nullable  = false
  sensitive = true

  # Required, so a missing secret fails the plan before anything changes.
  validation {
    condition = (
      can(regex("^Iv[0-9A-Za-z.]{10,40}$", var.remediation_app.client_id))
      && var.remediation_app.installation_id >= 1
      && floor(var.remediation_app.installation_id) == var.remediation_app.installation_id
    )
    error_message = "remediation_app takes the App's client ID (Iv...) and its installation's numeric ID."
  }
}
