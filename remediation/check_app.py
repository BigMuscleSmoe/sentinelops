"""Evening 4, step 11: the App end to end against GitHub, run by hand on the user's go.

    uv run python check_app.py --client-id <id> --installation-id <n> [--attempts --test-pr <n>]

From remediation/, with the sentinelops profile (kms:Sign on the App's key).
Neither ID is a secret, and neither goes in the repo. It checks, as the flow's
own client:
  - GET /app takes the KMS-signed JWT; the App holds exactly the four
    permissions and subscribes to no events
  - one installation, on selected repositories, with the same four
  - a token scoped to this repository and the four (the client refuses and
    revokes any other grant)
  - the installation sees this repository only
  - main and release/1.43.0 refuse updates, deletions, and force pushes
  - auto-merge is off, if GitHub shows that setting to the installation
and then revokes the token, whatever happened. With --attempts, as the App
itself, test-only calls that the client's list doesn't have:
  - merging the test PR, by each method: refused, and its base's commit the
    same after the attempts as before them
  - creating merge-test-app: refused
  - creating and deleting sentinelops/app-check: both allowed
It prints no token, JWT, or ID. Exit 0 only if every check passed.
"""

import argparse
import json
import re
import sys
from collections.abc import Callable
from typing import Any

from sentinelops_remediation import github
from sentinelops_remediation.validate import protected

PROFILE = "sentinelops"
REGION = "us-east-1"
RELEASE = "release/1.43.0"
MERGE_BASE = "release/merge-test"  # step 1's throwaway base: the test PR merges into it
MERGE_METHODS = ("merge", "squash", "rebase")
_TWELVE = re.compile(r"(?<![0-9A-Za-z.])[0-9]{12}(?![0-9A-Za-z]|\.[0-9])")  # the hooks' 12-digit rule


def run(
    app: github.GitHubApp,
    out: Callable[[str], None],
    attempts: bool = False,
    test_pr: int | None = None,
    transport: github.Transport = github.urllib_transport,
) -> bool:
    """Every check, each printed PASS, FAIL, or SKIP; True if none failed. The token is revoked at the end."""
    failed: list[str] = []

    def report(ok: bool | None, what: str) -> None:
        word = "SKIP" if ok is None else "PASS" if ok else "FAIL"
        out(_TWELVE.sub("<12 digits>", f"{word}  {what}"))
        if ok is False:
            failed.append(what)

    try:
        about = app.call("app")
        report(
            about.get("permissions") == github.PERMISSIONS and about.get("events") == [],
            "the App: its JWT accepted, exactly the four permissions, no events",
        )
        installs = app.call("app_installations", query={"per_page": 100})
        one = installs[0] if isinstance(installs, list) and len(installs) == 1 else {}
        count = len(installs) if isinstance(installs, list) else "?"
        report(
            one.get("account", {}).get("login") == github.OWNER
            and one.get("repository_selection") == "selected"
            and one.get("permissions") == github.PERMISSIONS,
            f"installations: {count}, on selected repositories, the four permissions",
        )
        repos = app.call("installation_repositories", query={"per_page": 100})
        names = [r.get("full_name") for r in repos.get("repositories", [])] if isinstance(repos, dict) else []
        report(
            names == [github.REPO] and repos.get("total_count") == 1,
            f"a token for {github.REPO} and the four; it sees {len(names)} repository",
        )
        for branch in ("main", RELEASE):
            report(protected(app, branch), f"{branch} refuses updates, deletions, and force pushes")
        settings = app.call("repository")
        if isinstance(settings, dict) and "allow_auto_merge" in settings:
            report(settings["allow_auto_merge"] is False, "auto-merge is off")
        else:
            report(None, "auto-merge: the setting isn't shown to the installation (step 6's check stands)")
        if attempts:
            _attempts(app, transport, report, test_pr)
    except (github.GitHubError, github.NotAllowed, github.SigningFailed) as error:
        report(False, f"stopped: {error}")
    finally:
        try:
            app.revoke()
            report(True, "the token revoked")
        except github.GitHubError as error:
            report(False, f"revoking the token: {error}")
    out(f"{'ALL PASSED' if not failed else f'{len(failed)} FAILED'}")
    return not failed


def _attempts(
    app: github.GitHubApp, transport: github.Transport, report: Callable[[bool | None, str], None], test_pr: int | None
) -> None:
    """As the App, the calls its list leaves out, each expected refused, and one branch it may make."""
    token = app._installation_token()  # test-only: these calls go around the client's list on purpose
    main = app.head_commit("main")

    def reply(method: str, path: str, body: dict[str, Any] | None = None) -> github.Response:
        headers, data = github.encoded(f"Bearer {token}", body)
        return transport(github.Request(method, f"{github.API}/repos/{github.REPO}{path}", headers, data))

    def send(method: str, path: str, body: dict[str, Any] | None = None) -> int:
        return reply(method, path, body).status

    def head(branch: str) -> str | None:
        answer = reply("GET", f"/git/ref/heads/{branch}")
        return json.loads(answer.body).get("object", {}).get("sha") if answer.status == 200 else None

    if test_pr is None:
        report(None, "merging the test PR: no --test-pr given")
    elif (before := head(MERGE_BASE)) is None:
        report(False, f"{MERGE_BASE} not found: no merge attempted")
    else:
        for method in MERGE_METHODS:
            status = send("PUT", f"/pulls/{test_pr}/merge", {"merge_method": method})
            report(status not in (200, 201), f"the App merging PR {test_pr} by {method}: refused (HTTP {status})")
        report(head(MERGE_BASE) == before, f"{MERGE_BASE} at the same commit after the attempts: nothing merged")
    status = send("POST", "/git/refs", {"ref": "refs/heads/merge-test-app", "sha": main})
    report(status not in (200, 201), f"the App creating merge-test-app: refused (HTTP {status})")
    if status in (200, 201):
        report(False, "merge-test-app exists: delete it by hand (git push origin --delete merge-test-app)")
    status = send("POST", "/git/refs", {"ref": "refs/heads/sentinelops/app-check", "sha": main})
    report(status == 201, f"the App creating sentinelops/app-check: allowed (HTTP {status})")
    if status == 201:
        status = send("DELETE", "/git/refs/heads/sentinelops/app-check")
        report(status == 204, f"the App deleting sentinelops/app-check: allowed (HTTP {status})")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--installation-id", required=True, type=int)
    parser.add_argument("--attempts", action="store_true", help="also the App's own merge and branch attempts")
    parser.add_argument("--test-pr", type=int, help="the throwaway PR's number, for the merge attempts")
    args = parser.parse_args(argv)
    import boto3  # here, not at the top: the tests import this module without AWS

    kms = boto3.Session(profile_name=PROFILE, region_name=REGION).client("kms")
    app = github.GitHubApp(args.client_id, args.installation_id, kms)
    return 0 if run(app, print, attempts=args.attempts, test_pr=args.test_pr) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
