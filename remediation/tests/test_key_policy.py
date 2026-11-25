"""The GitHub App key's policy, pinned (the user's call, 2026-11-24).

The account root keeps full administration, so the key can't be locked out
and IAM governs who manages it (the admin user, CI's deploy role). Using it,
kms:Sign above all, is denied to every principal but the admin user: the
deploy role and root included. Evening 5 adds the flow's role to kms:Sign's
exception, and this test with it.
"""

import fnmatch
import json
import re
from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parents[2] / "infra"
TEMPLATE = INFRA / "github_app_key_policy.json.tftpl"
KEY_FILE = INFRA / "github_app.tf"
ACCOUNT = "123456789012"  # the documentation placeholder the commit hooks allow
ROOT = f"arn:aws:iam::{ACCOUNT}:root"
ADMIN_USER = f"arn:aws:iam::{ACCOUNT}:user/sentinelops-dev"
DEPLOY_ROLE = f"arn:aws:iam::{ACCOUNT}:role/github-actions-sentinelops-deploy"
RUNTIME_ROLE = f"arn:aws:iam::{ACCOUNT}:role/sentinelops-agent-runtime"
OTHER_ACCOUNT_ROLE = "arn:aws:iam::000011112222:role/anyone"

PINNED = {
    "Version": "2012-10-17",
    "Id": "sentinelops-github-app-key",
    "Statement": [
        {
            "Sid": "AccountRootAdministers",
            "Effect": "Allow",
            "Principal": {"AWS": ROOT},
            "Action": "kms:*",
            "Resource": "*",
        },
        {
            "Sid": "OnlyTheAdminUserUsesTheKey",
            "Effect": "Deny",
            "Principal": {"AWS": "*"},
            "Action": ["kms:Sign", "kms:Verify", "kms:GetPublicKey"],
            "Resource": "*",
            "Condition": {"ArnNotEquals": {"aws:PrincipalArn": ADMIN_USER}},
        },
    ],
}


def rendered() -> dict:
    """The template as Terraform's templatefile renders it, for a placeholder account."""
    text = TEMPLATE.read_text(encoding="utf-8")
    assert text.isascii()
    assert re.findall(r"[$%]\{[^}]*\}", text) == ["${account_id}"] * text.count("${account_id}")
    return json.loads(text.replace("${account_id}", ACCOUNT))


def _listed(value: str | list[str]) -> list[str]:
    return [value] if isinstance(value, str) else value


def decision(policy: dict, principal_arn: str, action: str) -> str:
    """What this key policy says, for this policy's shape only.

    "denied" when a Deny matches (nothing in IAM or a grant overrides it);
    "iam" when the root statement delegates the call to the caller's IAM
    policies (a principal of this account); "none" otherwise.
    """
    allowed = False
    for statement in policy["Statement"]:
        assert set(statement) <= {"Sid", "Effect", "Principal", "Action", "Resource", "Condition"}
        assert statement["Resource"] == "*"
        if not any(fnmatch.fnmatchcase(action, pattern) for pattern in _listed(statement["Action"])):
            continue
        principals = _listed(statement["Principal"]["AWS"])
        account = principal_arn.split(":")[4]
        if "*" not in principals and f"arn:aws:iam::{account}:root" not in principals:
            continue
        condition = statement.get("Condition", {})
        assert set(condition) <= {"ArnNotEquals"}
        exempt = _listed(condition.get("ArnNotEquals", {}).get("aws:PrincipalArn", []))
        if principal_arn in exempt:
            continue
        if statement["Effect"] == "Deny":
            return "denied"
        allowed = True
    return "iam" if allowed else "none"


def test_the_template_renders_the_pinned_policy():
    assert rendered() == PINNED


@pytest.mark.parametrize("action", ["kms:Sign", "kms:Verify", "kms:GetPublicKey"])
@pytest.mark.parametrize(
    "principal", [DEPLOY_ROLE, ROOT, RUNTIME_ROLE, f"arn:aws:iam::{ACCOUNT}:role/sentinelops-remediation-flow"]
)
def test_only_the_admin_user_may_use_the_key(principal, action):
    assert decision(rendered(), principal, action) == "denied"
    assert decision(rendered(), ADMIN_USER, action) == "iam"


@pytest.mark.parametrize(
    "action",
    [
        "kms:PutKeyPolicy",
        "kms:GetKeyPolicy",
        "kms:DescribeKey",
        "kms:ListResourceTags",
        "kms:TagResource",
        "kms:ScheduleKeyDeletion",
        "kms:CancelKeyDeletion",
        "kms:GetParametersForImport",
        "kms:ImportKeyMaterial",
        "kms:DisableKey",
    ],
)
@pytest.mark.parametrize("principal", [ADMIN_USER, DEPLOY_ROLE, ROOT])
def test_the_admin_user_the_deploy_role_and_root_manage_it(principal, action):
    """Left to IAM by the root statement: no lockout, and Terraform can read and update it."""
    assert decision(rendered(), principal, action) == "iam"


def test_no_other_account_gets_anything():
    for action in ("kms:Sign", "kms:PutKeyPolicy", "kms:DescribeKey"):
        assert decision(rendered(), OTHER_ACCOUNT_ROLE, action) in ("none", "denied")
    assert decision(rendered(), OTHER_ACCOUNT_ROLE, "kms:PutKeyPolicy") == "none"


def test_the_key_applies_this_template_and_nothing_overrides_it():
    tf = KEY_FILE.read_text(encoding="utf-8")
    key = tf[tf.index('resource "aws_kms_external_key" "github_app"') :]
    key = key[: key.index("\n}\n")]
    assert (
        '  policy = templatefile("${path.module}/github_app_key_policy.json.tftpl", {\n'
        "    account_id = data.aws_caller_identity.current.account_id\n"
        "  })\n"
    ) in key
    assert "bypass_policy_lockout_safety_check" not in key
    others = [p for p in INFRA.rglob("*.tf") if ".terraform" not in p.parts]
    for path in others:
        text = path.read_text(encoding="utf-8")
        # A separate key-policy resource would replace this one; a grant would add uses.
        assert 'resource "aws_kms_key_policy"' not in text, path.name
        assert 'resource "aws_kms_grant"' not in text, path.name
        if path != TEMPLATE and "github_app_key_policy.json.tftpl" in text:
            assert path == KEY_FILE, path.name
