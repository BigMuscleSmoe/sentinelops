"""app: the agent's HTTP app for AgentCore Runtime, driven over ASGI with no server.

The investigations run the real tools behind the stand-in Gateway, on recorded
AWS calls, with a scripted model. No test here reaches AWS or the Anthropic API.
"""

import asyncio
import json
import logging

import httpx
import pytest

import app as runtime_app
from app import GATEWAY_URL, SESSION_HEADER, create_app
from fake_anthropic import ACCESS_TOKEN, FEDERATION_IDS, FakeAnthropic, answer
from fake_gateway import URL, FakeGateway
from sentinelops_tools.env import ToolEnv
from stubs import AGENT_FIXTURES, TOOL_FIXTURES, WEB_IDENTITY_TOKEN, FakeSession, load
from test_main import DIAGNOSIS, ERRORS, FAKE_KEY, TOOL_FIXTURE_NAMES, investigating, replay_session

REPLAY = {"alarm": ERRORS, "as_of": "2026-10-27T19:06:00Z"}


def call(app: object, method: str, path: str, body: bytes | None = None, **headers: str) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=transport, base_url="http://runtime") as client:
            return await client.request(method, path, content=body, headers=headers)

    return asyncio.run(send())


def unused_session(region: str) -> FakeSession:
    raise AssertionError("no AWS session should be made")


