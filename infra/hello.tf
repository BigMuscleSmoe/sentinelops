# Phase 0 smoke test: proves the pipeline, tagging, and log retention work
# end to end. Delete once checkout-api exists.

data "archive_file" "hello" {
  type        = "zip"
  source_dir  = "${path.module}/../services/hello"
  output_path = "${path.module}/.build/hello.zip"
}

# Created before the function so Lambda never auto-creates it without retention.
resource "aws_cloudwatch_log_group" "hello" {
  name              = "/aws/lambda/sentinelops-hello"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "hello" {
  name                 = "sentinelops-hello"
  assume_role_policy   = data.aws_iam_policy_document.lambda_assume.json
  permissions_boundary = data.aws_iam_policy.workload_boundary.arn
}

# Not AWSLambdaBasicExecutionRole: that grants logs:CreateLogGroup on *, which
# lets Lambda create log groups with infinite retention behind Terraform's back.
data "aws_iam_policy_document" "hello_logs" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.hello.arn}:*"]
  }
}

resource "aws_iam_role_policy" "hello_logs" {
  name   = "logs"
  role   = aws_iam_role.hello.id
  policy = data.aws_iam_policy_document.hello_logs.json
}

resource "aws_lambda_function" "hello" {
  function_name    = "sentinelops-hello"
  role             = aws_iam_role.hello.arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "handler.handler"
  filename         = data.archive_file.hello.output_path
  source_code_hash = data.archive_file.hello.output_base64sha256
  memory_size      = 128
  timeout          = 5

  logging_config {
    log_format = "JSON"
    log_group  = aws_cloudwatch_log_group.hello.name
  }

  depends_on = [aws_iam_role_policy.hello_logs]
}
