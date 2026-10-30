# The trigger: the one way an alarm reaches the agent. The rule sends each of
# checkout-api's alarms going into ALARM or back to OK to the trigger Lambda,
# which triages it in code; for a new incident it takes the lock and a slot in
# the hourly cap of 20, runs one investigation on the Runtime and waits for it,
# then stops the session and stores the answer. See trigger/trigger.py.
#
# KILL SWITCH: aws events disable-rule --name sentinelops-alarms. Terraform
# ignores the rule's on/off state, so a deploy never turns a killed agent back
# on. A run already going ends within its guard's 300 s, or at once with
# stop-runtime-session: its session ID is on the incident and in the log.

locals {
  trigger_dir = "${path.module}/../trigger"
  # Everything in trigger/ but its four modules.
  trigger_tooling = ["pyproject.toml", "uv.lock", "tests", ".venv", ".pytest_cache", "**/__pycache__"]
}

# Each service's triage state, alarm changes, and incidents with their
# investigations, and the global lock and hourly cap: trigger/store.py. Only
# the lock has an expires_at, and its condition ignores a lock past it; TTL
# only deletes it some time later.
resource "aws_dynamodb_table" "incidents" {
  name         = "sentinelops-incidents"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

module "trigger" {
  source = "./modules/python_lambda"

  name        = "sentinelops-trigger"
  description = "Triages checkout-api's alarms, and starts one investigation per incident"
  source_dir  = local.trigger_dir
  excludes    = local.trigger_tooling
  # It waits up to 370 s for the Runtime, whose sessions end by 360 s, and the
  # lock's 420 s outlasts it (trigger/tests/test_timeouts.py).
  timeout = 400

  environment = {
    INCIDENTS_TABLE   = aws_dynamodb_table.incidents.name
    AGENT_RUNTIME_ARN = aws_bedrockagentcore_agent_runtime.agent.agent_runtime_arn
  }
  policy_json = data.aws_iam_policy_document.trigger.json

  build_dir                = "${path.module}/.build"
  log_retention_days       = var.log_retention_days
  permissions_boundary_arn = data.aws_iam_policy.workload_boundary.arn
  assume_role_policy_json  = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "trigger" {
  # A transaction's writes are authorized as the PutItems they are.
  statement {
    sid       = "Incidents"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"]
    resources = [aws_dynamodb_table.incidents.arn]
  }

  # AgentCore authorizes an invoke against the runtime and its endpoint both.
  statement {
    sid     = "RunTheAgent"
    actions = ["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:StopRuntimeSession"]
    resources = [
      aws_bedrockagentcore_agent_runtime.agent.agent_runtime_arn,
      "${aws_bedrockagentcore_agent_runtime.agent.agent_runtime_arn}/runtime-endpoint/DEFAULT",
    ]
  }
}

# Lambda never retries the trigger, so a failed one can't start a second
# investigation; its error is in its log. An event that can't start within 5
# minutes is dropped, and an incident it would have closed closes at 60.
resource "aws_lambda_function_event_invoke_config" "trigger" {
  function_name                = module.trigger.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 300
}

# Disabled, it's the kill switch: nothing else starts an investigation.
resource "aws_cloudwatch_event_rule" "alarms" {
  name        = "sentinelops-alarms"
  description = "checkout-api's alarms going into ALARM or back to OK, to the trigger. Disable it to stop the agent."
  event_pattern = jsonencode({
    source        = ["aws.cloudwatch"]
    "detail-type" = ["CloudWatch Alarm State Change"]
    detail = {
      alarmName = [{ prefix = "sentinelops-checkout-" }]
      state     = { value = ["ALARM", "OK"] }
    }
  })
  state = "ENABLED"

  # Set on create only, so a deploy leaves a disabled rule disabled.
  lifecycle {
    ignore_changes = [state, is_enabled]
  }
}

# EventBridge retries a failed delivery for up to 5 minutes. A change delivered
# twice is one triage already has, and does nothing the second time.
resource "aws_cloudwatch_event_target" "trigger" {
  rule      = aws_cloudwatch_event_rule.alarms.name
  target_id = "trigger"
  arn       = module.trigger.function_arn

  retry_policy {
    maximum_event_age_in_seconds = 300
    maximum_retry_attempts       = 20
  }
}

resource "aws_lambda_permission" "alarms" {
  statement_id  = "AllowAlarmsRule"
  action        = "lambda:InvokeFunction"
  function_name = module.trigger.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.alarms.arn
}
