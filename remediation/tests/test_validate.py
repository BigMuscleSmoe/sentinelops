"""The validation step, on GitHub's recorded answers: a right fix passes, and each check refuses what it's for."""

from pathlib import Path

import base64

import commit_scan
import pytest
from answers import ANSWER, NULL_CHECK, PATCH_OLD, PRICING_1_42_0, PRICING_1_43_0, REPO_ROOT, hypothesis, kept
from fake_github import FakeGitHub, app, recorded

from sentinelops_core import code
from sentinelops_remediation import github, validate
from sentinelops_remediation.validate import Rejected, Validated

ACCOUNT = "123456789012"  # AWS's documented placeholder, which the hooks allow in this file
FILE = "services/checkout-api/checkout/pricing.py"
SHA = "ee9ef23a8d7eb51670b5c00e655a9d1814e0a640"


def github_as_recorded(rules_on_release: list | None = None, file_content: str | None = None) -> FakeGitHub:
    """GitHub as it was recorded: the release branch's head, 1.43.0's pricing.py there, both branches' rules."""
    fake = FakeGitHub()
    fake.add_token()
    fake.add_recorded("get_ref-release-1.43.0")
    exchange = recorded("get_file-pricing-at-release-1.43.0")["json"]
    fake.add_recorded("get_file-pricing-at-release-1.43.0", None if file_content is None else {**exchange, "content": file_content})
    fake.add_recorded("branch_rules-main")
    fake.add_recorded("branch_rules-release-1.43.0", rules_on_release)
    return fake


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def test_a_right_fix_validates_against_the_release_branch() -> None:
    fake = github_as_recorded()
    result = validate.validate(hypothesis(), app(fake), ACCOUNT)
    assert result == Validated(
        version="1.43.0",
        base="release/1.43.0",
        base_sha=SHA,
        path=FILE,
        text=PRICING_1_42_0,  # the fix puts back exactly what 1.43.0 dropped
        diff=ANSWER["remediation"]["diff"],
        branch="sentinelops/fix-1.43.0-pricing",
    )
    assert fake.unused() == []
    # Reads only: the one POST is the token.
    assert {method for method, _ in fake.sent()} == {"GET", "POST"}
    assert [url for method, url in fake.sent() if method == "POST"] == [
        github.url("installation_token", {"installation_id": 4242})
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"confidence": 0.2},
        {"fault_location": "upstream"},
        {"running_version": "1.42.0"},
        {"remediation": None},
        {"remediation": {**kept(), "diff": None}},
        {"halted_by": "cost_limit"},
    ],
    ids=["low-confidence", "upstream", "patch-not-on-the-running-release", "no-patch", "never-checked", "halted"],
)
def test_nothing_the_gate_refuses_reaches_github(changes: dict) -> None:
    fake = FakeGitHub()
    assert validate.validate(hypothesis(**changes), app(fake), ACCOUNT) == Rejected("gate", "can_open_pr is False")
    assert fake.requests == []


def test_a_release_without_a_fix_branch_is_refused_before_github() -> None:
    # 1.42.0 is main's: main is never a base.
    patch = kept(text=PRICING_1_42_0.replace(NULL_CHECK, "    # promo may be None\n" + NULL_CHECK), version="1.42.0",
                 edits=[{"old": "    # promo may be None\n", "new": ""}])
    fake = FakeGitHub()
    result = validate.validate(hypothesis(remediation=patch, running_version="1.42.0"), app(fake), ACCOUNT)
    assert result == Rejected("release", "the release map gives this release no fix branch")
    assert fake.requests == []


def test_a_release_not_in_the_map_is_refused_before_github() -> None:
    fake = FakeGitHub()
    result = validate.validate(hypothesis(remediation=kept(version="1.44.0"), running_version="1.44.0"), app(fake), ACCOUNT)
    assert result == Rejected("release", "not a release in the release map")
    assert fake.requests == []


def test_a_file_on_github_that_isnt_the_one_read_is_refused() -> None:
    fake = github_as_recorded(file_content=b64(PRICING_1_42_0))
    result = validate.validate(hypothesis(), app(fake), ACCOUNT)
    assert result == Rejected("base", "the file at the base isn't the file the agent read")


def test_edits_that_no_longer_render_the_kept_diff_are_refused() -> None:
    tampered = {**kept(), "diff": kept()["diff"].replace("return subtotal_cents\n", "return 0\n")}
    result = validate.validate(hypothesis(remediation=tampered), app(github_as_recorded()), ACCOUNT)
    assert result == Rejected("patch", "the edits don't render the diff the run kept")


def test_the_patch_checks_run_again_on_githubs_bytes() -> None:
    # A file whose text has two copies of the edit's old text: the same check as in the run refuses it.
    doubled = PRICING_1_43_0 + "\n\n" + PATCH_OLD + "subtotal_cents\n"
    patch = {**kept(), "file_sha256": code.digest(doubled)}
    result = validate.validate(hypothesis(remediation=patch), app(github_as_recorded(file_content=b64(doubled))), ACCOUNT)
    assert result == Rejected("patch", "edit 1: old text is in the file more than once")


