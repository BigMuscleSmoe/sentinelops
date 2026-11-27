# The remediation flow: from an answered incident to a draft PR into its
# release branch, with no model call (BUILD_PLAN.md, Phase 4 design and
# "Evening 5 plan"; remediation/sentinelops_remediation/flow.py). The trigger
# starts it once per answered incident, as a durable execution named by the
# incident's ID; only Hypothesis.can_open_pr, in its first step, decides
# whether a PR opens. It signs the GitHub App's JWT with kms:Sign and never
# sees the key; GitHub's rulesets keep it off main and release/** and block
# every merge, so nothing it writes ships without a person.
#
# Its own resources, not modules/python_lambda: the kill switch below needs
# Terraform to ignore its reserved concurrency, and the module's rules are
# copied here and pinned by remediation/tests/test_terraform.py (decision 1).
#
# KILL SWITCH (README, "Kill switch"): reserved concurrency 0 stops every
# start and holds a waiting execution, then stop-durable-execution ends each
# running one. Terraform ignores the concurrency, so a deploy never lifts it.

locals {
  remediation_name = "sentinelops-remediation"
  # Built from parts, not from the function, which depends on this role's policy.
  remediation_function_arn = "arn:aws:lambda:${var.region}:${data.aws_caller_identity.current.account_id}:function:${local.remediation_name}"
}

# Staged by remediation/build_flow.py: the package, core, the commit hooks'
# scanner, and its locked dependencies for Linux arm64 and Python 3.13.
data "archive_file" "remediation" {
  type        = "zip"
  source_dir  = "${path.module}/.build/remediation"
  output_path = "${path.module}/.build/${local.remediation_name}.zip"
  # Same permissions on every OS, so a plan from Windows and an apply from CI
  # hash the same zip.
  output_file_mode = "0644"
}

resource "aws_cloudwatch_log_group" "remediation" {
  name              = "/aws/lambda/${local.remediation_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "remediation" {
  name                 = "sentinelops-remediation"
  assume_role_policy   = data.aws_iam_policy_document.lambda_assume.json
  permissions_boundary = data.aws_iam_policy.workload_boundary.arn
}

data "aws_iam_policy_document" "remediation" {
  # Not AWSLambdaBasicExecutionRole or AWSLambdaBasicDurableExecutionRolePolicy:
  # both grant logs:CreateLogGroup, which lets Lambda make a log group with no
  # retention behind Terraform's back.
  statement {
    sid       = "WriteOwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.remediation.arn}:*"]
  }

  # The SDK checkpoints each step and reads the execution's state on replay.
  statement {
    sid       = "DurableExecution"
    actions   = ["lambda:CheckpointDurableExecution", "lambda:GetDurableExecutionState"]
    resources = [local.remediation_function_arn, "${local.remediation_function_arn}:*"]
  }

  # The App's JWT, RS256 only. The key's own policy also denies Sign to every
  # principal but the admin user and this role (github_app.tf).
  statement {
    sid       = "SignTheAppsJwt"
    actions   = ["kms:Sign"]
    resources = [aws_kms_external_key.github_app.arn]

    condition {
      test     = "StringEquals"
      variable = "kms:SigningAlgorithm"
      values   = ["RSASSA_PKCS1_V1_5_SHA_256"]
    }
  }

  # checkout-api's incidents only: the answer read, the remediation map
  # written. ForAllValues passes a request with no key at all, so the Null
  # test requires one.
  statement {
    sid       = "CheckoutIncidents"
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.incidents.arn]

    condition {
      test     = "ForAllValues:StringEquals"
      variable = "dynamodb:LeadingKeys"
      values   = ["checkout-api"]
    }

    condition {
      test     = "Null"
      variable = "dynamodb:LeadingKeys"
      values   = ["false"]
    }
  }

  # Each step's event on the incident's trace, for the page (trace.tf): PutItem only, on checkout-api's incidents' items.
  # ForAllValues passes a request with no key at all, so the Null test
  # requires one.
  statement {
    sid       = "WriteTheTrace"
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.trace.arn]

    condition {
      test     = "ForAllValues:StringLike"
      variable = "dynamodb:LeadingKeys"
      values   = ["checkout-api-*"]
    }

    condition {
      test     = "Null"
      variable = "dynamodb:LeadingKeys"
      values   = ["false"]
    }
  }
}

resource "aws_iam_role_policy" "remediation" {
  name   = "permissions"
  role   = aws_iam_role.remediation.id
  policy = data.aws_iam_policy_document.remediation.json
}

resource "aws_lambda_function" "remediation" {
  function_name    = local.remediation_name
  description      = "Opens a draft PR for an answered incident whose Hypothesis can_open_pr; calls no model"
  role             = aws_iam_role.remediation.arn
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "handler.handler"
  filename         = data.archive_file.remediation.output_path
  source_code_hash = data.archive_file.remediation.output_base64sha256
  memory_size      = 256
  # One invocation: the open step's GitHub calls, each bounded by the client.
  timeout = 300
  # A durable function is invoked by version or alias: the live alias.
  publish = true

  # 15 polls a minute apart, with the steps around them, end well inside 30
  # minutes; the history is kept 7 days, as the logs are.
  durable_config {
    execution_timeout = 1800
    retention_period  = 7
  }

  environment {
    variables = {
      INCIDENTS_TABLE            = aws_dynamodb_table.incidents.name
      GITHUB_APP_CLIENT_ID       = var.remediation_app.client_id
      GITHUB_APP_INSTALLATION_ID = tostring(var.remediation_app.installation_id)
      # For the commit hooks' account rule, which the scan applies to all it posts.
      SENTINELOPS_ACCOUNT_ID  = data.aws_caller_identity.current.account_id
      SENTINELOPS_TRACE_TABLE = aws_dynamodb_table.trace.name
    }
  }

  logging_config {
    log_format = "JSON"
    log_group  = aws_cloudwatch_log_group.remediation.name
  }

  # The kill switch's concurrency 0 is set by hand and stays until lifted by hand.
  lifecycle {
    ignore_changes = [reserved_concurrent_executions]
  }

  depends_on = [aws_iam_role_policy.remediation, aws_cloudwatch_log_group.remediation]
}

resource "aws_lambda_alias" "remediation_live" {
  name             = "live"
  description      = "What the trigger starts: the version this deploy published"
  function_name    = aws_lambda_function.remediation.function_name
  function_version = aws_lambda_function.remediation.version
}
