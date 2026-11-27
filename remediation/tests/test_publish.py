"""The flow's writes: each made once, each looked for first, and only what the fix needs."""

import base64
import json

import pytest
from answers import PRICING_1_42_0, PRICING_1_43_0
from fake_github import FakeGitHub, app
from fix_github import (
    BASE,
    BASE_SHA,
    BLOB_1_42_0,
    BLOB_1_43_0,
    BRANCH,
    CHECKS,
    COMMENTS,
    FILE_ON_BRANCH,
    FIX,
    HEAD_SHA,
    INCIDENT_ID,
    NUMBER,
    PATH,
    PULLS,
    check_run,
    checks,
    file,
    new_fix,
    pull,
    ready,
    ref,
    url,
)

from sentinelops_remediation import body, publish
from sentinelops_remediation.body import Incident
from sentinelops_remediation.github import Unavailable
from sentinelops_remediation.publish import Pull
from sentinelops_remediation.validate import Validated

INCIDENT = Incident(INCIDENT_ID, "sentinelops-checkout-errors", "2026-11-25T04:30:00.123Z")
OTHER = Incident("checkout-api-20261126T010000Z", "sentinelops-checkout-errors", "2026-11-26T01:00:00.000Z")
FIXED = Validated("1.43.0", BASE, BASE_SHA, PATH, PRICING_1_42_0, "diff", FIX)
MINE = body.marker(INCIDENT_ID) + "\nthe body"
TODAY = "2026-11-26"


def writes(fake: FakeGitHub) -> list[tuple[str, str, dict]]:
    """Every request but a read or the token's: method, URL, and body."""
    sent = []
    for request in fake.requests:
        if request.method == "GET" or request.url.endswith("/access_tokens") or request.method == "DELETE":
            continue
        sent.append((request.method, request.url, json.loads(request.body or b"{}")))
    return sent


def test_a_new_fix_makes_the_branch_commits_the_file_and_opens_a_draft() -> None:
    fake = FakeGitHub()
    fake.add_token()
    new_fix(fake, MINE)

    result = publish.open_or_update(app(fake), FIXED, MINE, INCIDENT, TODAY)

    assert result == Pull("opened", NUMBER, f"https://github.com/BigMuscleSmoe/sentinelops/pull/{NUMBER}", f"PR_kwTest{NUMBER}", True, HEAD_SHA, True)
    assert fake.unused() == []
    made = writes(fake)
    assert [(method, sent_to) for method, sent_to, _ in made] == [
        ("POST", url("create_ref")),
        ("PUT", url("put_file", {"path": PATH})),
        ("POST", url("create_pull")),
    ]
    assert made[0][2] == {"ref": f"refs/heads/{FIX}", "sha": BASE_SHA}
    put = made[1][2]
    assert (put["branch"], put["sha"]) == (FIX, BLOB_1_43_0)
    assert base64.b64decode(put["content"]).decode("utf-8") == PRICING_1_42_0
    assert put["message"] == body.commit_message(FIXED, INCIDENT)
    assert made[2][2] == {"title": body.title(FIXED, INCIDENT), "body": MINE, "head": FIX, "base": BASE, "draft": True}


def test_a_second_try_finds_what_the_first_wrote_and_writes_nothing() -> None:
    """A step runs at least once: everything is there already, so nothing is made twice."""
    fake = FakeGitHub()
    fake.add_token()
    fake.add("GET", BRANCH, 200, ref(HEAD_SHA))
    fake.add("GET", FILE_ON_BRANCH, 200, file(PRICING_1_42_0, BLOB_1_42_0))
    fake.add("GET", BRANCH, 200, ref(HEAD_SHA))
    fake.add("GET", PULLS, 200, [pull(MINE)])

    result = publish.open_or_update(app(fake), FIXED, MINE, INCIDENT, TODAY)

    assert (result.status, result.pushed) == ("opened", False)
    assert writes(fake) == [] and fake.unused() == []


def test_another_incidents_open_pr_gets_one_comment_not_a_twin() -> None:
    fake = FakeGitHub()
    fake.add_token()
    fake.add("GET", BRANCH, 200, ref(HEAD_SHA))
    fake.add("GET", FILE_ON_BRANCH, 200, file(PRICING_1_42_0, BLOB_1_42_0))
    fake.add("GET", BRANCH, 200, ref(HEAD_SHA))
    fake.add("GET", PULLS, 200, [pull(MINE)])
    fake.add("GET", url("list_comments", {"number": NUMBER}, {"per_page": 100, "page": 1}), 200, [{"body": "a person's note"}])
    fake.add("POST", url("create_comment", {"number": NUMBER}), 201, {"id": 1})

    result = publish.open_or_update(app(fake), FIXED, "the other body", OTHER, TODAY)

    assert (result.status, result.commented) == ("commented", True)
    [(method, sent_to, sent)] = writes(fake)
    assert sent == {"body": body.same_fix_comment(FIXED, OTHER, pushed=False)}
    assert sent["body"].startswith(body.marker(OTHER.id))


