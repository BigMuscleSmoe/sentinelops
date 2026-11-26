"""GitHub's answers for the flow's writes, queued on a FakeGitHub: the fix branch, its file, the PR, its checks.

Shapes as GitHub's REST docs give them, the check run's taken from the one
recorded on main (check_runs-main-checkout-api.json). URLs come from the
client's own github.url(), so each is an endpoint the client may call.
"""

import base64
from typing import Any

from answers import PRICING_1_42_0, PRICING_1_43_0
from fake_github import FakeGitHub, recorded

from sentinelops_remediation import github
from sentinelops_remediation.body import CHECK

FIX = "sentinelops/fix-1.43.0-pricing"
BASE = "release/1.43.0"
PATH = "services/checkout-api/checkout/pricing.py"
BASE_SHA = "ee9ef23a8d7eb51670b5c00e655a9d1814e0a640"
HEAD_SHA = "4d3c2b1a" * 5
BLOB_1_43_0 = "a1" * 20
BLOB_1_42_0 = "b2" * 20
NUMBER = 7
INCIDENT_ID = "checkout-api-20261125T043000Z"
RUN = recorded("check_runs-main-checkout-api")["json"]["check_runs"][0]


def pull(body: str = "", draft: bool = True, number: int = NUMBER) -> dict[str, Any]:
    return {
        "number": number,
        "html_url": f"https://github.com/BigMuscleSmoe/sentinelops/pull/{number}",
        "node_id": f"PR_kwTest{number}",
        "draft": draft,
        "state": "open",
        "head": {"ref": FIX, "sha": HEAD_SHA},
        "base": {"ref": BASE},
        "body": body,
    }


def ref(sha: str, branch: str = FIX) -> dict[str, Any]:
    return {"ref": f"refs/heads/{branch}", "object": {"type": "commit", "sha": sha}}


def file(text: str, blob: str) -> dict[str, Any]:
    content = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return {"type": "file", "path": PATH, "encoding": "base64", "content": content, "sha": blob}


def check_run(status: str, conclusion: str | None, started: str = "2026-11-25T04:40:00Z", run_id: int = 1) -> dict:
    return {**RUN, "id": run_id, "name": CHECK, "status": status, "conclusion": conclusion, "started_at": started}


def url(name: str, params: dict | None = None, query: dict | None = None) -> str:
    return github.url(name, params, query)


PULLS = url("list_pulls", query={"head": f"BigMuscleSmoe:{FIX}", "base": BASE, "state": "open"})
BRANCH = url("get_ref", {"branch": FIX})
FILE_ON_BRANCH = url("get_file", {"path": PATH}, {"ref": FIX})
COMMENTS = url("list_comments", {"number": NUMBER}, {"per_page": 100, "page": 1})
CHECKS = url("check_runs", {"sha": HEAD_SHA}, {"check_name": CHECK, "per_page": 100})


def new_fix(fake: FakeGitHub, opened_body: str = "<!-- incident: " + INCIDENT_ID + " -->\nbody") -> None:
    """A first run: no branch yet, so it's made from the base, the file committed, and a draft PR opened."""
    fake.add("GET", BRANCH, 404, {"message": "Not Found"})
    fake.add("POST", url("create_ref"), 201, ref(BASE_SHA))
    fake.add("GET", BRANCH, 200, ref(BASE_SHA))
    fake.add("GET", FILE_ON_BRANCH, 200, file(PRICING_1_43_0, BLOB_1_43_0))
    fake.add("PUT", url("put_file", {"path": PATH}), 200, {"commit": {"sha": HEAD_SHA}})
    fake.add("GET", FILE_ON_BRANCH, 200, file(PRICING_1_42_0, BLOB_1_42_0))
    fake.add("GET", BRANCH, 200, ref(HEAD_SHA))
    fake.add("GET", PULLS, 200, [])
    fake.add("POST", url("create_pull"), 201, pull(opened_body))
    fake.add("GET", PULLS, 200, [pull(opened_body)])


def checks(fake: FakeGitHub, *runs: list[dict]) -> None:
    """One check-runs answer per poll, in order."""
    for listed in runs:
        fake.add("GET", CHECKS, 200, {"total_count": len(listed), "check_runs": listed})


def ready(fake: FakeGitHub) -> None:
    fake.add("POST", url("ready_for_review"), 200, {"data": {"markPullRequestReadyForReview": {"pullRequest": {"isDraft": False}}}})
