"""check_app.py, step 11's script, against GitHub's answers as recorded (the App's own, 2026-11-25): what passes, fails, prints."""

import json

import check_app
from fake_github import INSTALLATION_ID, TOKEN, FakeGitHub, FakeKMS, app, recorded

from sentinelops_remediation import github

MAIN_SHA = "776d432dce254ea28bb45acfafc2cc3fdd9d5c2d"  # main after step 7
BASE_SHA = "6cbadbbee3d734b9e90d442ae81a4f4cbc993038"  # release/merge-test, made from main before it
APP = recorded("app")["json"]
SETTINGS = recorded("repository")["json"]


def github_answering(app_answer: dict | None = None, settings: dict | None = None) -> FakeGitHub:
    """GitHub as recorded on 2026-11-25, as the App: every answer step 11 reads."""
    fake = FakeGitHub()
    fake.add_recorded("app", app_answer)
    fake.add_recorded("app_installations")
    fake.add_token()
    fake.add_recorded("installation_repositories")
    fake.add_recorded("branch_rules-main")
    fake.add_recorded("branch_rules-release-1.43.0")
    fake.add_recorded("repository", settings)
    fake.add_revoke()
    return fake


def test_the_recorded_app_is_the_one_step_11_passed() -> None:
    assert APP["permissions"] == github.PERMISSIONS and APP["events"] == []
    (install,) = recorded("app_installations")["json"]
    assert install["id"] == INSTALLATION_ID and install["repository_selection"] == "selected"
    assert SETTINGS["allow_auto_merge"] is False


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
    unseen = {k: v for k, v in SETTINGS.items() if k != "allow_auto_merge"}
    ok, printed = run(github_answering(settings=unseen))
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
    fake.add("GET", f"{base}/git/ref/heads/release/merge-test", 200, {"object": {"sha": BASE_SHA}})
    for _ in check_app.MERGE_METHODS:
        fake.add("PUT", f"{base}/pulls/1/merge", 405, {"message": "Repository rule violations found"})
    fake.add("GET", f"{base}/git/ref/heads/release/merge-test", 200, {"object": {"sha": BASE_SHA}})
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
    fake.add("GET", f"{base}/git/ref/heads/release/merge-test", 200, {"object": {"sha": BASE_SHA}})
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
        "FAIL  release/merge-test at the same commit after the attempts: nothing merged",
    ]


def test_the_base_is_compared_with_itself_not_with_main() -> None:
    """main moves on (step 7's push did); only a change to the base's own commit is a merge."""
    assert BASE_SHA != MAIN_SHA
    fake = github_answering()
    fake.add("GET", github.url("get_ref", {"branch": "main"}), 200, {"ref": "refs/heads/main", "object": {"type": "commit", "sha": MAIN_SHA}})
    base = f"{github.API}/repos/{github.REPO}"
    fake.add("GET", f"{base}/git/ref/heads/release/merge-test", 200, {"object": {"sha": BASE_SHA}})
    for _ in check_app.MERGE_METHODS:
        fake.add("PUT", f"{base}/pulls/1/merge", 405, {})
    fake.add("GET", f"{base}/git/ref/heads/release/merge-test", 200, {"object": {"sha": MAIN_SHA}})
    fake.add("POST", f"{base}/git/refs", 422, {})
    fake.add("POST", f"{base}/git/refs", 201, {})
    fake.add("DELETE", f"{base}/git/refs/heads/sentinelops/app-check", 204)
    ok, printed = run(fake, attempts=True, test_pr=1)
    assert not ok
    assert [line for line in printed if line.startswith("FAIL")] == [
        "FAIL  release/merge-test at the same commit after the attempts: nothing merged",
    ]


def test_a_missing_base_fails_and_no_merge_is_tried() -> None:
    fake = github_answering()
    fake.add("GET", github.url("get_ref", {"branch": "main"}), 200, {"ref": "refs/heads/main", "object": {"type": "commit", "sha": MAIN_SHA}})
    base = f"{github.API}/repos/{github.REPO}"
    fake.add("GET", f"{base}/git/ref/heads/release/merge-test", 404, {"message": "Not Found"})
    fake.add("POST", f"{base}/git/refs", 422, {})
    fake.add("POST", f"{base}/git/refs", 201, {})
    fake.add("DELETE", f"{base}/git/refs/heads/sentinelops/app-check", 204)
    ok, printed = run(fake, attempts=True, test_pr=1)
    assert not ok
    assert not [r for r in fake.requests if r.method == "PUT"]
    assert [line for line in printed if line.startswith("FAIL")] == ["FAIL  release/merge-test not found: no merge attempted"]


def test_it_runs_as_a_script_without_pytests_path() -> None:
    """Its documented command: `uv run python check_app.py ...` finds the hooks' scanner by itself."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    here = Path(check_app.__file__).parent
    done = subprocess.run([sys.executable, "check_app.py", "--help"], cwd=here, env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-300:]
    assert "--installation-id" in done.stdout
