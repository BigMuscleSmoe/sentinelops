# Account-level foundations that must exist before CI can deploy anything:
# the state bucket, the budget alarm, and the GitHub OIDC deploy role.
#
# Applied once, locally, with SSO credentials. State stays local (and
# gitignored) because the bucket it would live in is created here. If the
# state file is lost, `terraform import` the handful of resources below.

terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.27"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      cost_center = "sentinelops"
      managed_by  = "terraform"
      stack       = "bootstrap"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  account_id   = data.aws_caller_identity.current.account_id
  state_bucket = "sentinelops-tfstate-${local.account_id}"
}

# --- Terraform state ---------------------------------------------------------

resource "aws_s3_bucket" "state" {
  bucket = local.state_bucket

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket = aws_s3_bucket.state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Old state versions are only useful for recovery; don't keep them forever.
resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"
    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

# --- Budget alarm ------------------------------------------------------------

resource "aws_budgets_budget" "monthly" {
  name         = "sentinelops-monthly"
  budget_type  = "COST"
  limit_amount = "25"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_email]
  }
}

# The cost_center tag only shows up in Cost Explorer once it's activated, and
# AWS only offers it for activation ~24h after it first appears on a resource.
# Apply once without this, then set activate_cost_tag = true and apply again.
resource "aws_ce_cost_allocation_tag" "cost_center" {
  count = var.activate_cost_tag ? 1 : 0

  tag_key = "cost_center"
  status  = "Active"
}

# --- GitHub Actions OIDC -----------------------------------------------------

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_policy_document" "github_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # Only pushes to main can deploy. PRs and other branches get no AWS access.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repo}:ref:refs/heads/main"]
    }
  }
}

# Named outside the sentinelops-* prefix on purpose: the deploy role can manage
# sentinelops-* roles and policies, so it must not match its own permissions.
resource "aws_iam_role" "github_deploy" {
  name                 = "github-actions-sentinelops-deploy"
  assume_role_policy   = data.aws_iam_policy_document.github_trust.json
  max_session_duration = 3600
}

# Non-IAM services: PowerUserAccess. Scoping this per service would mean
# re-applying bootstrap every phase; the real escalation risk is IAM, which is
# handled separately below.
resource "aws_iam_role_policy_attachment" "github_deploy_poweruser" {
  role       = aws_iam_role.github_deploy.name
  policy_arn = "arn:aws:iam::aws:policy/PowerUserAccess"
}

# Every role the pipeline creates must carry this boundary, so no workload role
# can ever touch IAM — even if someone attaches AdministratorAccess to it.
data "aws_iam_policy_document" "workload_boundary" {
  statement {
    sid       = "AllowWorkloadServices"
    actions   = ["*"]
    resources = ["*"]
  }

  statement {
    sid       = "DenyIdentityAndAccountChanges"
    effect    = "Deny"
    actions   = ["iam:*", "organizations:*", "account:*", "sso:*"]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "workload_boundary" {
  name        = "boundary-sentinelops-workload"
  description = "Permissions boundary required on every role created by the SentinelOps pipeline."
  policy      = data.aws_iam_policy_document.workload_boundary.json
}

data "aws_iam_policy_document" "github_deploy_iam" {
  statement {
    sid       = "ReadIam"
    actions   = ["iam:Get*", "iam:List*"]
    resources = ["*"]
  }

  statement {
    sid = "ManageBoundedRoles"
    actions = [
      "iam:CreateRole",
      "iam:PutRolePermissionsBoundary",
      "iam:AttachRolePolicy",
      "iam:DetachRolePolicy",
      "iam:PutRolePolicy",
      "iam:DeleteRolePolicy",
    ]
    resources = ["arn:aws:iam::${local.account_id}:role/sentinelops-*"]

    condition {
      test     = "StringEquals"
      variable = "iam:PermissionsBoundary"
      values   = [aws_iam_policy.workload_boundary.arn]
    }
  }

  statement {
    sid = "MaintainRoles"
    actions = [
      "iam:DeleteRole",
      "iam:TagRole",
      "iam:UntagRole",
      "iam:UpdateRole",
      "iam:UpdateRoleDescription",
      "iam:UpdateAssumeRolePolicy",
    ]
    resources = ["arn:aws:iam::${local.account_id}:role/sentinelops-*"]
  }

  statement {
    sid       = "PassRoles"
    actions   = ["iam:PassRole"]
    resources = ["arn:aws:iam::${local.account_id}:role/sentinelops-*"]
  }

  statement {
    sid = "ManagePolicies"
    actions = [
      "iam:CreatePolicy",
      "iam:DeletePolicy",
      "iam:CreatePolicyVersion",
      "iam:DeletePolicyVersion",
      "iam:TagPolicy",
      "iam:UntagPolicy",
    ]
    resources = ["arn:aws:iam::${local.account_id}:policy/sentinelops-*"]
  }

  statement {
    sid    = "ProtectBoundary"
    effect = "Deny"
    actions = [
      "iam:DeleteRolePermissionsBoundary",
      "iam:CreatePolicyVersion",
      "iam:DeletePolicy",
      "iam:SetDefaultPolicyVersion",
    ]
    resources = [
      aws_iam_policy.workload_boundary.arn,
      "arn:aws:iam::${local.account_id}:role/sentinelops-*",
    ]
  }

  statement {
    sid       = "StateBucketIsNotDeployable"
    effect    = "Deny"
    actions   = ["s3:DeleteBucket", "s3:PutBucketPolicy", "s3:PutBucketVersioning", "s3:PutLifecycleConfiguration"]
    resources = [aws_s3_bucket.state.arn]
  }
}

resource "aws_iam_role_policy" "github_deploy_iam" {
  name   = "bounded-iam"
  role   = aws_iam_role.github_deploy.id
  policy = data.aws_iam_policy_document.github_deploy_iam.json
}