def test_the_comment_is_made_once() -> None:
    fake = FakeGitHub()
    fake.add_token()
    fake.add("GET", BRANCH, 200, ref(HEAD_SHA))
    fake.add("GET", FILE_ON_BRANCH, 200, file(PRICING_1_42_0, BLOB_1_42_0))
    fake.add("GET", BRANCH, 200, ref(HEAD_SHA))
    fake.add("GET", PULLS, 200, [pull(MINE)])
    fake.add("GET", COMMENTS, 200, [{"body": body.marker(OTHER.id) + "\nalready said"}])

    found = publish.open_or_update(app(fake), FIXED, "the other body", OTHER, TODAY)
    assert (found.status, found.commented) == ("commented", True)  # its own, from an earlier try
    assert writes(fake) == []


def same_fix_pr(fake: FakeGitHub, comments: list[dict]) -> None:
    """The fix's branch and file already there, and the PR another incident opened, with these comments."""
    fake.add_token()
    fake.add("GET", BRANCH, 200, ref(HEAD_SHA))
    fake.add("GET", FILE_ON_BRANCH, 200, file(PRICING_1_42_0, BLOB_1_42_0))
    fake.add("GET", BRANCH, 200, ref(HEAD_SHA))
    fake.add("GET", PULLS, 200, [pull(MINE)])
    fake.add("GET", COMMENTS, 200, comments)


def test_a_same_fix_comment_made_today_by_another_incident_caps_this_ones() -> None:
    fake = FakeGitHub()
    third = body.marker("checkout-api-20261126T120000Z") + "\nsame fix"
    same_fix_pr(fake, [{"body": third, "created_at": "2026-11-26T12:01:07Z"}])

    found = publish.open_or_update(app(fake), FIXED, "the other body", OTHER, TODAY)

    assert (found.status, found.commented, found.number) == ("commented", False, NUMBER)
    assert writes(fake) == [] and fake.unused() == []


@pytest.mark.parametrize(
    "comment",
    [
        {"body": body.marker("checkout-api-20261125T120000Z") + "\nsame fix", "created_at": "2026-11-25T23:59:59Z"},
        {"body": body.marker(INCIDENT_ID, "checks") + "\nchecks", "created_at": "2026-11-26T09:00:00Z"},
        {"body": "a person's note, <!-- incident: checkout-api-20261126T120000Z --> quoted", "created_at": "2026-11-26T09:00:00Z"},
        {"body": body.marker("checkout-api-20261126T120000Z"), "created_at": None},
    ],
    ids=["yesterdays", "a-checks-comment", "not-its-first-line", "no-time"],
)
def test_only_another_incidents_same_fix_comment_from_today_counts(comment: dict) -> None:
    fake = FakeGitHub()
    same_fix_pr(fake, [comment])
    fake.add("POST", url("create_comment", {"number": NUMBER}), 201, {"id": 3})

    found = publish.open_or_update(app(fake), FIXED, "the other body", OTHER, TODAY)

    assert (found.status, found.commented) == ("commented", True)
    [(_, _, sent)] = writes(fake)
    assert sent["body"].startswith(body.marker(OTHER.id))


def test_this_incidents_own_comment_counts_as_made_whatever_else_is_there() -> None:
    fake = FakeGitHub()
    third = {"body": body.marker("checkout-api-20261126T120000Z"), "created_at": "2026-11-26T12:01:07Z"}
    same_fix_pr(fake, [third, {"body": body.marker(OTHER.id) + "\nmine", "created_at": "2026-11-26T13:00:00Z"}])

    found = publish.open_or_update(app(fake), FIXED, "the other body", OTHER, TODAY)

    assert found.commented is True and writes(fake) == []


def test_a_create_that_timed_out_but_landed_isnt_made_twice() -> None:
    fake = FakeGitHub()
    fake.add_token()
    fake.add("GET", BRANCH, 200, ref(HEAD_SHA))
    fake.add("GET", FILE_ON_BRANCH, 200, file(PRICING_1_42_0, BLOB_1_42_0))
    fake.add("GET", BRANCH, 200, ref(HEAD_SHA))
    fake.add("GET", PULLS, 200, [])
    fake.add("POST", url("create_pull"), 502)
    fake.add("GET", PULLS, 200, [pull(MINE)])

    assert publish.open_or_update(app(fake), FIXED, MINE, INCIDENT, TODAY).status == "opened"
    assert [method for method, _, _ in writes(fake)] == ["POST"]  # sent once: a create never retries a 5xx


