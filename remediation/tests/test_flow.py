"""The durable flow end to end, in the SDK's own test runner: each outcome, what it writes, and what it keeps.

The runner checkpoints like Lambda's service does and starts a new invocation
after every wait (time skipped), so each test also shows the token made in an
invocation is revoked when that invocation ends, a wait's suspension included.
"""

import base64
import json
from typing import Any

import pytest
from answers import PRICING_1_42_0, PRICING_1_43_0, hypothesis
from aws_durable_execution_sdk_python import DurableContext, durable_execution
from aws_durable_execution_sdk_python_testing import DurableFunctionTestRunner
from botocore.exceptions import ClientError
from fake_github import TOKEN, FakeGitHub, app, recorded
from fix_github import (
    BLOB_1_42_0,
    BRANCH,
    CHECKS,
    COMMENTS,
    FILE_ON_BRANCH,
    HEAD_SHA,
    INCIDENT_ID,
    NUMBER,
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

from sentinelops_core import trace as fmt
from sentinelops_remediation import body, flow, github
from sentinelops_remediation.incidents import Answered

ACCOUNT = "123456789012"  # AWS's documented placeholder, which the hooks allow in this file
EVENT = {"service": "checkout-api", "incident_id": INCIDENT_ID}
OPENED_AT = "2026-11-25T04:30:00.123Z"
ALARM = "sentinelops-checkout-errors"


class FakeIncidents:
    def __init__(self, item: Answered | None, fail_record: bool = False) -> None:
        self.item = item
        self.fail_record = fail_record
        self.gets: list[tuple[str, str]] = []
        self.records: list[tuple[str, str, dict]] = []

    def get(self, service: str, incident_id: str) -> Answered | None:
        self.gets.append((service, incident_id))
        return self.item

    def record(self, service: str, incident_id: str, result: dict) -> None:
        if self.fail_record:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "arn:aws:x"}}, "UpdateItem")
        self.records.append((service, incident_id, dict(result)))


def answered(status: str = "done", **changes: Any) -> Answered:
    return Answered(INCIDENT_ID, OPENED_AT, ALARM, status, hypothesis(**changes).model_dump_json())


def validation(fake: FakeGitHub) -> None:
    """GitHub as recorded for validate: the release's head, 1.43.0's file, both branches' rules."""
    fake.add_recorded("get_ref-release-1.43.0")
    fake.add_recorded("get_file-pricing-at-release-1.43.0")
    fake.add_recorded("branch_rules-main")
    fake.add_recorded("branch_rules-release-1.43.0")


def tokens(fake: FakeGitHub, n: int = 20) -> None:
    for _ in range(n):
        fake.add_token()
        fake.add_revoke()


class FakeTrace:
    """Stands in for the trace table: each event the flow puts, by step, or the error it's told to raise."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.events: list[tuple[str, str, fmt.EventModel]] = []
        self.fail = fail

    def put(self, incident_id: str, step: str, at: Any, event: fmt.EventModel) -> None:
        if self.fail is not None:
            raise self.fail
        self.events.append((incident_id, step, event))

    def steps(self) -> list[tuple[str, fmt.EventModel]]:
        assert {incident for incident, _, _ in self.events} <= {INCIDENT_ID}
        return [(step, event) for _, step, event in self.events]


class Flow:
    """The flow's handler, as handler.py builds it, over fakes; every App it made counted."""

    def __init__(self, incidents: FakeIncidents, fake: FakeGitHub, trace: Any = None) -> None:
        self.incidents, self.fake, self.made = incidents, fake, 0

        def make() -> github.GitHubApp:
            self.made += 1
            return app(fake)

        @durable_execution
        def handler(event: Any, context: DurableContext) -> dict[str, Any]:
            return flow.invocation(event, context, incidents, make, ACCOUNT, trace)

        self.handler = handler

    def run(self, event: Any = EVENT) -> Any:
        with DurableFunctionTestRunner(handler=self.handler) as runner:
            return runner.run(input=json.dumps(event), execution_timeout=120)

    def granted(self) -> int:
        return sum(1 for m, u in self.fake.sent() if m == "POST" and u.endswith("/access_tokens"))

    def revoked(self) -> int:
        return sum(1 for m, _ in self.fake.sent() if m == "DELETE")