def test_ping_answers_healthy_and_its_time_never_moves(monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(environ={}, session_factory=unused_session)
    first = call(app, "GET", "/ping")
    monkeypatch.setattr(runtime_app.time, "time", lambda: 4_000_000_000.0)

    later = call(app, "GET", "/ping")

    assert first.status_code == later.status_code == 200
    assert first.json()["status"] == "Healthy"
    assert later.json() == first.json()


def test_a_replay_runs_through_the_gateway_and_answers_with_its_transcript(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session, fake = replay_session(), investigating()
    gateway = FakeGateway(ToolEnv.from_session(session))
    regions = []
    app = create_app(
        environ={GATEWAY_URL: URL},
        session_factory=lambda region: regions.append(region) or session,
        http_client=fake.client(),
        gateway_transport=gateway.transport(),
    )

    with caplog.at_level(logging.INFO, logger="sentinelops"):
        response = call(app, "POST", "/invocations", json.dumps(REPLAY).encode(), **{SESSION_HEADER: "s-1"})

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "done"
    assert body["hypothesis"]["root_cause"] == DIAGNOSIS["root_cause"]
    assert body["tools_via"] == "gateway" and body["alarm"]["replay"] is True
    assert body["alarm"]["fired_at"] == "2026-10-27T19:05:57.026000+00:00"
    assert len(body["model_calls"]) == 4 and body["cost_usd"] > 0
    assert regions == ["us-east-1"]
    session.assert_all_called()  # the alarm's history, the key, and each tool's recorded calls
    for result, name in zip(fake.tool_results(3), TOOL_FIXTURE_NAMES, strict=True):
        assert result.rsplit("\n", 1)[0] == load(TOOL_FIXTURES / f"{name}.json")["text"]
    assert [arguments["as_of"] for _, arguments in gateway.calls] == ["2026-10-27T19:05:57.026Z"] * 3
    assert FAKE_KEY not in response.text
    logged = [json.loads(r.getMessage()) for r in caplog.records if r.name == "sentinelops.app"]
    assert logged == [
        {
            "event": "invocation",
            "session": "s-1",
            "outcome": "done",
            "alarm": ERRORS,
            "replay": True,
            "summary": logged[0]["summary"],
        }
    ]
    assert logged[0]["summary"].startswith("tool calls: get_metrics x1, get_recent_logs x1, get_deploy_history x1")


def test_the_runtime_signs_in_by_federation_once_terraform_sets_the_ids() -> None:
    session, fake = replay_session(federated=True), investigating()
    app = create_app(
        environ={GATEWAY_URL: URL, **FEDERATION_IDS},
        session_factory=lambda region: session,
        http_client=fake.client(),
        token_http_client=fake.token_client(),
        gateway_transport=FakeGateway(ToolEnv.from_session(session)).transport(),
    )

    response = call(app, "POST", "/invocations", json.dumps(REPLAY).encode(), **{SESSION_HEADER: "s-2"})

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "done" and body["model"]["auth"] == "federation"
    session.assert_all_called()  # the alarm, one JWT from STS, and the tools: no key
    assert len(fake.token_requests) == 1
    assert all(r["headers"]["authorization"] == f"Bearer {ACCESS_TOKEN}" for r in fake.requests)
    assert ACCESS_TOKEN not in response.text and WEB_IDENTITY_TOKEN not in response.text


def test_without_the_gateway_nothing_runs() -> None:
    fake = FakeAnthropic([answer(DIAGNOSIS)])
    app = create_app(environ={}, session_factory=unused_session, http_client=fake.client())

    response = call(app, "POST", "/invocations", json.dumps(REPLAY).encode())

    assert response.status_code == 200
    assert response.json() == {
        "outcome": "refused",
        "reason": f"{GATEWAY_URL} is not set, and here the tools run only through it.",
    }
    assert fake.requests == []


def test_an_alarm_that_is_not_firing_is_refused() -> None:
    session = FakeSession()
    alarm = load(AGENT_FIXTURES / "alarm_bad_deploy.json")["calls"][0]["response"]
    alarm["MetricAlarms"][0]["StateValue"] = "OK"
    session.expect("describe_alarms", None, alarm)
    fake = FakeAnthropic([answer(DIAGNOSIS)])
    app = create_app(environ={GATEWAY_URL: URL}, session_factory=lambda region: session, http_client=fake.client())

    response = call(app, "POST", "/invocations", json.dumps({"alarm": ERRORS}).encode())

    assert response.json() == {"outcome": "refused", "reason": f"{ERRORS} is OK, not ALARM."}
    assert fake.requests == []


def test_a_model_without_a_price_is_refused_before_anything_runs() -> None:
    app = create_app(
        environ={GATEWAY_URL: URL, "SENTINELOPS_MODEL_ID": "claude-sonnet-5-5"}, session_factory=unused_session
    )

    response = call(app, "POST", "/invocations", json.dumps(REPLAY).encode())

    assert response.json()["outcome"] == "refused"
    assert "pricing.PRICES" in response.json()["reason"]


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"not json", "The body must be a JSON object"),
        (b"\xff\xfe\xfd", "The body must be a JSON object"),
        (json.dumps([ERRORS]).encode(), "The body must be a JSON object"),
        (json.dumps({"alarm": ""}).encode(), "alarm must be the alarm's name."),
        (json.dumps({"as_of": "2026-10-27T19:06:00Z"}).encode(), "alarm must be the alarm's name."),
        (json.dumps({**REPLAY, "asof": "2026-10-27T19:06:00Z"}).encode(), "Unknown fields: asof."),
        (json.dumps({**REPLAY, "as_of": 1793127960}).encode(), "as_of must be an ISO 8601 time"),
        (json.dumps({**REPLAY, "as_of": "2026-10-27T19:06:00"}).encode(), "has no time zone"),
        (json.dumps({**REPLAY, "as_of": "yesterday"}).encode(), "Invalid isoformat string"),
    ],
)
def test_a_body_it_cannot_use_is_answered_400_before_anything_runs(body: bytes, reason: str) -> None:
    app = create_app(environ={GATEWAY_URL: URL}, session_factory=unused_session)

    response = call(app, "POST", "/invocations", body)

    assert response.status_code == 400
    assert response.json()["outcome"] == "invalid"
    assert reason in response.json()["reason"]


def test_only_the_two_routes_answer() -> None:
    app = create_app(environ={}, session_factory=unused_session)

    assert call(app, "GET", "/invocations").status_code == 405
    assert call(app, "POST", "/ping").status_code == 405
    assert call(app, "GET", "/").status_code == 404
