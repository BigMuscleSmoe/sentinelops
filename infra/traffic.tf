# Steady baseline traffic for checkout-api: EventBridge Scheduler invokes the
# generator once a minute, and each run sends 12 signed requests over ~50 s.
# Retries are off, so a missed minute is skipped, not replayed. The generator
# is the largest line in Phase 1's cost (~$3.70/month), because Lambda bills
# the waits between requests; traffic_enabled = false stops it.

module "traffic_generator" {
  source = "./modules/python_lambda"

  name        = "sentinelops-traffic-generator"
  description = "Steady baseline traffic for checkout-api: 12 signed requests a minute"
  source_dir  = local.checkout_dir
  excludes    = concat(local.checkout_tooling, ["handler.py", "checkout", "sim", "controller"])
  handler     = "traffic.handler.handler"
  # ~50 s of requests, then up to checkout-api's own 10 s for the last one.
  timeout = 70

  environment = {
    CHECKOUT_API_URL = aws_apigatewayv2_api.checkout.api_endpoint
  }
  policy_json = data.aws_iam_policy_document.traffic_generator.json

  build_dir                = "${path.module}/.build"
  log_retention_days       = var.log_retention_days
  permissions_boundary_arn = data.aws_iam_policy.workload_boundary.arn
  assume_role_policy_json  = data.aws_iam_policy_document.lambda_assume.json
}

# Only the three routes in the mix; not /health.
data "aws_iam_policy_document" "traffic_generator" {
  statement {
    sid     = "CallCheckoutApi"
    actions = ["execute-api:Invoke"]
    resources = [
      for route in ["GET/products/*", "POST/cart/items", "POST/checkout"] :
      "${aws_apigatewayv2_api.checkout.execution_arn}/*/${route}"
    ]
  }
}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }

    # Only schedules in this account can use the role.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "traffic_scheduler" {
  name                 = "sentinelops-traffic-scheduler"
  description          = "Lets EventBridge Scheduler invoke the traffic generator"
  assume_role_policy   = data.aws_iam_policy_document.scheduler_assume.json
  permissions_boundary = data.aws_iam_policy.workload_boundary.arn
}

data "aws_iam_policy_document" "traffic_scheduler" {
  statement {
    sid       = "InvokeGenerator"
    actions   = ["lambda:InvokeFunction"]
    resources = [module.traffic_generator.function_arn]
  }
}

resource "aws_iam_role_policy" "traffic_scheduler" {
  name   = "permissions"
  role   = aws_iam_role.traffic_scheduler.id
  policy = data.aws_iam_policy_document.traffic_scheduler.json
}

resource "aws_scheduler_schedule" "traffic" {
  name                = "sentinelops-traffic-generator"
  description         = "Baseline traffic for checkout-api, once a minute"
  schedule_expression = "rate(1 minute)"
  state               = var.traffic_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = module.traffic_generator.function_arn
    role_arn = aws_iam_role.traffic_scheduler.arn

    retry_policy {
      maximum_retry_attempts       = 0
      maximum_event_age_in_seconds = 60
    }
  }
}
