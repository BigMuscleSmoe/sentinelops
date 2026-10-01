data "aws_caller_identity" "current" {}

# Created by infra/bootstrap. Every role in this stack must carry it, or the
# deploy role isn't allowed to create the role at all.
data "aws_iam_policy" "workload_boundary" {
  name = "boundary-sentinelops-workload"
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}
