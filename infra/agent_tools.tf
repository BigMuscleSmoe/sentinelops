# The agent's tools, deployed: one Lambda each, behind an AgentCore Gateway that
# serves them as MCP tools. Each function's role reads only what its tool reads,
# and only the gateway's role may call them.

locals {
  tools_dir = "${path.module}/../tools"
  # Everything in tools/ but the package. The tests hold the recorded windows,
  # megabytes of the live stack the functions never read.
  tools_tooling = ["pyproject.toml", "uv.lock", "tests", ".venv", ".pytest_cache", "**/__pycache__"]

  # Each Gateway target by name, with the tool its function runs. Gateway names
  # the tools <target>___<tool>.
  tool_targets = {
    metrics = "get_metrics"
    logs    = "get_recent_logs"
    deploys = "get_deploy_history"
  }

  # The tools as the model sees them, in Gateway's schema: agent/gateway_tools.py
  # writes the file, and a test keeps it in step with the agent's tool specs.
  gateway_tools = { for t in jsondecode(file("${path.module}/gateway_tools.json")) : t.name => t }

  tool_policies = {
    metrics = data.aws_iam_policy_document.tool_metrics.json
    logs    = data.aws_iam_policy_document.tool_logs.json
    deploys = data.aws_iam_policy_document.tool_deploys.json
  }
}

module "tool" {
  source   = "./modules/python_lambda"
  for_each = local.tool_targets

  name        = "sentinelops-tool-${each.key}"
  description = "The agent's ${each.value} tool, behind the tools gateway"
  source_dir  = local.tools_dir
  excludes    = local.tools_tooling
  handler     = "sentinelops_tools.handlers.${each.value}"
  memory_size = 256
  # A log search can step back through three hours in a dozen FilterLogEvents
  # calls, each seconds long on a busy window; the others make one or two calls.
  timeout     = each.key == "logs" ? 60 : 30
  policy_json = local.tool_policies[each.key]

  build_dir                = "${path.module}/.build"
  log_retention_days       = var.log_retention_days
  permissions_boundary_arn = data.aws_iam_policy.workload_boundary.arn
  assume_role_policy_json  = data.aws_iam_policy_document.lambda_assume.json
}

# GetMetricData can't be scoped to a namespace or a metric.
data "aws_iam_policy_document" "tool_metrics" {
  statement {
    sid       = "ReadMetrics"
    actions   = ["cloudwatch:GetMetricData"]
    resources = ["*"]
  }
}

data "aws_iam_policy_document" "tool_logs" {
  statement {
    sid       = "SearchCheckoutLogs"
    actions   = ["logs:FilterLogEvents"]
    resources = [module.checkout_api.log_group_arn, "${module.checkout_api.log_group_arn}:*"]
  }
}

data "aws_iam_policy_document" "tool_deploys" {
  statement {
    sid       = "QueryDeployHistory"
    actions   = ["dynamodb:Query"]
    resources = [aws_dynamodb_table.deploy_history.arn]
  }
}

resource "aws_iam_role" "tools_gateway" {
  name                 = "sentinelops-tools-gateway"
  assume_role_policy   = data.aws_iam_policy_document.tools_gateway_assume.json
  permissions_boundary = data.aws_iam_policy.workload_boundary.arn
}

data "aws_iam_policy_document" "tools_gateway_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }

    # Only for a gateway in this account, never on another account's behalf.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:bedrock-agentcore:${var.region}:${data.aws_caller_identity.current.account_id}:gateway/*"]
    }
  }
}

data "aws_iam_policy_document" "tools_gateway" {
  statement {
    sid       = "InvokeTools"
    actions   = ["lambda:InvokeFunction"]
    resources = [for t in module.tool : t.function_arn]
  }
}

resource "aws_iam_role_policy" "tools_gateway" {
  name   = "permissions"
  role   = aws_iam_role.tools_gateway.id
  policy = data.aws_iam_policy_document.tools_gateway.json
}

# Callers sign with SigV4 and need bedrock-agentcore:InvokeGateway on it: the
# agent's runtime role once it exists, and an admin testing by hand. No
# semantic search: with three tools there's nothing to search, and it bills.
resource "aws_bedrockagentcore_gateway" "tools" {
  name            = "sentinelops-tools"
  description     = "checkout-api's investigation tools, for the SentinelOps agent"
  role_arn        = aws_iam_role.tools_gateway.arn
  authorizer_type = "AWS_IAM"
  protocol_type   = "MCP"
}

resource "aws_bedrockagentcore_gateway_target" "tool" {
  for_each = local.tool_targets

  name               = each.key
  gateway_identifier = aws_bedrockagentcore_gateway.tools.gateway_id
  description        = "Runs sentinelops-tool-${each.key}"

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = module.tool[each.key].function_arn

        tool_schema {
          inline_payload {
            name        = each.value
            description = local.gateway_tools[each.value].description

            input_schema {
              type = "object"

              dynamic "property" {
                for_each = local.gateway_tools[each.value].inputSchema.properties

                content {
                  name        = property.key
                  type        = property.value.type
                  description = property.value.description
                  required    = contains(local.gateway_tools[each.value].inputSchema.required, property.key)
                }
              }
            }
          }
        }
      }
    }
  }

  # The gateway's role must be able to call the function before the target serves it.
  depends_on = [aws_iam_role_policy.tools_gateway]
}
