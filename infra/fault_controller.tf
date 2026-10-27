# Breaks checkout-api on purpose, and clears it: the only writer of the fault
# flag, and of deploy history after the seed. Invoked from the CLI for now; see
# services/checkout-api/controller/handler.py.

module "fault_controller" {
  source = "./modules/python_lambda"

  name        = "sentinelops-fault-controller"
  description = "Sets and clears checkout-api's fault flag, with matching deploy history"
  source_dir  = local.checkout_dir
  excludes    = concat(local.checkout_tooling, ["handler.py", "checkout", "sim", "traffic"])
  handler     = "controller.handler.handler"
  timeout     = 10

  environment = {
    FLAGS_TABLE          = aws_dynamodb_table.checkout_flags.name
    DEPLOY_HISTORY_TABLE = aws_dynamodb_table.deploy_history.name
  }
  policy_json = data.aws_iam_policy_document.fault_controller.json

  build_dir                = "${path.module}/.build"
  log_retention_days       = var.log_retention_days
  permissions_boundary_arn = data.aws_iam_policy.workload_boundary.arn
  assume_role_policy_json  = data.aws_iam_policy_document.lambda_assume.json
}

# Every write is part of a transaction, which IAM checks per item: a Put or a
# Delete in TransactWriteItems needs PutItem or DeleteItem on its table.
data "aws_iam_policy_document" "fault_controller" {
  statement {
    sid       = "FaultFlag"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"]
    resources = [aws_dynamodb_table.checkout_flags.arn]
  }

  statement {
    sid       = "DeployHistory"
    actions   = ["dynamodb:PutItem", "dynamodb:DeleteItem"]
    resources = [aws_dynamodb_table.deploy_history.arn]
  }
}
