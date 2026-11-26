"""infra/remediation.tf keeps modules/python_lambda's rules by hand (decision 1), and the kill switch's hold.

Read as text, block by block: each rule the module applies to every other
function in the stack, and the one thing this function does differently.
"""

import re
from pathlib import Path

import build_flow

REPO = Path(__file__).resolve().parents[2]
TF = (REPO / "infra" / "remediation.tf").read_text(encoding="utf-8")
TRIGGER_TF = (REPO / "infra" / "trigger.tf").read_text(encoding="utf-8")


def block(header: str, text: str = TF) -> str:
    """A top-level block, from its header to its closing brace."""
    start = text.index(header + " {\n")
    return text[start : text.index("\n}\n", start)]


def statement(sid: str) -> str:
    policy = block('data "aws_iam_policy_document" "remediation"')
    start = policy.index(f'sid       = "{sid}"')
    end = policy.find("\n  statement {", start)
    return policy[start : end if end != -1 else len(policy)]


FUNCTION = block('resource "aws_lambda_function" "remediation"')


def test_its_log_group_has_the_stacks_retention_and_comes_first() -> None:
    group = block('resource "aws_cloudwatch_log_group" "remediation"')
    assert 'name              = "/aws/lambda/${local.remediation_name}"' in group
    assert "retention_in_days = var.log_retention_days" in group
    assert "depends_on = [aws_iam_role_policy.remediation, aws_cloudwatch_log_group.remediation]" in FUNCTION
    assert '    log_format = "JSON"\n    log_group  = aws_cloudwatch_log_group.remediation.name\n' in FUNCTION


def test_it_can_write_only_its_own_logs_and_never_make_a_log_group() -> None:
    assert "CreateLogGroup" not in TF.replace("logs:CreateLogGroup, which", "")
    assert 'resources = ["${aws_cloudwatch_log_group.remediation.arn}:*"]' in statement("WriteOwnLogs")
    assert "managed_policy_arns" not in TF and "aws_iam_role_policy_attachment" not in TF


def test_its_role_is_boundary_fenced_and_lambdas_alone() -> None:
    role = block('resource "aws_iam_role" "remediation"')
    assert "permissions_boundary = data.aws_iam_policy.workload_boundary.arn" in role
    assert "assume_role_policy   = data.aws_iam_policy_document.lambda_assume.json" in role
    assert re.findall(r'^resource "aws_iam_role" ', TF, re.M) == ['resource "aws_iam_role" ']


def test_the_role_holds_exactly_four_statements_each_scoped() -> None:
    policy = block('data "aws_iam_policy_document" "remediation"')
    assert re.findall(r'sid       = "(\w+)"', policy) == ["WriteOwnLogs", "DurableExecution", "SignTheAppsJwt", "CheckoutIncidents"]
    assert "*" not in policy.replace(":*", "").replace("# ", "")
    durable = statement("DurableExecution")
    assert 'actions   = ["lambda:CheckpointDurableExecution", "lambda:GetDurableExecutionState"]' in durable
    assert 'resources = [local.remediation_function_arn, "${local.remediation_function_arn}:*"]' in durable
    sign = statement("SignTheAppsJwt")
    assert 'actions   = ["kms:Sign"]' in sign and "resources = [aws_kms_external_key.github_app.arn]" in sign
    assert 'variable = "kms:SigningAlgorithm"' in sign and 'values   = ["RSASSA_PKCS1_V1_5_SHA_256"]' in sign
    incidents = statement("CheckoutIncidents")
    assert 'actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]' in incidents
    assert 'test     = "ForAllValues:StringEquals"\n      variable = "dynamodb:LeadingKeys"\n      values   = ["checkout-api"]' in incidents
    assert 'test     = "Null"\n      variable = "dynamodb:LeadingKeys"\n      values   = ["false"]' in incidents


def test_the_function_is_the_durable_one_the_build_stages() -> None:
    assert 'runtime          = "python3.13"' in FUNCTION and f"python{build_flow.PYTHON}" == "python3.13"
    assert 'architectures    = ["arm64"]' in FUNCTION and build_flow.PLATFORM.startswith("aarch64-")
    assert 'handler          = "handler.handler"' in FUNCTION and build_flow.ENTRY == "handler.py"
    assert "publish = true" in FUNCTION
    assert "    execution_timeout = 1800\n    retention_period  = 7\n" in FUNCTION
    archive = block('data "archive_file" "remediation"')
    assert 'source_dir  = "${path.module}/.build/remediation"' in archive
    assert build_flow.OUT == REPO / "infra" / ".build" / "remediation"
    assert 'output_file_mode = "0644"' in archive
    assert 'remediation_name = "sentinelops-remediation"' in TF


def test_fifteen_polls_fit_the_execution_timeout() -> None:
    from sentinelops_remediation import flow

    polling = flow.POLLS * flow.POLL.to_seconds() if hasattr(flow.POLL, "to_seconds") else flow.POLLS * 60
    assert polling + 300 * 2 < 1800  # the polls, and two invocations' worth of open and conclude


def test_a_deploy_never_lifts_the_kill_switch() -> None:
    assert "  lifecycle {\n    ignore_changes = [reserved_concurrent_executions]\n  }" in FUNCTION
    assert "reserved_concurrent_executions =" not in FUNCTION


def test_the_trigger_starts_it_only_through_the_live_alias() -> None:
    alias = block('resource "aws_lambda_alias" "remediation_live"')
    assert 'name             = "live"' in alias
    assert "function_version = aws_lambda_function.remediation.version" in alias
    trigger = block('data "aws_iam_policy_document" "trigger"', TRIGGER_TF)
    start = trigger[trigger.index('sid       = "StartTheRemediationFlow"') :]
    assert 'actions   = ["lambda:InvokeFunction"]' in start
    assert "resources = [aws_lambda_alias.remediation_live.arn]" in start
    # Nothing else may invoke it: no resource policy, no event source.
    for path in (REPO / "infra").glob("*.tf"):
        text = path.read_text(encoding="utf-8")
        assert "aws_lambda_function.remediation.function_name" not in text or path.name in ("remediation.tf", "outputs.tf"), path.name
        assert 'resource "aws_lambda_permission" "remediation' not in text, path.name


def test_the_apps_ids_come_from_the_variable_never_the_repo() -> None:
    assert "GITHUB_APP_CLIENT_ID       = var.remediation_app.client_id" in FUNCTION
    assert "GITHUB_APP_INSTALLATION_ID = tostring(var.remediation_app.installation_id)" in FUNCTION
    variables = (REPO / "infra" / "variables.tf").read_text(encoding="utf-8")
    app = block('variable "remediation_app"', variables)
    assert "sensitive = true" in app and "nullable  = false" in app
