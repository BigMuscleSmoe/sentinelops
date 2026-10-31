"""main: a whole run from the CLI's entry, with the real tools on recorded AWS calls and a scripted model.

No test here reaches AWS or the Anthropic API.
"""

import asyncio
import io
import json
import logging
import os
from pathlib import Path

import pytest

import main
from fake_anthropic import ACCESS_TOKEN, FEDERATION_IDS, FakeAnthropic, Reply, answer, text, tool_use
from fake_gateway import URL, FakeGateway
from pricing import price_for
from providers import API_KEY_SECRET, FEDERATION_ENV, Settings
from sentinelops_tools.env import ToolEnv
from stubs import AGENT_FIXTURES, TOOL_FIXTURES, WEB_IDENTITY_TOKEN, FakeSession, load

HAIKU = price_for("anthropic", "claude-haiku-5-5")
# Shaped like a real key, so anything that masks keys by their look would act on it.
FAKE_KEY = "sk-ant-api03-" + "N0tARealKey7q" * 7 + "-AA"
FEDERATED = Settings.from_env(FEDERATION_IDS)
ERRORS = "sentinelops-checkout-errors"
REPLAY = ["--alarm", ERRORS, "--as-of", "2026-10-27T19:06:00Z"]
TOOL_FIXTURE_NAMES = ["metrics_bad_deploy", "logs_bad_deploy_errors", "history_bad_deploy"]
DIAGNOSIS = {
    "root_cause": "Release 1.43.0 removed the null check in apply_promo; checkouts without a promo fail.",
    "confidence": 0.9,
    "evidence": ["err% 10.1 at 19:05", "TypeError at checkout/pricing.py:15 in apply_promo"],
    "affected_component": "checkout-api",
}


def replay_session(tools: list[str] = TOOL_FIXTURE_NAMES, *, federated: bool = False) -> FakeSession:
    """The alarm, then the key or one JWT from STS, then each tool's recorded calls."""
    session = FakeSession()
    session.replay(load(AGENT_FIXTURES / "alarm_bad_deploy.json"))
    if federated:
        session.expect_web_identity_token()
    else:
        session.expect(
            "get_secret_value", {"SecretId": API_KEY_SECRET}, {"Name": API_KEY_SECRET, "SecretString": FAKE_KEY}
        )
    for name in tools:
        session.replay(load(TOOL_FIXTURES / f"{name}.json"))
    return session


