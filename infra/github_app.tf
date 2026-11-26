# The GitHub App's private key, in KMS: the remediation flow signs the App's
# JWT with kms:Sign (RS256) and never sees the key (BUILD_PLAN.md, Phase 4
# design, decision 14; remediation/sentinelops_remediation/github.py).
#
# Terraform makes the key with no key material, so it waits in PendingImport.
# The user imports GitHub's key once, by hand, from the CLI (BUILD_PLAN.md,
# "Evening 4 plan", step 9), so the key never passes through Terraform or its
# state. After the import KMS shows the key Enabled; `enabled` is left unset
# (computed), so that reads as no change. An asymmetric KMS key takes one key
# material for life, and no copy of GitHub's key is kept: losing this key means
# a new GitHub key, a new KMS key, and a new import, so Terraform may never
# destroy it. $1 a month from creation; RSA 2048 signing $0.03 per 10,000 calls.
resource "aws_kms_external_key" "github_app" {
  description             = "sentinelops: signs the GitHub App's JWTs (RS256). Key material imported once, by hand."
  key_spec                = "RSA_2048"
  key_usage               = "SIGN_VERIFY"
  deletion_window_in_days = 7

  # The key policy (the user's call, 2026-11-24; pinned by
  # remediation/tests/test_key_policy.py). The account root keeps kms:*, so
  # the key can't be locked out and IAM still governs who manages it: the
  # admin user (AdministratorAccess) and CI's deploy role (PowerUserAccess).
  # Using it is denied to every other principal, the deploy role and root
  # included, whatever IAM or a grant allows: kms:Sign, the one call that
  # touches the private key, to all but the admin user and the remediation
  # flow's role (evening 5, the user's note of 2026-11-24); kms:Verify and
  # kms:GetPublicKey, the key's other uses, to all but the admin user
  # (Terraform's reads need neither). The role is named in a condition, a
  # string, so the policy holds whether or not the role exists yet.
  policy = templatefile("${path.module}/github_app_key_policy.json.tftpl", {
    account_id = data.aws_caller_identity.current.account_id
    flow_role  = aws_iam_role.remediation.name
  })

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_alias" "github_app" {
  name          = "alias/sentinelops-github-app"
  target_key_id = aws_kms_external_key.github_app.id
}