@pytest.mark.parametrize(
    "rules",
    [
        [],
        [{"type": "update"}, {"type": "deletion"}],
        [{"type": "update", "parameters": {"update_allows_fetch_and_merge": True}}, {"type": "deletion"}, {"type": "non_fast_forward"}],
    ],
    ids=["no-rules", "force-pushes-allowed", "fork-sync-allowed"],
)
def test_a_base_no_longer_protected_is_refused(rules: list) -> None:
    result = validate.validate(hypothesis(), app(github_as_recorded(rules_on_release=rules)), ACCOUNT)
    assert result == Rejected("rules", "release/1.43.0 no longer refuses updates, deletions, and force pushes")


def test_main_no_longer_protected_is_refused() -> None:
    fake = FakeGitHub()
    fake.add_token()
    fake.add_recorded("get_ref-release-1.43.0")
    fake.add_recorded("get_file-pricing-at-release-1.43.0")
    fake.add_recorded("branch_rules-main", [{"type": "creation"}])
    result = validate.validate(hypothesis(), app(fake), ACCOUNT)
    assert result == Rejected("rules", "main no longer refuses updates, deletions, and force pushes")


def test_the_fix_branch_isnt_protected_so_the_app_can_write_it() -> None:
    fake = FakeGitHub()
    fake.add_token()
    fake.add_recorded("branch_rules-sentinelops-fix-1.43.0-pricing")
    assert validate.protected(app(fake), "sentinelops/fix-1.43.0-pricing") is False


# Planted values, built at run time so this file itself holds none the hooks refuse.
AT = "@"
PLANTED = {
    "email": "ops" + AT + "corp-mail.io",
    "account-id": ACCOUNT,
    "12-digits": "4" * 12,
    "home-path": "C:" + "\\" + "Users" + "\\" + "someone" + "\\notes.txt",
    "public-ip": ".".join(["8", "8", "8", "8"]),
    "aws-key": "AKIA" + "Q" * 16,
    "anthropic-key": "sk-" + "ant-api03-" + "a" * 60,
    "arn": "arn" + ":aws:iam::" + "role/x",
}


@pytest.mark.parametrize("rule", sorted(PLANTED))
def test_everything_the_flow_will_post_is_scanned_and_findings_never_show_the_text(rule: str) -> None:
    planted = PLANTED[rule]
    result = validate.validate(
        hypothesis(), app(github_as_recorded()), ACCOUNT, {"pr body": f"Fix promo pricing.\nsee {planted} now", "commit message": "Fix"}
    )
    assert isinstance(result, Rejected) and result.check == "scan"
    assert result.reason == f"pr body:2: {rule}"
    assert planted not in result.reason


def test_the_patched_file_is_scanned_too() -> None:
    comment = "    # owner: " + "ops" + AT + "corp-mail.io\n"
    edits = [{"old": PATCH_OLD, "new": PATCH_OLD.replace("    return ", comment + NULL_CHECK + "    return ")}]
    result = validate.validate(hypothesis(remediation=kept(edits=edits)), app(github_as_recorded()), ACCOUNT)
    assert result == Rejected("scan", f"{FILE}:15: email")


def test_findings_past_five_are_counted() -> None:
    body = "\n".join(PLANTED["public-ip"] for _ in range(8))
    result = validate.validate(hypothesis(), app(github_as_recorded()), ACCOUNT, {"pr body": body})
    assert isinstance(result, Rejected) and result.reason.endswith("; and 3 more")


def test_example_addresses_and_noreply_pass_the_scan() -> None:
    texts = {"pr body": "Mail ops" + AT + "example.com or noreply" + AT + "github.com; host 10.0.0.1."}
    assert validate.scan(texts, ACCOUNT) == []


def test_github_failing_is_an_error_not_a_rejection() -> None:
    fake = FakeGitHub()
    fake.add_token()
    for _ in range(4):
        fake.add("GET", github.url("get_ref", {"branch": "release/1.43.0"}), 502)
    with pytest.raises(github.Unavailable):
        validate.validate(hypothesis(), app(fake), ACCOUNT)


@pytest.mark.parametrize("account", ["", "12345", "1234567890123", "12345678901a", None])
def test_the_account_id_is_required(account: object) -> None:
    with pytest.raises(ValueError):
        validate.validate(hypothesis(), app(FakeGitHub()), account)  # type: ignore[arg-type]


def test_fix_branches_are_named_by_release_and_module_only() -> None:
    assert validate.fix_branch("1.43.0", "checkout/pricing.py") == "sentinelops/fix-1.43.0-pricing"
    with pytest.raises(ValueError):
        validate.fix_branch("1.43.0", "checkout/Pricing.py")


def test_the_scan_is_the_commit_hooks_own_one_copy() -> None:
    assert Path(commit_scan.__file__).resolve() == REPO_ROOT / ".githooks" / "commit_scan.py"
    assert validate.commit_scan is commit_scan


def test_githubs_release_file_is_main_minus_the_null_check() -> None:
    # What the fixtures stand on: 1.43.0 on GitHub is 1.42.0 (main's checkout/) without one check.
    assert PRICING_1_42_0.replace(NULL_CHECK, "", 1) == PRICING_1_43_0
