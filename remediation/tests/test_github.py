"""The GitHub client: the App's JWT, its tokens, the fixed endpoint list, the parameter and body rules, and retries."""

import base64
import json
from datetime import UTC, datetime

import boto3
import pytest
from botocore.stub import Stubber
from fake_github import CLIENT_ID, NOW, TOKEN, TOKEN_ANSWER, FakeGitHub, FakeKMS, app, recorded

from sentinelops_remediation import github

FIX = "sentinelops/fix-1.43.0-pricing"
FILE = "services/checkout-api/checkout/pricing.py"
SHA = "ee9ef23a8d7eb51670b5c00e655a9d1814e0a640"


def part(segment: str) -> object:
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


# --- the App's JWT ------------------------------------------------------------------


def test_the_jwt_is_rs256_signed_by_kms_for_the_client_id() -> None:
    kms = boto3.client("kms", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")
    header = github._b64url(b'{"alg":"RS256","typ":"JWT"}')
    claims = github._b64url(json.dumps({"iat": int(NOW) - 60, "exp": int(NOW) + 540, "iss": CLIENT_ID}, separators=(",", ":")).encode())
    with Stubber(kms) as stub:
        # The Stubber checks the call against KMS's real API model.
        stub.add_response(
            "sign",
            {"Signature": b"signature-bytes", "SigningAlgorithm": "RSASSA_PKCS1_V1_5_SHA_256"},
            {
                "KeyId": "alias/sentinelops-github-app",
                "Message": f"{header}.{claims}".encode(),
                "MessageType": "RAW",
                "SigningAlgorithm": "RSASSA_PKCS1_V1_5_SHA_256",
            },
        )
        jwt = app(FakeGitHub(), kms=kms).app_jwt()
        stub.assert_no_pending_responses()
    first, second, third = jwt.split(".")
    assert part(first) == {"alg": "RS256", "typ": "JWT"}
    assert part(second) == {"iat": int(NOW) - 60, "exp": int(NOW) + 540, "iss": CLIENT_ID}
    assert third == github._b64url(b"signature-bytes")


def test_a_kms_failure_names_its_code_only() -> None:
    kms = boto3.client("kms", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")
    with Stubber(kms) as stub:
        stub.add_client_error(
            "sign",
            service_error_code="KMSInvalidStateException",
            service_message="arn:aws:kms:us-east-1:123456789012:key/abc is pending import.",
        )
        with pytest.raises(github.SigningFailed) as caught:
            app(FakeGitHub(), kms=kms).app_jwt()
    assert str(caught.value) == "kms:Sign failed (KMSInvalidStateException)"
    # Raised outside the except block, so no traceback carries AWS's message.
    assert caught.value.__context__ is None


def test_the_client_takes_only_a_client_id_and_an_installation_id() -> None:
    for client_id in ("", "Iv23/../x", "a" * 65, 7):
        with pytest.raises(ValueError):
            github.GitHubApp(client_id, 1, FakeKMS())  # type: ignore[arg-type]
    for installation_id in (0, -1, "1", True):
        with pytest.raises(ValueError):
            github.GitHubApp(CLIENT_ID, installation_id, FakeKMS())  # type: ignore[arg-type]


# --- installation tokens ---------------------------------------------------------------


def test_a_token_is_asked_for_this_repository_and_the_four_permissions_only() -> None:
    fake = FakeGitHub()
    fake.add_token()
    fake.add_recorded("branch_rules-main")
    fake.add_recorded("branch_rules-main")
    client = app(fake)
    client.branch_rules("main")
    client.branch_rules("main")
    token_request, first, second = fake.requests
    assert token_request.method == "POST"
    assert json.loads(token_request.body or b"") == {
        "repositories": ["sentinelops"],
        "permissions": {"checks": "read", "contents": "write", "metadata": "read", "pull_requests": "write"},
    }
    assert token_request.headers["Authorization"].startswith("Bearer ey")  # the JWT
    # One token, held for both calls.
    assert first.headers["Authorization"] == second.headers["Authorization"] == f"Bearer {TOKEN}"
    for request in fake.requests:
        assert request.headers["Accept"] == "application/vnd.github+json"
        assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
        assert request.headers["User-Agent"] == "sentinelops-remediation"
        assert request.url.startswith("https://api.github.com/")


def test_a_token_near_its_expiry_is_replaced() -> None:
    fake = FakeGitHub()
    now = [NOW]
    fake.add_token()
    fake.add_token({**TOKEN_ANSWER, "token": "second-test-token", "expires_at": "2026-11-25T05:00:00Z"})
    fake.add_recorded("branch_rules-main")
    fake.add_recorded("branch_rules-main")
    client = app(fake, clock=lambda: now[0])
    client.branch_rules("main")
    now[0] = datetime(2026, 11, 25, 3, 56, tzinfo=UTC).timestamp()  # 4 minutes before the first expires
    client.branch_rules("main")
    assert fake.requests[-1].headers["Authorization"] == "Bearer second-test-token"


@pytest.mark.parametrize(
    "grant",
    [
        {**TOKEN_ANSWER, "permissions": {**github.PERMISSIONS, "administration": "write"}},
        {**TOKEN_ANSWER, "permissions": {**github.PERMISSIONS, "contents": "read"}},
        {**TOKEN_ANSWER, "repository_selection": "all"},
        {**TOKEN_ANSWER, "repositories": [{"full_name": github.REPO}, {"full_name": "BigMuscleSmoe/other"}]},
        {**TOKEN_ANSWER, "repositories": []},
        {**TOKEN_ANSWER, "expires_at": "2026-11-25T04:00:00"},
    ],
    ids=["more-permissions", "less-permissions", "all-repositories", "two-repositories", "no-repositories", "no-zone"],
)
def test_a_token_with_any_other_grant_is_revoked_and_refused(grant: dict[str, object]) -> None:
    fake = FakeGitHub()
    fake.add_token(grant)
    fake.add_revoke()
    with pytest.raises(github.WrongGrant):
        app(fake).branch_rules("main")
    revoke = fake.requests[-1]
    assert (revoke.method, revoke.url) == ("DELETE", "https://api.github.com/installation/token")
    assert revoke.headers["Authorization"] == f"Bearer {TOKEN}"


def test_a_grant_without_metadata_is_the_same_grant() -> None:
    fake = FakeGitHub()
    fake.add_token({**TOKEN_ANSWER, "permissions": {k: v for k, v in github.PERMISSIONS.items() if k != "metadata"}})
    fake.add_recorded("branch_rules-main")
    assert app(fake).branch_rules("main")


def test_revoke_ends_the_token_and_the_next_call_asks_again() -> None:
    fake = FakeGitHub()
    fake.add_token()
    fake.add_recorded("branch_rules-main")
    fake.add_revoke()
    client = app(fake)
    client.branch_rules("main")
    client.revoke()
    client.revoke()  # nothing held: nothing sent
    assert fake.sent()[-1] == ("DELETE", "https://api.github.com/installation/token")
    assert len(fake.requests) == 3


def test_no_token_is_shown_by_the_client_or_its_errors() -> None:
    fake = FakeGitHub()
    fake.add_token()
    fake.add("GET", github.url("branch_rules", {"branch": "main"}), 404, {"message": "Not Found"})
    client = app(fake)
    with pytest.raises(github.Refused) as caught:
        client.branch_rules("main")
    assert TOKEN not in repr(client) and TOKEN not in str(caught.value)
    assert str(caught.value) == "branch_rules: HTTP 404 (Not Found)"


# --- the endpoint list -------------------------------------------------------------------


def test_the_endpoint_list_is_exactly_this() -> None:
    # Pinned: an endpoint added here is a change to what the App can do, made on purpose.
    assert {name: (e.method, e.path) for name, e in github.ENDPOINTS.items()} == {
        "app": ("GET", "/app"),
        "app_installations": ("GET", "/app/installations"),
        "installation_token": ("POST", "/app/installations/{installation_id}/access_tokens"),
        "revoke_token": ("DELETE", "/installation/token"),
        "installation_repositories": ("GET", "/installation/repositories"),
        "repository": ("GET", "/repos/{repo}"),
        "branch_rules": ("GET", "/repos/{repo}/rules/branches/{branch}"),
        "get_ref": ("GET", "/repos/{repo}/git/ref/heads/{branch}"),
        "create_ref": ("POST", "/repos/{repo}/git/refs"),
        "get_file": ("GET", "/repos/{repo}/contents/{path}"),
        "put_file": ("PUT", "/repos/{repo}/contents/{path}"),
        "list_pulls": ("GET", "/repos/{repo}/pulls"),
        "create_pull": ("POST", "/repos/{repo}/pulls"),
        "list_comments": ("GET", "/repos/{repo}/issues/{number}/comments"),
        "create_comment": ("POST", "/repos/{repo}/issues/{number}/comments"),
        "check_runs": ("GET", "/repos/{repo}/commits/{sha}/check-runs"),
        "ready_for_review": ("POST", "/graphql"),
    }


def test_nothing_on_the_list_can_merge_dispatch_or_administer() -> None:
    for name, endpoint in github.ENDPOINTS.items():
        assert "merge" not in endpoint.path, name
        for word in ("dispatches", "/actions", "/hooks", "/keys", "/collaborators", "/rulesets", "/protection",
                     "/environments", "/secrets", "/variables", "/deployments", "/releases", "/tags", "/forks"):
            assert word not in endpoint.path, (name, word)
        assert endpoint.method != "PATCH", name
        # The only delete ends the client's own token; no ref is ever updated or deleted.
        assert endpoint.method != "DELETE" or name == "revoke_token", name
    # Creates are never resent after a 5xx: one that landed would be made twice.
    assert {n for n, e in github.ENDPOINTS.items() if e.method in ("POST", "PUT") and not e.repeatable} == {
        "create_ref", "put_file", "create_pull", "create_comment",
    }


def test_the_one_graphql_call_is_the_fixed_ready_for_review_mutation() -> None:
    fake = FakeGitHub()
    fake.add_token()
    fake.add("POST", "https://api.github.com/graphql", 200, {"data": {"markPullRequestReadyForReview": {"pullRequest": {"isDraft": False}}}})
    app(fake).mark_ready_for_review("PR_kwDOtest")
    body = json.loads(fake.requests[-1].body or b"")
    assert body == {"query": github.READY_FOR_REVIEW, "variables": {"id": "PR_kwDOtest"}}
    assert "markPullRequestReadyForReview" in github.READY_FOR_REVIEW and "merge" not in github.READY_FOR_REVIEW.lower()


def test_a_graphql_error_is_a_refusal() -> None:
    fake = FakeGitHub()
    fake.add_token()
    fake.add("POST", "https://api.github.com/graphql", 200, {"errors": [{"message": "no"}]})
    with pytest.raises(github.Refused):
        app(fake).mark_ready_for_review("PR_kwDOtest")


@pytest.mark.parametrize("node_id", ["", "PR kw", "PR_kw\"}){mergePullRequest", "x" * 101, 7])
def test_a_node_id_outside_its_form_is_never_sent(node_id: object) -> None:
    fake = FakeGitHub()
    with pytest.raises(github.NotAllowed):
        app(fake).mark_ready_for_review(node_id)  # type: ignore[arg-type]
    assert fake.requests == []


@pytest.mark.parametrize("name", ["installation_token", "revoke_token", "ready_for_review", "merge", "merge_pull", "dispatch", ""])
def test_call_takes_only_listed_endpoints_without_their_own_method(name: str) -> None:
    fake = FakeGitHub()
    with pytest.raises(github.NotAllowed):
        app(fake).call(name)
    assert fake.requests == []


# --- parameters -----------------------------------------------------------------------------

HOSTILE_BRANCHES = [
    "main/../release/1.43.0", "release/1.43.0/../../x", f"{FIX}/../../main", "refs/heads/main", "release/9.9.9",
    "release/merge-test", "%2e%2e", "sentinelops/fix-1.43.0-Pricing", "sentinelops/other", "sentinelops/fix-1.43.0-pricing?x",
    "sentinelops/fix-1.43.0-pricing#x", "main ", "MAIN", "", 7,
]
HOSTILE_PATHS = [
    "services/checkout-api/sim/flags.py", "services/checkout-api/checkout/../handler.py", ".github/workflows/deploy.yml",
    "services/checkout-api/checkout/pricing.py?ref=main", "services/checkout-api/checkout/pricing.py#x", "checkout/pricing.py",
    "services/checkout-api//checkout/pricing.py", "services/checkout-api/checkout/Pricing.py", "/services/checkout-api/checkout/pricing.py",
    "services/checkout-api/checkout/%70ricing.py", "services\\checkout-api\\checkout\\pricing.py",
]


@pytest.mark.parametrize("branch", HOSTILE_BRANCHES)
def test_a_branch_outside_main_the_release_map_and_fix_branches_is_never_named(branch: object) -> None:
    for name in ("branch_rules", "get_ref"):
        with pytest.raises(github.NotAllowed):
            github.url(name, {"branch": branch})


@pytest.mark.parametrize("path", HOSTILE_PATHS)
def test_a_path_outside_checkouts_modules_is_never_named(path: str) -> None:
    with pytest.raises(github.NotAllowed):
        github.url("get_file", {"path": path}, {"ref": SHA})


@pytest.mark.parametrize(
    ("name", "params", "query"),
    [
        ("list_comments", {"number": True}, None),
        ("list_comments", {"number": 0}, None),
        ("list_comments", {"number": "1"}, None),
        ("check_runs", {"sha": "ee9ef23"}, None),
        ("check_runs", {"sha": SHA.upper()}, None),
        ("branch_rules", {}, None),
        ("branch_rules", {"branch": "main", "extra": 1}, None),
        ("get_file", {"path": FILE}, {"ref": "main..release/1.43.0"}),
        ("get_file", {"path": FILE}, {"sha": SHA}),
        ("list_pulls", {}, {"head": f"someone:{FIX}"}),
        ("list_pulls", {}, {"head": "BigMuscleSmoe:main"}),
        ("list_pulls", {}, {"base": "main"}),
        ("list_pulls", {}, {"state": "merged"}),
        ("check_runs", {"sha": SHA}, {"check_name": "x" * 101}),
        ("list_comments", {"number": 1}, {"per_page": 101}),
    ],
)
def test_a_parameter_outside_its_rule_is_never_sent(name: str, params: dict, query: dict | None) -> None:
    with pytest.raises(github.NotAllowed):
        github.url(name, params, query)


def test_listed_parameters_build_the_urls_github_documents() -> None:
    assert github.url("get_file", {"path": FILE}, {"ref": SHA}) == (
        f"https://api.github.com/repos/BigMuscleSmoe/sentinelops/contents/{FILE}?ref={SHA}"
    )
    assert github.url("list_pulls", query={"head": f"BigMuscleSmoe:{FIX}", "state": "open"}) == (
        "https://api.github.com/repos/BigMuscleSmoe/sentinelops/pulls?head=BigMuscleSmoe%3Asentinelops%2Ffix-1.43.0-pricing&state=open"
    )
    assert github.url("check_runs", {"sha": SHA}, {"check_name": "test (services/checkout-api)"}).endswith(
        f"/commits/{SHA}/check-runs?check_name=test+%28services%2Fcheckout-api%29"
    )
    assert github.url("get_ref", {"branch": "release/1.43.0"}).endswith("/git/ref/heads/release/1.43.0")


# --- write bodies -----------------------------------------------------------------------------

PR = {"title": "Fix", "body": "text", "head": FIX, "base": "release/1.43.0", "draft": True}


@pytest.mark.parametrize(
    ("name", "params", "body"),
    [
        ("create_ref", {}, {"ref": "refs/heads/main", "sha": SHA}),
        ("create_ref", {}, {"ref": "refs/heads/release/1.43.0", "sha": SHA}),
        ("create_ref", {}, {"ref": "refs/tags/v1.43.1", "sha": SHA}),
        ("create_ref", {}, {"ref": f"refs/heads/{FIX}", "sha": SHA, "force": True}),
        ("create_ref", {}, {"ref": f"refs/heads/{FIX}"}),
        ("put_file", {"path": FILE}, {"message": "m", "content": "eA==", "branch": "main"}),
        ("put_file", {"path": FILE}, {"message": "m", "content": "eA==", "branch": "release/1.43.0"}),
        ("put_file", {"path": FILE}, {"message": "m", "content": "eA=="}),
        ("put_file", {"path": FILE}, {"message": "m", "content": "eA==", "branch": FIX, "committer": {}}),
        ("create_pull", {}, {**PR, "base": "main"}),
        ("create_pull", {}, {**PR, "base": "release/merge-test"}),
        ("create_pull", {}, {**PR, "head": "release/1.43.0"}),
        ("create_pull", {}, {**PR, "draft": False}),
        ("create_pull", {}, {k: v for k, v in PR.items() if k != "draft"}),
        ("create_pull", {}, {**PR, "draft": "true"}),
        ("create_comment", {"number": 1}, {"body": "x", "extra": 1}),
        ("create_comment", {"number": 1}, None),
        ("branch_rules", {"branch": "main"}, {"x": 1}),
    ],
)
def test_a_write_outside_a_fix_branch_or_a_draft_into_a_release_is_never_sent(name: str, params: dict, body: dict | None) -> None:
    fake = FakeGitHub()
    with pytest.raises(github.NotAllowed):
        app(fake).call(name, params, body=body)
    assert fake.requests == []


def test_a_draft_pr_from_a_fix_branch_into_the_release_branch_is_sent() -> None:
    fake = FakeGitHub()
    fake.add_token()
    fake.add("POST", github.url("create_pull"), 201, {"number": 2, "draft": True})
    assert app(fake).call("create_pull", body=PR) == {"number": 2, "draft": True}
    assert json.loads(fake.requests[-1].body or b"") == PR


# --- retries ------------------------------------------------------------------------------------------

RULES_URL = github.url("branch_rules", {"branch": "main"})
COMMENT_URL = github.url("create_comment", {"number": 2})


def test_a_5xx_on_a_read_is_retried_three_times_with_backoff() -> None:
    fake, sleeps = FakeGitHub(), []
    fake.add_token()
    for _ in range(3):
        fake.add("GET", RULES_URL, 502)
    fake.add_recorded("branch_rules-main")
    assert app(fake, sleeps=sleeps).branch_rules("main")
    assert sleeps == [1.0, 2.0, 4.0]


def test_a_read_that_keeps_failing_is_unavailable_after_four_tries() -> None:
    fake, sleeps = FakeGitHub(), []
    fake.add_token()
    for _ in range(4):
        fake.add("GET", RULES_URL, 503)
    with pytest.raises(github.Unavailable) as caught:
        app(fake, sleeps=sleeps).branch_rules("main")
    assert caught.value.status == 503 and sleeps == [1.0, 2.0, 4.0]
    assert fake.sent().count(("GET", RULES_URL)) == 4


def test_a_timeout_on_a_read_is_retried() -> None:
    fake = FakeGitHub()
    fake.add_token()
    fake.fail("GET", RULES_URL)
    fake.add_recorded("branch_rules-main")
    assert app(fake).branch_rules("main")


@pytest.mark.parametrize("failure", ["502", "timeout"])
def test_a_create_is_never_resent_after_a_5xx_or_a_timeout(failure: str) -> None:
    fake, sleeps = FakeGitHub(), []
    fake.add_token()
    if failure == "502":
        fake.add("POST", COMMENT_URL, 502)
    else:
        fake.fail("POST", COMMENT_URL)
    with pytest.raises(github.Unavailable):
        app(fake, sleeps=sleeps).call("create_comment", {"number": 2}, body={"body": "x"})
    assert fake.sent().count(("POST", COMMENT_URL)) == 1 and sleeps == []


def test_a_rate_limit_is_retried_even_on_a_create_after_the_wait_github_asks() -> None:
    fake, sleeps = FakeGitHub(), []
    fake.add_token()
    fake.add("POST", COMMENT_URL, 429, {"message": "slow down"}, {"retry-after": "7"})
    fake.add("POST", COMMENT_URL, 403, {"message": "rate limit"}, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(NOW) + 30)})
    fake.add("POST", COMMENT_URL, 429, {}, {"retry-after": "3600"})
    fake.add("POST", COMMENT_URL, 201, {"id": 1})
    assert app(fake, sleeps=sleeps).call("create_comment", {"number": 2}, body={"body": "x"}) == {"id": 1}
    assert sleeps == [7.0, 30.0, 60.0]  # at most a minute


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_a_refusal_is_not_retried(status: int) -> None:
    fake, sleeps = FakeGitHub(), []
    fake.add_token()
    fake.add("GET", RULES_URL, status, {"message": "Repository rule violations found"})
    with pytest.raises(github.Refused) as caught:
        app(fake, sleeps=sleeps).branch_rules("main")
    assert caught.value.status == status and sleeps == []
    assert str(caught.value) == f"branch_rules: HTTP {status} (Repository rule violations found)"


