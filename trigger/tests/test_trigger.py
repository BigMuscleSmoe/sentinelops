"""The trigger's flow: what reaches the runtime, in what order, and what the incident keeps."""

import gzip
import io
import json
import os
import re
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from botocore.response import StreamingBody
from botocore.stub import Stubber

from recorded_alarms import ERRORS, OCT_27, POOL
from triage import ALARM, OK, Change, Incident, ServiceState, Verdict, classify, iso
from trigger import (
    MAX_RECORD_GZ_BYTES,
    AgentRuntime,
    Trigger,
    answer_fields,
    new_session_id,
    parse_change,
)

INCIDENT = "checkout-api-20261027T190557Z"
SESSION = f"{INCIDENT}-session"
RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/sentinelops_agent-abc123"
HYPOTHESIS = {
    "root_cause": "Deploy 1.43.0 broke checkout for carts without a promo.",
    "confidence": 0.9,
    "evidence": ["TypeError at pricing.py:15 on every failing line."],
    "affected_component": "checkout-api 1.43.0",
    "runbook_cited": None,
}
DONE = {
    "outcome": "done",
    "alarm": {"name": ERRORS, "fired_at": "2026-10-27T19:05:57.026000+00:00", "replay": False},
    "tools_via": "gateway",
    "hypothesis": HYPOTHESIS,
    "failure": None,
    "tool_calls": ["get_metrics", "get_deploy_history", "get_recent_logs"],
    "model_calls": [{"prompt_tokens": 3389}, {"prompt_tokens": 11079}, {"prompt_tokens": 22315}],
    "input_tokens": 36783,
    "cost_usd": 0.005751,
    "seconds": 34.7,
    "messages": [],
}


def event(alarm: str, state: str, timestamp: str) -> dict[str, Any]:
    """An alarm's EventBridge event, as CloudWatch sends it."""
    return {
        "version": "0",
        "id": "6a7e8feb-b491-4cf7-a9f1-bf3703467718",
        "detail-type": "CloudWatch Alarm State Change",
        "source": "aws.cloudwatch",
        "account": "123456789012",
        "time": timestamp[:19] + "Z",
        "region": "us-east-1",
        "resources": [f"arn:aws:cloudwatch:us-east-1:123456789012:alarm:{alarm}"],
        "detail": {
            "alarmName": alarm,
            "state": {"value": state, "reason": "Threshold Crossed", "timestamp": timestamp},
            "previousState": {"value": OK if state == ALARM else ALARM, "timestamp": timestamp},
            "configuration": {"metrics": []},
        },
    }


FIRED = event(ERRORS, ALARM, "2026-10-27T19:05:57.026+0000")


class FakeStore:
    """The Store's interface over memory, with the real triage."""

    def __init__(self, calls: list) -> None:
        self.calls = calls
        self.state = ServiceState()
        self.incidents: dict[str, dict[str, Any]] = {}
        self.holder: str | None = None
        self.cap_open = True
        self.fail_final_update = False

    def triage(self, service: str, change: Change):
        result = classify(self.state, change, service)
        if result.verdict is not Verdict.SEEN:
            self.state = result.state
        if result.verdict is Verdict.NEW:
            self.incidents[result.incident.id] = {"status": "opened"}
        return result

    def lock(self, holder: str, now: float) -> bool:
        self.calls.append(("lock", holder))
        if self.holder is not None:
            return False
        self.holder = holder
        return True

    def unlock(self, holder: str) -> bool:
        self.calls.append(("unlock", holder))
        if self.holder != holder:
            return False
        self.holder = None
        return True

    def take_cap_slot(self, now: float) -> bool:
        self.calls.append(("cap",))
        return self.cap_open

    def update_incident(self, service: str, incident_id: str, fields: dict[str, Any]) -> None:
        self.calls.append(("update", fields.get("status")))
        if self.fail_final_update and fields.get("status") != "investigating":
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem")
        self.incidents[incident_id].update(fields)


class FakeRuntime:
    def __init__(self, calls: list, answer: Any = DONE, error: Exception | None = None) -> None:
        self.calls = calls
        self.answer = answer
        self.error = error

    def investigate(self, alarm: str, session_id: str) -> Any:
        self.calls.append(("investigate", alarm, session_id))
        if self.error is not None:
            raise self.error
        return self.answer

    def stop(self, session_id: str) -> str:
        self.calls.append(("stop", session_id))
        return "stopped"


