# The Anthropic API key the agent uses until Bedrock has quota. It was made by
# hand on 2026-10-27; this brings the secret, not its value, under Terraform.
# There is no aws_secretsmanager_secret_version, so the key never enters a
# plan or the state. It's set and rotated by hand, never through Terraform.
resource "aws_secretsmanager_secret" "anthropic_api_key" {
  name        = "sentinelops/anthropic-api-key"
  description = "Anthropic API key for the SentinelOps agent. The value is set by hand, never through Terraform."

  lifecycle {
    prevent_destroy = true
  }
}

# Adopts the hand-made secret once. The import ID is its ARN, looked up by name
# when planning, so the account ID stays out of the repo. After the first
# apply both blocks do nothing, and can go.
data "aws_secretsmanager_secret" "anthropic_api_key" {
  name = "sentinelops/anthropic-api-key"
}

import {
  to = aws_secretsmanager_secret.anthropic_api_key
  id = data.aws_secretsmanager_secret.anthropic_api_key.arn
}
