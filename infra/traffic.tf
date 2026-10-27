# Traffic for checkout-api, from two functions built from the same package.
#
# The generator is the steady baseline: EventBridge Scheduler invokes it once a
# minute, and each run sends 12 signed requests over ~50 s. Retries are off,
# so a missed minute is skipped, not replayed. It is the largest line in
# Phase 1's cost (~$3.70/month), because Lambda bills the waits between
# requests; traffic_enabled = false stops it.
#
# The burst is what every break sends: 6 minutes, ramping from 6 to 22
# requests a second. Only the fault controller starts it. See
# services/checkout-api/traffic/burst.py.

locals {
  traffic_excludes = concat(local.checkout_tooling, ["handler.py", "checkout", "sim", "controller"])
}

module "traffic_generator" {
  source = "./modules/python_lambda"

  name        = "sentinelops-traffic-generator"
  description = "Steady baseline traffic for checkout-api: 12 signed requests a minute"
  source_dir  = local.checkout_dir
  excludes    = local.traffic_excludes
  handler     = "traffic.handler.handler"
  # ~50 s of requests, then up to checkout-api's own 10 s for the last one.
  timeout = 70

  environment = {
    CHECKOUT_API_URL = aws_apigatewayv2_api.checkout.api_endpoint
  }
  policy_json = data.aws_iam_policy_document.call_checkout_api.json

  build_dir                = "${path.module}/.build"
  log_retention_days       = var.log_retention_days
  permissions_boundary_arn = data.aws_iam_policy.workload_boundary.arn
  assume_role_policy_json  = data.aws_iam_policy_document.lambda_assume.json
}

module "traffic_burst" {
  source = "./modules/python_lambda"

  name        = "sentinelops-traffic-burst"
  description = "The traffic every break sends checkout-api: 6 minutes, ramping from 6 to 22 requests a second"
  source_dir  = local.checkout_dir
  excludes    = local.traffic_excludes
  handler     = "traffic.handler.burst_handler"
  # Lambda's CPU scales with memory. Signing ~22 requests a second, each on a
  # new TLS connection, would fall behind the schedule on 128 MB's sliver of
  # CPU. At 1024 MB a burst still costs about half a cent.
  memory_size = 1024
  # 360 s of requests, then up to checkout-api's own 10 s for the last one.
  timeout = 400
  # One burst at a time. A second one, started while the first runs, is
  # throttled until it ages out (below), so two can never stack up.
  reserved_concurrent_executions = 1

  environment = {
    CHECKOUT_API_URL = aws_apigatewayv2_api.checkout.api_endpoint
  }
  policy_json = data.aws_iam_policy_document.call_checkout_api.json

  build_dir                = "${path.module}/.build"
  log_retention_days       = var.log_retention_days
  permissions_boundary_arn = data.aws_iam_policy.workload_boundary.arn
  assume_role_policy_json  = data.aws_iam_policy_document.lambda_assume.json
}

# The controller starts the burst with an asynchronous invoke. By default Lambda
# retries a failed run twice, and keeps a throttled event for up to 6 hours, so
# a burst could arrive long after its break. Never retried, and dropped if it
# can't start within a minute.
resource "aws_lambda_function_event_invoke_config" "traffic_burst" {
  function_name                = module.traffic_burst.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 60
}

# Only the three routes in the mix; not /health.
data "aws_iam_policy_document" "call_checkout_api" {
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