def test_a_204_answers_none() -> None:
    fake = FakeGitHub()
    fake.add_token()
    fake.add("GET", RULES_URL, 204)
    assert app(fake).call("branch_rules", {"branch": "main"}) is None


def test_redirects_are_never_followed() -> None:
    # Following one would send the Authorization header wherever it pointed.
    handlers = [type(h) for h in github._OPENER.handlers]
    assert github._NoRedirect in handlers
    assert github._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://example.com/") is None


# --- the reads the validation step makes, on recorded answers ---------------------------------------------


def test_head_commit_and_file_at_read_the_recorded_answers() -> None:
    fake = FakeGitHub()
    fake.add_token()
    fake.add_recorded("get_ref-release-1.43.0")
    fake.add_recorded("get_file-pricing-at-release-1.43.0")
    client = app(fake)
    head = client.head_commit("release/1.43.0")
    assert head == SHA
    text = client.file_at(FILE, head).decode("utf-8")
    assert "def apply_promo" in text and "if promo is None" not in text  # 1.43.0's file


def test_an_answer_for_another_ref_or_file_is_refused() -> None:
    fake = FakeGitHub()
    fake.add_token()
    ref = recorded("get_ref-release-1.43.0")["json"]
    fake.add_recorded("get_ref-release-1.43.0", {**ref, "ref": "refs/heads/main"})
    fake.add_recorded("get_file-pricing-at-release-1.43.0", {"type": "dir", "path": FILE})
    client = app(fake)
    with pytest.raises(github.Refused):
        client.head_commit("release/1.43.0")
    with pytest.raises(github.Refused):
        client.file_at(FILE, SHA)