def result_of(run: Any) -> dict[str, Any]:
    assert run.execution_status.name == "SUCCEEDED", run.error
    return json.loads(run.result)


def names(run: Any) -> list[str]:
    return [op.name for op in run.operations]


def everything_kept(run: Any) -> str:
    """Every checkpointed result and the execution's own: what the service would store and show."""
    kept = [run.result or ""]
    for op in run.get_all_operations():
        kept.append(str(getattr(op, "result", "") or ""))
    return "\n".join(kept)


def test_a_passing_check_opens_the_pr_and_marks_it_ready() -> None:
    incidents, fake = FakeIncidents(answered()), FakeGitHub()
    tokens(fake)
    validation(fake)
    new_fix(fake, body.marker(INCIDENT_ID) + "\nbody")
    checks(fake, [check_run("in_progress", None)], [check_run("completed", "success")])
    ready(fake)
    runner = Flow(incidents, fake)

    run = runner.run()

    result = result_of(run)
    assert result["status"] == "ready" and result["pr_number"] == NUMBER and result["pushed"] is True
    assert names(run) == ["load", "open", "wait-1", "checks-1", "wait-2", "checks-2", "conclude", "record"]
    [(service, incident_id, recorded_map)] = incidents.records
    assert (service, incident_id) == ("checkout-api", INCIDENT_ID)
    assert recorded_map == {
        "status": "ready",
        "pr": "opened",
        "pr_number": NUMBER,
        "pr_url": f"https://github.com/BigMuscleSmoe/sentinelops/pull/{NUMBER}",
        "pushed": True,
        "checks": "success",
        "finished_at": recorded_map["finished_at"],
    }
    # Three invocations (one per wait), each one's token revoked as it ended; no write sent twice.
    assert runner.granted() == runner.revoked() == 3
    assert [m for m, u in fake.sent() if m in ("PUT", "POST") and "access_tokens" not in u].count("POST") == 3
    assert TOKEN not in everything_kept(run)


def test_a_failing_check_leaves_a_draft_with_one_comment() -> None:
    incidents, fake = FakeIncidents(answered()), FakeGitHub()
    tokens(fake)
    validation(fake)
    new_fix(fake, body.marker(INCIDENT_ID) + "\nbody")
    checks(fake, [check_run("completed", "failure")])
    fake.add("GET", COMMENTS, 200, [])
    fake.add("POST", url("create_comment", {"number": NUMBER}), 201, {"id": 3})

    result = result_of(Flow(incidents, fake).run())

    assert result["status"] == "checks_failed"
    assert (result["conclusion"], result["failed_check"]) == ("failure", body.CHECK)
    assert not any(u == url("ready_for_review") for _, u in fake.sent())
    assert all(key[1].endswith(("/access_tokens", "/installation/token")) for key in fake.unused())


def test_a_check_that_never_finishes_times_out_after_fifteen_polls() -> None:
    incidents, fake = FakeIncidents(answered()), FakeGitHub()
    tokens(fake)
    validation(fake)
    new_fix(fake, body.marker(INCIDENT_ID) + "\nbody")
    checks(fake, *([[check_run("in_progress", None)]] * flow.POLLS))
    fake.add("GET", COMMENTS, 200, [])
    fake.add("POST", url("create_comment", {"number": NUMBER}), 201, {"id": 4})
    runner = Flow(incidents, fake)

    run = runner.run()

    assert result_of(run)["status"] == "checks_timeout"
    assert names(run)[-3:] == [f"checks-{flow.POLLS}", "conclude", "record"]
    posted = [json.loads(r.body) for r in fake.requests if r.url == url("create_comment", {"number": NUMBER})]
    assert posted == [{"body": body.checks_comment(body.Incident(INCIDENT_ID, ALARM, OPENED_AT), None)}]
    assert runner.granted() == runner.revoked() == flow.POLLS + 1