def make(answer: Any = DONE, error: Exception | None = None) -> tuple[Trigger, FakeStore, list]:
    calls: list = []
    store = FakeStore(calls)
    clock = iter(range(1_000, 2_000, 10))
    trigger = Trigger(
        store, FakeRuntime(calls, answer, error), clock=lambda: float(next(clock)), session_id=lambda i: SESSION
    )
    return trigger, store, calls


def test_a_new_incident_runs_one_investigation_then_stops_stores_and_unlocks() -> None:
    trigger, store, calls = make()

    line = trigger.handle(FIRED)

    assert calls == [
        ("lock", SESSION),
        ("cap",),
        ("update", "investigating"),
        ("investigate", ERRORS, SESSION),
        ("stop", SESSION),
        ("update", "done"),
        ("unlock", SESSION),
    ]
    stored = store.incidents[INCIDENT]
    assert stored["status"] == "done"
    assert json.loads(stored["hypothesis"]) == HYPOTHESIS
    assert json.loads(gzip.decompress(stored["record_gz"])) == DONE
    assert (stored["tool_calls"], stored["model_calls"], stored["input_tokens"]) == (DONE["tool_calls"], 3, 36783)
    assert (stored["cost_usd"], stored["seconds"]) == (0.005751, 34.7)
    assert (stored["session_id"], stored["session_stop"]) == (SESSION, "stopped")
    assert stored["invoked_at"] < stored["finished_at"]
    assert line["hypothesis"] == HYPOTHESIS
    assert store.holder is None


def test_only_the_first_of_a_days_alarms_reaches_the_runtime() -> None:
    trigger, store, calls = make()

    for change in OCT_27:
        trigger.handle(event(change.alarm, change.state, iso(change.at)))

    assert [c for c in calls if c[0] == "investigate"] == [("investigate", POOL, SESSION)]
    assert list(store.incidents) == ["checkout-api-20261027T185646Z"]


def test_the_same_event_delivered_twice_runs_once() -> None:
    trigger, _, calls = make()

    trigger.handle(FIRED)
    line = trigger.handle(FIRED)

    assert line["verdict"] == "seen"
    assert sum(c[0] == "investigate" for c in calls) == 1


def test_a_held_lock_stores_busy_and_isnt_released() -> None:
    trigger, store, calls = make()
    store.holder = "another-session"

    trigger.handle(FIRED)

    assert calls == [("lock", SESSION), ("update", "busy")]
    assert store.holder == "another-session"


def test_past_the_hourly_cap_nothing_runs_and_the_lock_is_released() -> None:
    trigger, store, calls = make()
    store.cap_open = False

    trigger.handle(FIRED)

    assert calls == [("lock", SESSION), ("cap",), ("update", "capped"), ("unlock", SESSION)]
    assert store.incidents[INCIDENT]["status"] == "capped"


@pytest.mark.parametrize(
    ("error", "name"),
    [
        (ClientError({"Error": {"Code": "ThrottlingException", "Message": "arn:..."}}, "InvokeAgentRuntime"), "ThrottlingException"),
        (ReadTimeoutError(endpoint_url="https://bedrock-agentcore.us-east-1.amazonaws.com"), "ReadTimeoutError"),
        (json.JSONDecodeError("Expecting value", "<html>", 0), "JSONDecodeError"),
    ],
)
def test_a_failed_invoke_is_stored_and_the_session_still_stopped(error: Exception, name: str) -> None:
    trigger, store, calls = make(error=error)

    trigger.handle(FIRED)

    assert calls[-3:] == [("stop", SESSION), ("update", "error"), ("unlock", SESSION)]
    assert store.incidents[INCIDENT]["error"] == name
    assert "hypothesis" not in store.incidents[INCIDENT]


def test_a_refused_run_keeps_its_reason() -> None:
    reason = "sentinelops-checkout-errors is OK, not ALARM."
    trigger, store, _ = make(answer={"outcome": "refused", "reason": reason})

    trigger.handle(FIRED)

    assert (store.incidents[INCIDENT]["status"], store.incidents[INCIDENT]["reason"]) == ("refused", reason)


def test_a_store_failure_after_the_run_still_stops_and_unlocks() -> None:
    trigger, store, calls = make()
    store.fail_final_update = True

    with pytest.raises(ClientError):
        trigger.handle(FIRED)

    assert calls[-3:] == [("stop", SESSION), ("update", "done"), ("unlock", SESSION)]
    assert store.holder is None


