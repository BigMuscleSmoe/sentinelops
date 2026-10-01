# Copy these into GitHub: Settings > Secrets and variables > Actions > Variables.

output "AWS_DEPLOY_ROLE_ARN" {
  value = aws_iam_role.github_deploy.arn
}

output "TF_STATE_BUCKET" {
  value = aws_s3_bucket.state.bucket
}

output "AWS_REGION" {
  value = var.region
}

output "workload_boundary_arn" {
  value = aws_iam_policy.workload_boundary.arn
}
