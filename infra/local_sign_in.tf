# --- Local runs' sign-in to the Anthropic API ---------------------------------

# Anthropic's federation rules name IAM roles only, so a local run, as the user
# sentinelops-dev, assumes this role just to ask STS for the token the
# runtime's role asks for (agent/providers.py, SENTINELOPS_ANTHROPIC_ROLE_ARN).
# It may do nothing else: the run's other calls stay sentinelops-dev's.
resource "aws_iam_role" "local_sign_in" {
  name                 = "sentinelops-local"
  description          = "Assumed by sentinelops-dev only to sign local agent runs in to the Anthropic API."
  assume_role_policy   = data.aws_iam_policy_document.local_sign_in_assume.json
  permissions_boundary = data.aws_iam_policy.workload_boundary.arn
}

data "aws_iam_policy_document" "local_sign_in_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:user/sentinelops-dev"]
    }
  }
}

# The runtime's SignInToAnthropic statement, condition for condition, which
# agent/tests/test_providers.py checks in both files.
data "aws_iam_policy_document" "local_sign_in" {
  statement {
    sid       = "SignInToAnthropic"
    actions   = ["sts:GetWebIdentityToken"]
    resources = ["*"]

    # ForAllValues alone would pass a request naming no audience; Null rules it out.
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "sts:IdentityTokenAudience"
      values   = ["https://api.anthropic.com"]
    }
    condition {
      test     = "Null"
      variable = "sts:IdentityTokenAudience"
      values   = ["false"]
    }
    condition {
      test     = "StringEquals"
      variable = "sts:SigningAlgorithm"
      values   = ["RS256"]
    }
    condition {
      test     = "NumericLessThanEquals"
      variable = "sts:DurationSeconds"
      values   = ["300"]
    }
  }
}

resource "aws_iam_role_policy" "local_sign_in" {
  name   = "permissions"
  role   = aws_iam_role.local_sign_in.id
  policy = data.aws_iam_policy_document.local_sign_in.json
}