@pytest.fixture(autouse=True)
def tools_run_here_unless_a_test_says(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SENTINELOPS_GATEWAY_URL", raising=False)


def run(
    argv: list[str],
    session: FakeSession,
    fake: FakeAnthropic,
    transcript: Path,
    settings: Settings | None = None,
    **kwargs: object,
) -> tuple[int, str, str]:
    args = main.parse_args([*argv, "--transcript", str(transcript)])
    out, err = io.StringIO(), io.StringIO()
    code = asyncio.run(
        main.run(
            args,
            settings=settings or Settings(),
            price=HAIKU,
            session=session,
            http_client=fake.client(),
            token_http_client=fake.token_client(),
            stdout=out,
            stderr=err,
            **kwargs,
        )
    )
    return code, out.getvalue(), err.getvalue()


def investigating() -> FakeAnthropic:
    return FakeAnthropic(
        [
            Reply([tool_use("get_metrics")]),
            Reply([tool_use("get_recent_logs")]),
            Reply([tool_use("get_deploy_history")]),
            answer(DIAGNOSIS),
        ]
    )


def test_a_replay_runs_the_real_tools_as_of_the_alarm(tmp_path: Path) -> None:
    session, fake = replay_session(), investigating()

    code, out, err = run(REPLAY, session, fake, tmp_path / "run.json")

    assert code == main.EXIT_OK
    session.assert_all_called()  # every recorded call made, in order, with its exact parameters
    first = fake.requests[0]["body"]["messages"][0]["content"][0]["text"]
    assert first == load(AGENT_FIXTURES / "alarm_bad_deploy.json")["prompt"]
    # What each tool returned is what its recorded calls produce, then the budget line.
    results = fake.tool_results(3)
    for result, name in zip(results, TOOL_FIXTURE_NAMES, strict=True):
        output, budget = result.rsplit("\n", 1)
        assert output == load(TOOL_FIXTURES / f"{name}.json")["text"]
        assert budget.startswith("[budget: ") and budget.endswith(" remaining]")

    assert json.loads(out)["root_cause"] == DIAGNOSIS["root_cause"]
    assert "tool calls: get_metrics x1, get_recent_logs x1, get_deploy_history x1 | model calls: 4" in err

    record = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert record["alarm"]["replay"] is True
    assert record["alarm"]["fired_at"] == "2026-10-27T19:05:57.026000+00:00"
    assert record["model"] == {"provider": "anthropic", "model_id": "claude-haiku-5-5", "auth": "api_key"}
    assert record["tools_via"] == "local"
    assert record["hypothesis"]["root_cause"] == DIAGNOSIS["root_cause"]
    assert record["tool_calls"] == ["get_metrics", "get_recent_logs", "get_deploy_history"]
    assert len(record["model_calls"]) == 4 and record["cost_usd"] > 0


def test_a_replay_through_the_gateway_gives_the_model_what_a_local_one_does(tmp_path: Path) -> None:
    session, fake = replay_session(), investigating()
    gateway = FakeGateway(ToolEnv.from_session(session))

    code, out, _ = run(
        [*REPLAY, "--gateway", URL], session, fake, tmp_path / "run.json", gateway_transport=gateway.transport()
    )

    assert code == main.EXIT_OK
    session.assert_all_called()  # each tool's recorded calls, made by its Lambda handler
    for result, name in zip(fake.tool_results(3), TOOL_FIXTURE_NAMES, strict=True):
        assert result.rsplit("\n", 1)[0] == load(TOOL_FIXTURES / f"{name}.json")["text"]
    assert [arguments["as_of"] for _, arguments in gateway.calls] == ["2026-10-27T19:05:57.026Z"] * 3
    assert json.loads(out)["root_cause"] == DIAGNOSIS["root_cause"]
    record = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert record["tools_via"] == "gateway"


def test_a_hypothesis_beyond_ascii_prints_on_a_cp1252_stdout(tmp_path: Path) -> None:
    # What Windows gives a redirected stdout: cp1252, strict. Runs 7 and 8 of
    # 2026-10-29 wrote an arrow and ended in UnicodeEncodeError after the run.
    arrow = chr(0x2192)
    diagnosis = {**DIAGNOSIS, "root_cause": f"1.42.0 {arrow} 1.43.0 removed the null check in apply_promo."}
    fake = FakeAnthropic([Reply([tool_use("get_metrics")]), answer(diagnosis)])
    args = main.parse_args([*REPLAY, "--transcript", str(tmp_path / "run.json")])
    raw = io.BytesIO()
    stdout = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")

    code = asyncio.run(
        main.run(
            args,
            settings=Settings(),
            price=HAIKU,
            session=replay_session(tools=["metrics_bad_deploy"]),
            http_client=fake.client(),
            stdout=stdout,
            stderr=io.StringIO(),
        )
    )
    stdout.flush()

    assert code == main.EXIT_OK
    printed = raw.getvalue().decode("ascii")
    assert json.loads(printed)["root_cause"] == diagnosis["root_cause"]


def test_the_api_key_is_sent_and_never_shown(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)  # every logger, libraries included, at their most talkative
    session, fake = replay_session(), investigating()

    code, out, err = run(REPLAY, session, fake, tmp_path / "run.json")

    assert code == main.EXIT_OK
    assert fake.count_requests, "the count requests carry the key too"
    assert all(r["headers"]["x-api-key"] == FAKE_KEY for r in fake.requests + fake.count_requests)
    shown = {
        "logs": caplog.text + "".join(repr(r.args) for r in caplog.records),
        "stdout": out,
        "stderr": err,
        "transcript": (tmp_path / "run.json").read_text(encoding="utf-8"),
        "environment": json.dumps(dict(os.environ)),
        "request bodies": json.dumps([r["body"] for r in fake.requests + fake.count_requests]),
    }
    for where, content in shown.items():
        assert FAKE_KEY not in content, f"the key is in the {where}"
        assert FAKE_KEY[14:40] not in content, f"part of the key is in the {where}"


def test_a_federated_replay_signs_in_once_and_never_reads_the_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)  # every logger, libraries included, at their most talkative
    session, fake = replay_session(federated=True), investigating()

    code, out, err = run(REPLAY, session, fake, tmp_path / "run.json", settings=FEDERATED)

    assert code == main.EXIT_OK
    session.assert_all_called()  # one JWT from STS, and no read of the key's secret
    assert len(fake.token_requests) == 1, "one exchange covers the run, its count calls included"
    assert fake.token_requests[0]["body"] == {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": WEB_IDENTITY_TOKEN,
        "federation_rule_id": FEDERATION_IDS["SENTINELOPS_ANTHROPIC_FEDERATION_RULE_ID"],
        "organization_id": FEDERATION_IDS["SENTINELOPS_ANTHROPIC_ORGANIZATION_ID"],
        "service_account_id": FEDERATION_IDS["SENTINELOPS_ANTHROPIC_SERVICE_ACCOUNT_ID"],
    }
    assert fake.count_requests, "the count requests sign in too"
    for request in fake.requests + fake.count_requests:
        assert request["headers"]["authorization"] == f"Bearer {ACCESS_TOKEN}"
        assert "oauth-2025-04-20" in request["headers"]["anthropic-beta"], "the API takes the token only with it"
        assert "x-api-key" not in request["headers"]
    record = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert record["model"]["auth"] == "federation"
    shown = {
        "logs": caplog.text + "".join(repr(r.args) for r in caplog.records),
        "stdout": out,
        "stderr": err,
        "transcript": (tmp_path / "run.json").read_text(encoding="utf-8"),
        "environment": json.dumps(dict(os.environ)),
        "request bodies": json.dumps([r["body"] for r in fake.requests + fake.count_requests]),
    }
    for where, content in shown.items():
        for name, secret in (("JWT", WEB_IDENTITY_TOKEN), ("access token", ACCESS_TOKEN)):
            assert secret not in content, f"the {name} is in the {where}"
            assert secret[20:50] not in content, f"part of the {name} is in the {where}"