def test_a_check_that_cant_be_read_is_polled_again() -> None:
    incidents, fake = FakeIncidents(answered()), FakeGitHub()
    tokens(fake)
    validation(fake)
    new_fix(fake, body.marker(INCIDENT_ID) + "\nbody")
    for _ in range(4):
        fake.add("GET", CHECKS, 502)
    checks(fake, [check_run("completed", "success")])
    ready(fake)

    run = Flow(incidents, fake).run()

    assert result_of(run)["status"] == "ready"
    assert "checks-2" in names(run) and "Unavailable: check_runs HTTP 502" in str(run.get_step("checks-1").result)


@pytest.mark.parametrize(
    "item",
    [
        answered(status="failed"),
        answered(status="refused"),
        answered(confidence=0.2),
        answered(fault_location="upstream"),
        answered(remediation=None),
        answered(halted_by="cost_limit"),
    ],
    ids=["investigation-failed", "refused", "low-confidence", "upstream", "no-patch", "halted"],
)
def test_what_can_open_pr_refuses_never_reaches_github(item: Answered) -> None:
    incidents, fake = FakeIncidents(item), FakeGitHub()
    runner = Flow(incidents, fake)

    run = runner.run()

    assert result_of(run)["status"] == "no_pr"
    assert names(run) == ["load", "record"]
    assert runner.made == 0 and fake.requests == []
    assert incidents.records[0][2]["status"] == "no_pr"


def test_a_missing_incident_is_a_failure_recorded_where_it_can_be() -> None:
    incidents, fake = FakeIncidents(None), FakeGitHub()
    result = result_of(Flow(incidents, fake).run())
    assert (result["status"], result["error"]) == ("failed", "no such incident")
    assert fake.requests == []


def test_a_stored_answer_that_isnt_a_hypothesis_fails_closed() -> None:
    item = Answered(INCIDENT_ID, OPENED_AT, ALARM, "done", '{"root_cause": "x"}')
    result = result_of(Flow(FakeIncidents(item), FakeGitHub()).run())
    assert (result["status"], result["error"]) == ("failed", "the stored answer isn't a Hypothesis")


def test_a_file_on_github_that_isnt_the_one_read_is_rejected_and_nothing_written() -> None:
    incidents, fake = FakeIncidents(answered()), FakeGitHub()
    tokens(fake)
    fake.add_recorded("get_ref-release-1.43.0")
    changed = {**recorded("get_file-pricing-at-release-1.43.0")["json"]}
    changed["content"] = base64.b64encode((PRICING_1_43_0 + "# changed\n").encode("utf-8")).decode("ascii")
    fake.add_recorded("get_file-pricing-at-release-1.43.0", changed)
    runner = Flow(incidents, fake)

    result = result_of(runner.run())

    assert result["status"] == "rejected" and result["rejected_check"] == "base"
    assert {m for m, _ in fake.sent()} <= {"GET", "POST", "DELETE"}
    assert [u for m, u in fake.sent() if m == "POST"] == [github.url("installation_token", {"installation_id": 4242})]
    assert runner.granted() == runner.revoked() == 1


def test_github_down_is_a_failure_with_only_its_endpoint_and_status() -> None:
    incidents, fake = FakeIncidents(answered()), FakeGitHub()
    tokens(fake)
    for _ in range(4):
        fake.add("GET", github.url("get_ref", {"branch": "release/1.43.0"}), 502)

    result = result_of(Flow(incidents, fake).run())

    assert (result["status"], result["error"]) == ("failed", "Unavailable: get_ref HTTP 502")
    assert incidents.records[0][2]["error"] == "Unavailable: get_ref HTTP 502"


