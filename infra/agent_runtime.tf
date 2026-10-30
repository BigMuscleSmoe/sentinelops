# The agent on AgentCore Runtime: our own HTTP app (agent/app.py) with its
# locked dependencies, deployed as direct code from S3. The tools run behind
# the tools gateway, so the runtime's role reads the alarm, the API key, and
# nothing the tools read. Only the trigger (Phase 2, step 4) will invoke it.
#
# agent/build_runtime.py stages the package before any plan: CI runs it in the
# deploy job, and a local plan needs it run first.
locals {
  agent_runtime_name = "sentinelops_agent" # letters, digits, and underscores only
  # The Runtime names the log group after the runtime's ID, which is its name
  # and a random suffix: known only once it exists.
  agent_runtime_log_groups = "arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/bedrock-agentcore/runtimes/${local.agent_runtime_name}-*"
}

data "archive_file" "agent_runtime" {
  type        = "zip"
  source_dir  = "${path.module}/.build/agent-runtime"
  output_path = "${path.module}/.build/sentinelops-agent-runtime.zip"
  # As the Lambda module does: the same permissions on every OS, so a plan
  # from Windows and an apply from CI hash the same zip.
  output_file_mode = "0644"
}

# Only build outputs live here, each rebuilt from the repo, so a destroy may
# empty it. Versioned, because the runtime pins the version it runs.
resource "aws_s3_bucket" "agent_code" {
  bucket        = "sentinelops-agent-code-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_versioning" "agent_code" {
  bucket = aws_s3_bucket.agent_code.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "agent_code" {
  bucket = aws_s3_bucket.agent_code.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# A replaced package stays a month, to roll the runtime back to.
resource "aws_s3_bucket_lifecycle_configuration" "agent_code" {
  bucket = aws_s3_bucket.agent_code.id

  rule {
    id     = "expire-old-packages"
    status = "Enabled"
    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

resource "aws_s3_object" "agent_runtime" {
  bucket      = aws_s3_bucket.agent_code.id
  key         = "agent-runtime.zip"
  source      = data.archive_file.agent_runtime.output_path
  source_hash = data.archive_file.agent_runtime.output_base64sha256

  # The runtime pins this object's version ID, which needs versioning on first.
  depends_on = [aws_s3_bucket_versioning.agent_code]
}

resource "aws_iam_role" "agent_runtime" {
  name                 = "sentinelops-agent-runtime"
  assume_role_policy   = data.aws_iam_policy_document.agent_runtime_assume.json
  permissions_boundary = data.aws_iam_policy.workload_boundary.arn
}

data "aws_iam_policy_document" "agent_runtime_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
    # As AWS documents the runtime's trust: only for AgentCore in this account.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:bedrock-agentcore:${var.region}:${data.aws_caller_identity.current.account_id}:*"]
    }
  }
}

# No logs:CreateLogGroup: Terraform makes the group, with its retention, once
# the runtime's ID is known. No Bedrock, no S3 (the caller that deploys reads
# the package), and nothing the tools read: they run behind the gateway.
data "aws_iam_policy_document" "agent_runtime" {
  statement {
    sid       = "WriteOwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
    resources = [local.agent_runtime_log_groups, "${local.agent_runtime_log_groups}:log-stream:*"]
  }

  statement {
    sid       = "FindLogGroups"
    actions   = ["logs:DescribeLogGroups"]
    resources = ["arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:*"]
  }

  statement {
    sid       = "CallTheTools"
    actions   = ["bedrock-agentcore:InvokeGateway"]
    resources = [aws_bedrockagentcore_gateway.tools.gateway_arn]
  }

  # Metric alarms allow a narrower scope than *; composite alarms don't, and
  # the agent reads none.
  statement {
    sid       = "ReadTheAlarm"
    actions   = ["cloudwatch:DescribeAlarms", "cloudwatch:DescribeAlarmHistory"]
    resources = ["arn:aws:cloudwatch:${var.region}:${data.aws_caller_identity.current.account_id}:alarm:sentinelops-*"]
  }

  statement {
    sid       = "ReadTheApiKey"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.anthropic_api_key.arn]
  }
}

resource "aws_iam_role_policy" "agent_runtime" {
  name   = "permissions"
  role   = aws_iam_role.agent_runtime.id
  policy = data.aws_iam_policy_document.agent_runtime.json
}

resource "aws_bedrockagentcore_agent_runtime" "agent" {
  agent_runtime_name = local.agent_runtime_name
  description        = "The SentinelOps agent: one alarm's investigation per session"
  role_arn           = aws_iam_role.agent_runtime.arn

  agent_runtime_artifact {
    code_configuration {
      entry_point = ["app.py"]
      runtime     = "PYTHON_3_12"
      code {
        s3 {
          bucket     = aws_s3_object.agent_runtime.bucket
          prefix     = aws_s3_object.agent_runtime.key
          version_id = aws_s3_object.agent_runtime.version_id
        }
      }
    }
  }

  # PUBLIC reaches the Anthropic API and the gateway without a VPC or a NAT.
  network_configuration {
    network_mode = "PUBLIC"
  }

  protocol_configuration {
    server_protocol = "HTTP"
  }

  # The guard ends a run within 300 s. A session lives at most 360 s, and one
  # left idle ends after 120 s, so none bills for long.
  lifecycle_configuration = [{
    idle_runtime_session_timeout = 120
    max_lifetime                 = 360
  }]

  environment_variables = {
    SENTINELOPS_GATEWAY_URL = aws_bedrockagentcore_gateway.tools.gateway_url
  }

  # The role must be able to log and call the gateway before a session starts.
  depends_on = [aws_iam_role_policy.agent_runtime]
}

resource "aws_cloudwatch_log_group" "agent_runtime" {
  name              = "/aws/bedrock-agentcore/runtimes/${aws_bedrockagentcore_agent_runtime.agent.agent_runtime_id}-DEFAULT"
  retention_in_days = var.log_retention_days
}