def test_a_create_that_never_landed_fails() -> None:
    fake = FakeGitHub()
    fake.add_token()
    fake.add("GET", BRANCH, 200, ref(HEAD_SHA))
    fake.add("GET", FILE_ON_BRANCH, 200, file(PRICING_1_42_0, BLOB_1_42_0))
    fake.add("GET", BRANCH, 200, ref(HEAD_SHA))
    fake.add("GET", PULLS, 200, [])
    fake.add("POST", url("create_pull"), 502)
    fake.add("GET", PULLS, 200, [])

    with pytest.raises(Unavailable):
        publish.open_or_update(app(fake), FIXED, MINE, INCIDENT, TODAY)


def test_a_branch_made_a_moment_ago_is_used() -> None:
    """422 from create_ref: it exists (another try made it), so it's read and used."""
    fake = FakeGitHub()
    fake.add_token()
    fake.add("GET", BRANCH, 404, {"message": "Not Found"})
    fake.add("POST", url("create_ref"), 422, {"message": "Reference already exists"})
    fake.add("GET", BRANCH, 200, ref(BASE_SHA))
    fake.add("GET", FILE_ON_BRANCH, 200, file(PRICING_1_42_0, BLOB_1_42_0))
    fake.add("GET", BRANCH, 200, ref(HEAD_SHA))
    fake.add("GET", PULLS, 200, [pull(MINE)])

    assert publish.open_or_update(app(fake), FIXED, MINE, INCIDENT, TODAY).pushed is False


@pytest.mark.parametrize(
    ("listed", "state", "conclusion"),
    [
        ([], "pending", None),
        ([check_run("in_progress", None)], "pending", None),
        ([check_run("completed", "success")], "success", "success"),
        ([check_run("completed", "failure")], "failure", "failure"),
        ([check_run("completed", "cancelled")], "failure", "cancelled"),
        ([check_run("completed", "made-up")], "failure", "unknown"),
        # A re-run: the latest attempt decides.
        ([check_run("completed", "failure", "2026-11-25T04:40:00Z", 1), check_run("completed", "success", "2026-11-25T04:50:00Z", 2)], "success", "success"),
        ([check_run("completed", "success", "2026-11-25T04:40:00Z", 1), check_run("in_progress", None, "2026-11-25T04:50:00Z", 2)], "pending", None),
        ([{**check_run("completed", "success"), "name": "test (agent)"}], "pending", None),
    ],
)
def test_the_waited_on_check_as_github_reports_it(listed, state, conclusion) -> None:
    fake = FakeGitHub()
    fake.add_token()
    checks(fake, listed)
    assert publish.check_state(app(fake), HEAD_SHA) == {"state": state, "conclusion": conclusion}
    assert fake.sent()[-1] == ("GET", CHECKS)


def test_ready_for_review_only_takes_a_draft_out_of_draft() -> None:
    fake = FakeGitHub()
    fake.add_token()
    ready(fake)
    drafted = Pull("opened", NUMBER, "u", "PR_kwTest7", True, HEAD_SHA, True)
    publish.mark_ready(app(fake), drafted)
    publish.mark_ready(app(fake), Pull("commented", NUMBER, "u", "PR_kwTest7", False, HEAD_SHA, False))
    assert [m for m, _ in fake.sent() if m == "POST"] == ["POST", "POST"]  # the token, and one mutation
    assert fake.unused() == []


def test_a_failed_check_is_reported_once() -> None:
    drafted = Pull("opened", NUMBER, "u", "PR_kwTest7", True, HEAD_SHA, True)
    fake = FakeGitHub()
    fake.add_token()
    fake.add("GET", COMMENTS, 200, [])
    fake.add("POST", url("create_comment", {"number": NUMBER}), 201, {"id": 2})
    publish.report_checks(app(fake), drafted, INCIDENT, "failure")
    assert writes(fake) == [("POST", url("create_comment", {"number": NUMBER}), {"body": body.checks_comment(INCIDENT, "failure")})]

    again = FakeGitHub()
    again.add_token()
    again.add("GET", COMMENTS, 200, [{"body": body.checks_comment(INCIDENT, "failure")}])
    publish.report_checks(app(again), drafted, INCIDENT, "failure")
    assert writes(again) == []