def test_another_incidents_pr_gets_a_comment_then_waits_on_the_same_check() -> None:
    incidents, fake = FakeIncidents(answered()), FakeGitHub()
    tokens(fake)
    validation(fake)
    other = body.marker("checkout-api-20261124T010000Z") + "\nbody"
    fake.add("GET", BRANCH, 200, ref(HEAD_SHA))
    fake.add("GET", FILE_ON_BRANCH, 200, file(PRICING_1_42_0, BLOB_1_42_0))
    fake.add("GET", BRANCH, 200, ref(HEAD_SHA))
    fake.add("GET", PULLS, 200, [pull(other)])
    fake.add("GET", COMMENTS, 200, [])
    fake.add("POST", url("create_comment", {"number": NUMBER}), 201, {"id": 5})
    checks(fake, [check_run("completed", "success")])
    ready(fake)

    result = result_of(Flow(incidents, fake).run())

    assert (result["status"], result["pr"], result["pushed"]) == ("ready", "commented", False)


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"service": "checkout-api"},
        {"service": "payments-api", "incident_id": "payments-api-20261125T043000Z"},
        {"service": "checkout-api", "incident_id": "payments-api-20261125T043000Z"},
        {"service": "checkout-api", "incident_id": "checkout-api-2026"},
        {**EVENT, "hypothesis": "{}"},
        ["checkout-api", INCIDENT_ID],
    ],
    ids=["empty", "no-id", "other-service", "other-services-id", "bad-id", "extra-key", "not-a-dict"],
)
def test_only_the_triggers_event_is_taken(event: Any) -> None:
    incidents, fake = FakeIncidents(answered()), FakeGitHub()
    result = result_of(Flow(incidents, fake).run(event))
    assert result["status"] == "failed" and incidents.gets == [] and fake.requests == []


def test_a_record_that_fails_still_ends_the_flow_with_its_code() -> None:
    incidents = FakeIncidents(answered(status="failed"), fail_record=True)
    result = result_of(Flow(incidents, FakeGitHub()).run())
    assert (result["status"], result["record"]) == ("no_pr", "ConditionalCheckFailedException")
    assert "arn" not in json.dumps(result)


def test_steps_are_never_retried_by_the_sdk() -> None:
    assert flow.ONCE.retry_strategy is not None
    decision = flow.ONCE.retry_strategy(RuntimeError("x"), 1)
    assert decision.should_retry is False


# --- the trace: each step's event for the page --------------------------------------------


def opened_and_ready(fake: FakeGitHub) -> None:
    tokens(fake)
    validation(fake)
    new_fix(fake, body.marker(INCIDENT_ID) + "\nbody")
    checks(fake, [check_run("in_progress", None)], [check_run("completed", "success")])
    ready(fake)


def test_each_step_writes_its_event_once_in_order() -> None:
    incidents, fake, trace = FakeIncidents(answered()), FakeGitHub(), FakeTrace()
    opened_and_ready(fake)

    result_of(Flow(incidents, fake, trace).run())

    assert trace.steps() == [
        ("load", fmt.HandedOff()),
        ("open", fmt.PrOpened(number=NUMBER)),
        ("checks-1", fmt.PrChecks(check=body.CHECK, state="pending", poll=1)),
        ("checks-2", fmt.PrChecks(check=body.CHECK, state="success", poll=2)),
        ("conclude", fmt.PrReady(number=NUMBER)),
    ]
    keys = [fmt.flow_key(step) for step, _ in trace.steps()]
    assert keys == sorted(keys) == ["flow#00", "flow#02", "flow#03", "flow#04", "flow#20"]


def test_a_trace_that_fails_changes_nothing_the_flow_does(capsys: pytest.CaptureFixture[str]) -> None:
    traced, plain = FakeGitHub(), FakeGitHub()
    opened_and_ready(traced)
    opened_and_ready(plain)
    error = ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "arn:aws:dynamodb:x"}}, "PutItem")
    with_trace = FakeIncidents(answered())

    result = result_of(Flow(with_trace, traced, FakeTrace(fail=error)).run())
    expected = result_of(Flow(FakeIncidents(answered()), plain).run())

    assert {k: v for k, v in result.items() if k != "finished_at"} == {
        k: v for k, v in expected.items() if k != "finished_at"
    }
    assert [(m, u) for m, u in traced.sent()] == [(m, u) for m, u in plain.sent()]
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if "trace_write_failed" in line]
    assert [line["step"] for line in lines] == ["load", "open", "checks-1", "checks-2", "conclude"]
    assert {line["error"] for line in lines} == {"ResourceNotFoundException"}
    assert "arn:aws" not in json.dumps(lines)