@pytest.mark.parametrize(
    "event_",
    [
        {"source": "aws.events", "detail-type": "Scheduled Event", "detail": {}},
        event(ERRORS, "INSUFFICIENT_DATA", "2026-10-27T19:05:57.026+0000"),
        event(ERRORS, ALARM, "not a time"),
        event("sentinelops-other-errors", ALARM, "2026-10-27T19:05:57.026+0000"),
    ],
)
def test_anything_but_one_of_our_alarms_changing_is_ignored(event_: dict) -> None:
    trigger, store, calls = make()

    assert trigger.handle(event_)["event"] == "ignored"
    assert (calls, store.incidents) == ([], {})


def test_an_event_becomes_the_change_it_reports() -> None:
    change = parse_change(FIRED)

    assert (change.alarm, change.state, iso(change.at)) == (ERRORS, ALARM, "2026-10-27T19:05:57.026Z")


def test_a_record_too_big_for_an_item_is_counted_not_kept() -> None:
    # Random hex carries 4 bits a character, so gzip can't take this below the limit.
    noise = os.urandom(2 * MAX_RECORD_GZ_BYTES).hex()
    fields = answer_fields({**DONE, "messages": [noise]})

    assert "record_gz" not in fields
    assert fields["record_bytes"] > 2 * MAX_RECORD_GZ_BYTES
    assert fields["status"] == "done"


@pytest.mark.parametrize("answer", [["not", "an", "object"], {"outcome": "exploded"}])
def test_an_answer_it_doesnt_know_is_an_error(answer: Any) -> None:
    assert answer_fields(answer)["status"] == "error"


def test_session_ids_fit_agentcores_rules() -> None:
    incident = classify(ServiceState(), parse_change(FIRED), "checkout-api").incident
    assert isinstance(incident, Incident)

    session_id = new_session_id(incident)

    assert session_id.startswith(f"{INCIDENT}-")
    assert 33 <= len(session_id) <= 256
    assert re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-_]*", session_id)
    assert new_session_id(incident) != session_id


def test_the_session_and_then_the_diagnosis_reach_the_log(capsys: pytest.CaptureFixture[str]) -> None:
    trigger, _, _ = make()

    trigger.handle(FIRED)

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["event"] for line in lines] == ["alarm_change", "investigating", "investigation"]
    # Logged before the invoke, so the kill switch can find the session to stop.
    assert (lines[1]["incident"], lines[1]["session_id"]) == (INCIDENT, SESSION)
    assert lines[2]["hypothesis"]["root_cause"] == HYPOTHESIS["root_cause"]
    assert "record_gz" not in lines[2]


def runtime_with_stub() -> tuple[AgentRuntime, Stubber]:
    client = boto3.client(
        "bedrock-agentcore", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing"
    )
    return AgentRuntime(client, RUNTIME_ARN), Stubber(client)


def test_the_runtime_gets_a_live_investigation_of_the_alarm() -> None:
    runtime, stubber = runtime_with_stub()
    body = json.dumps(DONE).encode()
    stubber.add_response(
        "invoke_agent_runtime",
        {"response": StreamingBody(io.BytesIO(body), len(body)), "contentType": "application/json", "statusCode": 200},
        {
            "agentRuntimeArn": RUNTIME_ARN,
            "qualifier": "DEFAULT",
            "runtimeSessionId": SESSION,
            "contentType": "application/json",
            "accept": "application/json",
            "payload": b'{"alarm": "sentinelops-checkout-errors"}',
        },
    )

    with stubber:
        assert runtime.investigate(ERRORS, SESSION) == DONE


def test_stopping_reports_a_session_already_gone_by_its_code() -> None:
    runtime, stubber = runtime_with_stub()
    expected = {"agentRuntimeArn": RUNTIME_ARN, "qualifier": "DEFAULT", "runtimeSessionId": SESSION}
    stubber.add_response("stop_runtime_session", {"runtimeSessionId": SESSION, "statusCode": 200}, expected)
    stubber.add_client_error("stop_runtime_session", "ResourceNotFoundException", http_status_code=404, expected_params=expected)

    with stubber:
        assert runtime.stop(SESSION) == "stopped"
        assert runtime.stop(SESSION) == "ResourceNotFoundException"
