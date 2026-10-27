# A Python Lambda built the way every function in this stack is: its own log
# group with the stack's retention, created before the function so Lambda never
# makes one without it, and its own boundary-fenced role that can write only to
# that log group plus whatever the caller grants.

data "archive_file" "this" {
  type        = "zip"
  source_dir  = var.source_dir
  excludes    = var.excludes
  output_path = "${var.build_dir}/${var.name}.zip"
  # Same permissions on every OS, so a plan from Windows and an apply from CI
  # hash the same zip.
  output_file_mode = "0644"
}

resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${var.name}"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "this" {
  name                 = var.name
  assume_role_policy   = var.assume_role_policy_json
  permissions_boundary = var.permissions_boundary_arn
}

# Not AWSLambdaBasicExecutionRole: that grants logs:CreateLogGroup on *, which
# lets Lambda create log groups with infinite retention behind Terraform's back.
data "aws_iam_policy_document" "this" {
  source_policy_documents = var.policy_json == null ? [] : [var.policy_json]

  statement {
    sid       = "WriteOwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.this.arn}:*"]
  }
}

resource "aws_iam_role_policy" "this" {
  name   = "permissions"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.this.json
}

resource "aws_lambda_function" "this" {
  function_name    = var.name
  description      = var.description
  role             = aws_iam_role.this.arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = var.handler
  filename         = data.archive_file.this.output_path
  source_code_hash = data.archive_file.this.output_base64sha256
  memory_size      = var.memory_size
  timeout          = var.timeout

  reserved_concurrent_executions = var.reserved_concurrent_executions

  dynamic "environment" {
    for_each = length(var.environment) > 0 ? [var.environment] : []
    content {
      variables = environment.value
    }
  }

  logging_config {
    log_format = "JSON"
    log_group  = aws_cloudwatch_log_group.this.name
  }

  depends_on = [aws_iam_role_policy.this]
}