def test_a_same_fix_is_traced_and_recorded_with_whether_it_commented() -> None:
    incidents, fake, trace = FakeIncidents(answered()), FakeGitHub(), FakeTrace()
    tokens(fake)
    validation(fake)
    other = body.marker("checkout-api-20261124T010000Z") + "\nbody"
    fake.add("GET", BRANCH, 200, ref(HEAD_SHA))
    fake.add("GET", FILE_ON_BRANCH, 200, file(PRICING_1_42_0, BLOB_1_42_0))
    fake.add("GET", BRANCH, 200, ref(HEAD_SHA))
    fake.add("GET", PULLS, 200, [pull(other)])
    # Another incident's same-fix comment, made today by the flow's clock: this one's is capped.
    today = flow.datetime.now(flow.UTC).date().isoformat()
    third = body.marker("checkout-api-20261126T120000Z") + "\nsame fix"
    fake.add("GET", COMMENTS, 200, [{"body": third, "created_at": f"{today}T00:00:01Z"}])
    checks(fake, [check_run("completed", "success")])
    ready(fake)

    result = result_of(Flow(incidents, fake, trace).run())

    assert (result["pr"], result["commented"]) == ("commented", False)
    assert incidents.records[0][2]["commented"] is False
    assert not any(u == url("create_comment", {"number": NUMBER}) for _, u in fake.sent())
    assert trace.steps()[1] == ("open", fmt.PrSameFix(number=NUMBER, commented=False))


def test_a_rejected_patch_and_a_missing_incident_are_traced_by_their_names() -> None:
    rejected, missing = FakeTrace(), FakeTrace()
    fake = FakeGitHub()
    tokens(fake)
    fake.add_recorded("get_ref-release-1.43.0")
    changed = {**recorded("get_file-pricing-at-release-1.43.0")["json"]}
    changed["content"] = base64.b64encode((PRICING_1_43_0 + "# changed\n").encode("utf-8")).decode("ascii")
    fake.add_recorded("get_file-pricing-at-release-1.43.0", changed)

    result_of(Flow(FakeIncidents(answered()), fake, rejected).run())
    result_of(Flow(FakeIncidents(None), FakeGitHub(), missing).run())

    assert rejected.steps() == [("load", fmt.HandedOff()), ("open", fmt.PrRejected(check="base"))]
    assert missing.steps() == [("load", fmt.HandedOff()), ("load-result", fmt.FlowFailed(error="no such incident"))]


def test_an_answer_that_opens_no_pr_is_traced_as_handed_off_only() -> None:
    trace = FakeTrace()
    result_of(Flow(FakeIncidents(answered(confidence=0.2)), FakeGitHub(), trace).run())
    assert trace.steps() == [("load", fmt.HandedOff())]


def test_the_trace_table_writes_each_event_at_its_steps_slot_with_7_days_to_live() -> None:
    sent: list[dict[str, Any]] = []

    class Client:
        def put_item(self, **request: Any) -> dict[str, Any]:
            sent.append(request)
            return {}

    at = flow.datetime(2026, 11, 26, 18, 38, 29, 453000, tzinfo=flow.UTC)
    flow.TraceTable(Client(), "sentinelops-trace").put(INCIDENT_ID, "checks-1", at, fmt.PrChecks(check=body.CHECK, state="success", poll=1))

    [request] = sent
    assert request["TableName"] == "sentinelops-trace"
    item = request["Item"]
    assert (item["pk"], item["sk"], item["at"]) == ({"S": INCIDENT_ID}, {"S": "flow#03"}, {"S": "2026-11-26T18:38:29.453Z"})
    assert item["expires_at"] == {"N": str(int(at.timestamp()) + 7 * 24 * 3600)}
    assert fmt.read(item)[2] == fmt.PrChecks(check=body.CHECK, state="success", poll=1)
