# The demo service the agent investigates: one Lambda behind an HTTP API.

locals {
  # Four functions are built from this directory: checkout-api, the fault
  # controller, and the traffic generator and burst, which share the traffic
  # package. Each excludes the others' packages.
  checkout_dir = "${path.module}/../services/checkout-api"
  # Tests and local tooling, kept out of every zip built from it. tools/ holds
  # the live-run scripts, which name every fault.
  checkout_tooling = ["pyproject.toml", "uv.lock", "tests", "tools", ".venv", ".pytest_cache", "**/__pycache__"]
}

module "checkout_api" {
  source = "./modules/python_lambda"

  name        = "sentinelops-checkout-api"
  description = "Demo service that SentinelOps investigates"
  source_dir  = local.checkout_dir
  excludes    = concat(local.checkout_tooling, ["sim/pool_sim.py", "controller", "traffic"])
  # Over 128: every request makes two DynamoDB calls, and boto3's signing is CPU
  # bound, so more memory (and with it CPU) keeps the round trips short.
  memory_size = 256
  timeout     = 10
  # A ceiling on what a runaway caller can run up. The burst peaks at ~50 in
  # flight under the pool fault. Lambda turns away whatever comes past the cap,
  # and those requests never reach the service's metrics, so the cap must stay
  # well above any demo's peak.
  reserved_concurrent_executions = 80

  environment = {
    POOL_TABLE              = aws_dynamodb_table.checkout_pool.name
    FLAGS_TABLE             = aws_dynamodb_table.checkout_flags.name
    POOL_MAX_SIZE           = "20"
    POOL_ACQUIRE_TIMEOUT_MS = "1500"
    PAYMENTS_TIMEOUT_MS     = "2000"
  }
  policy_json = data.aws_iam_policy_document.checkout_api.json

  build_dir                = "${path.module}/.build"
  log_retention_days       = var.log_retention_days
  permissions_boundary_arn = data.aws_iam_policy.workload_boundary.arn
  assume_role_policy_json  = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "checkout_api" {
  # Put only creates the pool item, the first time it's used.
  statement {
    sid       = "PoolLedger"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.checkout_pool.arn]
  }

  # Reads the fault flag; only the fault controller writes it.
  statement {
    sid       = "ReadFaultFlag"
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.checkout_flags.arn]
  }
}

resource "aws_apigatewayv2_api" "checkout" {
  name          = "sentinelops-checkout-api"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "checkout" {
  api_id                 = aws_apigatewayv2_api.checkout.id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.checkout_api.invoke_arn
  payload_format_version = "2.0"
}

# Every route needs a SigV4-signed request: only the traffic generator and CI
# call this API, so it never takes anonymous traffic.
resource "aws_apigatewayv2_route" "checkout" {
  for_each = toset(["GET /health", "GET /products/{id}", "POST /cart/items", "POST /checkout"])

  api_id             = aws_apigatewayv2_api.checkout.id
  route_key          = each.key
  target             = "integrations/${aws_apigatewayv2_integration.checkout.id}"
  authorization_type = "AWS_IAM"
}

resource "aws_apigatewayv2_stage" "checkout" {
  api_id      = aws_apigatewayv2_api.checkout.id
  name        = "$default"
  auto_deploy = true

  # Room for the demo burst's 22 requests/s, and a ceiling on a runaway caller.
  default_route_settings {
    throttling_rate_limit  = 30
    throttling_burst_limit = 60
  }
}

resource "aws_lambda_permission" "checkout_api" {
  statement_id  = "AllowCheckoutHttpApi"
  action        = "lambda:InvokeFunction"
  function_name = module.checkout_api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.checkout.execution_arn}/*/*"
}