def test_a_federation_that_fails_stops_the_run_before_any_model_call(tmp_path: Path) -> None:
    session = FakeSession()
    session.replay(load(AGENT_FIXTURES / "alarm_bad_deploy.json"))
    session.fail("get_web_identity_token", "OutboundWebIdentityFederationDisabledException")
    fake = FakeAnthropic([answer(DIAGNOSIS)])

    code, _, err = run(REPLAY, session, fake, tmp_path / "run.json", settings=FEDERATED)

    assert code == main.EXIT_REFUSED
    assert err == (
        "Refused: Could not sign in to the Anthropic API by federation: "
        "STS returned OutboundWebIdentityFederationDisabledException.\n"
    )
    assert fake.requests == [] and fake.token_requests == []
    session.assert_all_called()  # and nothing fell back to the key


def test_a_live_run_refuses_an_alarm_that_is_not_firing(tmp_path: Path) -> None:
    session = FakeSession()
    response = load(AGENT_FIXTURES / "alarm_bad_deploy.json")["calls"][0]["response"]
    response["MetricAlarms"][0]["StateValue"] = "OK"
    session.expect("describe_alarms", None, response)
    fake = FakeAnthropic([answer(DIAGNOSIS)])

    code, out, err = run(["--alarm", ERRORS], session, fake, tmp_path / "run.json")

    assert code == main.EXIT_REFUSED
    assert err == f"Refused: {ERRORS} is OK, not ALARM.\n"
    assert fake.requests == [] and out == ""
    assert not (tmp_path / "run.json").exists()


def test_a_key_that_cannot_be_read_stops_the_run_before_any_model_call(tmp_path: Path) -> None:
    session = FakeSession()
    session.replay(load(AGENT_FIXTURES / "alarm_bad_deploy.json"))
    session.fail("get_secret_value", "ResourceNotFoundException", "Secrets Manager can't find the secret.")
    fake = FakeAnthropic([answer(DIAGNOSIS)])

    code, _, err = run(REPLAY, session, fake, tmp_path / "run.json")

    assert code == main.EXIT_REFUSED
    assert err == f"Refused: Could not read {API_KEY_SECRET}: AWS returned ResourceNotFoundException.\n"
    assert fake.requests == []


def test_a_run_without_a_hypothesis_exits_1_and_still_writes_its_transcript(tmp_path: Path) -> None:
    session = replay_session(tools=[])
    fake = FakeAnthropic([Reply([text("No.")], stop_reason="refusal")])

    code, out, err = run(REPLAY, session, fake, tmp_path / "run.json")

    assert code == main.EXIT_FAILED
    assert out == ""
    assert "No Hypothesis: The model refused to go on with the investigation." in err
    record = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert record["hypothesis"] is None and record["cost_usd"] > 0


def test_a_model_without_a_price_is_refused_before_anything_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SENTINELOPS_MODEL_ID", "claude-sonnet-5-5")
    for name in FEDERATION_ENV.values():  # this machine's own, once local runs federate
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(main, "configure_logging", lambda: None)

    assert main.main(["--alarm", ERRORS]) == main.EXIT_REFUSED
    assert "pricing.PRICES" in capsys.readouterr().err


def test_as_of_needs_a_time_zone() -> None:
    assert main.parse_args(REPLAY).as_of.isoformat() == "2026-10-27T19:06:00+00:00"
    with pytest.raises(SystemExit):
        main.parse_args(["--alarm", ERRORS, "--as-of", "2026-10-27T19:06:00"])
