"""check_app.py, step 11's script, against GitHub's answers as its docs show them: what passes, what fails, what it prints."""

import json

import check_app
from fake_github import INSTALLATION_ID, TOKEN, FakeGitHub, FakeKMS, app

from sentinelops_remediation import github

MAIN_SHA = "6cbadbbee3d734b9e90d442ae81a4f4cbc993038"
APP = {"slug": "sentinelops-remediation", "permissions": dict(github.PERMISSIONS), "events": []}
INSTALLATIONS = [
    {"id": INSTALLATION_ID, "account": {"login": "BigMuscleSmoe"}, "repository_selection": "selected", "permissions": dict(github.PERMISSIONS)}
]
REPOSITORIES = {"total_count": 1, "repositories": [{"full_name": github.REPO}]}
PROTECTED = [{"type": "update"}, {"type": "deletion"}, {"type": "non_fast_forward"}]


def github_answering(app_answer: dict | None = None, settings: dict | None = None) -> FakeGitHub:
    fake = FakeGitHub()
    fake.add("GET", github.url("app"), 200, app_answer or APP)
    fake.add("GET", github.url("app_installations", query={"per_page": 100}), 200, INSTALLATIONS)
    fake.add_token()
    fake.add("GET", github.url("installation_repositories", query={"per_page": 100}), 200, REPOSITORIES)
    fake.add("GET", github.url("branch_rules", {"branch": "main"}), 200, PROTECTED)
    fake.add("GET", github.url("branch_rules", {"branch": "release/1.43.0"}), 200, PROTECTED)
    fake.add("GET", github.url("repository"), 200, settings or {"full_name": github.REPO, "allow_auto_merge": False})
    fake.add_revoke()
    return fake


def run(fake: FakeGitHub, **kwargs: object) -> tuple[bool, list[str]]:
    printed: list[str] = []
    kms = FakeKMS()
    ok = check_app.run(app(fake, kms=kms), printed.append, transport=fake, **kwargs)  # type: ignore[arg-type]
    return ok, printed


def test_every_check_passes_on_the_app_as_set_up() -> None:
    fake = github_answering()
    ok, printed = run(fake)
    assert ok and printed[-1] == "ALL PASSED"
    assert [line.split()[0] for line in printed[:-1]] == ["PASS"] * 7
    assert fake.unused() == []
    assert fake.sent()[-1] == ("DELETE", "https://api.github.com/installation/token")


def test_a_fifth_permission_fails_and_the_token_is_still_revoked() -> None:
    fake = github_answering(app_answer={**APP, "permissions": {**github.PERMISSIONS, "workflows": "write"}})
    ok, printed = run(fake)
    assert not ok and printed[0].startswith("FAIL") and printed[-1] == "1 FAILED"
    assert fake.sent()[-1] == ("DELETE", "https://api.github.com/installation/token")


def test_auto_merge_unseen_is_a_skip_not_a_pass() -> None:
    ok, printed = run(github_answering(settings={"full_name": github.REPO}))
    assert ok and any(line.startswith("SKIP  auto-merge") for line in printed)


def test_it_prints_no_token_jwt_or_id() -> None:
    fake = github_answering()
    _, printed = run(fake)
    output = "\n".join(printed)
    jwt = fake.requests[0].headers["Authorization"].removeprefix("Bearer ")
    assert TOKEN not in output and jwt not in output and str(INSTALLATION_ID) not in output


def test_the_apps_own_attempts_pass_only_when_github_refuses_them() -> None:
    fake = github_answering()
    fake.add("GET", github.url("get_ref", {"branch": "main"}), 200, {"ref": "refs/heads/main", "object": {"type": "commit", "sha": MAIN_SHA}})
    base = f"{github.API}/repos/{github.REPO}"
    for _ in check_app.MERGE_METHODS:
        fake.add("PUT", f"{base}/pulls/1/merge", 405, {"message": "Repository rule violations found"})
    fake.add("GET", f"{base}/git/ref/heads/release/merge-test", 200, {"object": {"sha": MAIN_SHA}})
    fake.add("POST", f"{base}/git/refs", 422, {"message": "Repository rule violations found"})
    fake.add("POST", f"{base}/git/refs", 201, {"ref": "refs/heads/sentinelops/app-check"})
    fake.add("DELETE", f"{base}/git/refs/heads/sentinelops/app-check", 204)
    ok, printed = run(fake, attempts=True, test_pr=1)
    assert ok, printed
    creates = [json.loads(r.body or b"")["ref"] for r in fake.requests if r.method == "POST" and r.url.endswith("/git/refs")]
    assert creates == ["refs/heads/merge-test-app", "refs/heads/sentinelops/app-check"]
    assert fake.unused() == []


def test_a_merge_that_goes_through_fails() -> None:
    fake = github_answering()
    fake.add("GET", github.url("get_ref", {"branch": "main"}), 200, {"ref": "refs/heads/main", "object": {"type": "commit", "sha": MAIN_SHA}})
    base = f"{github.API}/repos/{github.REPO}"
    fake.add("PUT", f"{base}/pulls/1/merge", 200, {"merged": True})
    for _ in check_app.MERGE_METHODS[1:]:
        fake.add("PUT", f"{base}/pulls/1/merge", 405, {})
    fake.add("GET", f"{base}/git/ref/heads/release/merge-test", 200, {"object": {"sha": "0" * 40}})
    fake.add("POST", f"{base}/git/refs", 422, {})
    fake.add("POST", f"{base}/git/refs", 201, {})
    fake.add("DELETE", f"{base}/git/refs/heads/sentinelops/app-check", 204)
    ok, printed = run(fake, attempts=True, test_pr=1)
    assert not ok
    assert [line for line in printed if line.startswith("FAIL")] == [
        "FAIL  the App merging PR 1 by merge: refused (HTTP 200)",
        "FAIL  release/merge-test still at main's commit: nothing merged",
    ]
