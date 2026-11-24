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
from answers import CHECKED_PATCH, DEPLOY_EVIDENCE, PATCH, PRICING_1_43_0
from code_snapshot import CodeSnapshot, CodeSnapshotError
from fake_anthropic import ACCESS_TOKEN, FEDERATION_IDS, FakeAnthropic, Reply, answer, text, tool_use
from fake_gateway import URL, FakeGateway
from pricing import price_for
from sources import unmark
from providers import FEDERATION_ENV, FEDERATION_ROLE_ENV, Settings
from sentinelops_tools.env import ToolEnv
from knowledge_base import KnowledgeBase, KnowledgeBaseError
from sentinelops_core import code as checkout_code
from stubs import AGENT_FIXTURES, RECORDED_KB, TOOL_FIXTURES, WEB_IDENTITY_TOKEN, FakeSession, load

HAIKU = price_for("anthropic", "claude-haiku-5-5")
FEDERATED = Settings.from_env(FEDERATION_IDS)
ERRORS = "sentinelops-checkout-errors"
REPLAY = ["--alarm", ERRORS, "--as-of", "2026-10-27T19:06:00Z"]
# The runbook fixtures' corpus (tools/tests/fixtures/runbooks_*.json, recorded 2026-11-23).
RECORDED_CORPUS = "197a0201c9eb"
TOOL_FIXTURE_NAMES = ["metrics_bad_deploy", "logs_bad_deploy_errors", "history_bad_deploy"]
DIAGNOSIS = {
    "root_cause": "Release 1.43.0 removed the null check in apply_promo; checkouts without a promo fail.",
    "confidence": 0.9,
    "evidence": DEPLOY_EVIDENCE,
    "affected_component": "checkout-api",
    "fault_location": "this_service",
}
# What a run here reads: a stand-in for the snapshot built from each release's commit (test_code_snapshot.py).
CODE = CodeSnapshot({"1.43.0": {"checkout/pricing.py": PRICING_1_43_0}}, "0" * 64)


def replay_session(tools: list[str] = TOOL_FIXTURE_NAMES) -> FakeSession:
    """The alarm, then one JWT from STS, then each tool's recorded calls."""
    session = FakeSession()
    session.replay(load(AGENT_FIXTURES / "alarm_bad_deploy.json"))
    session.expect_web_identity_token()
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
    # Run here, the tools search the recorded knowledge base: its check is knowledge_base's tests'.
    kwargs.setdefault("connect_knowledge_base", lambda session: KnowledgeBase(RECORDED_KB, RECORDED_CORPUS))
    kwargs.setdefault("build_code_snapshot", lambda: CODE)
    out, err = io.StringIO(), io.StringIO()
    code = asyncio.run(
        main.run(
            args,
            settings=settings or FEDERATED,
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
    # What each tool returned is what its recorded calls produce, marked as a source, then the budget line.
    results = fake.tool_results(3)
    for result, name, label in zip(results, TOOL_FIXTURE_NAMES, ["m1", "l1", "d1"], strict=True):
        output, budget = result.rsplit("\n", 1)
        assert unmark(output)[:2] == (label, load(TOOL_FIXTURES / f"{name}.json")["text"])
        assert budget.startswith("[budget: ") and budget.endswith(" remaining]")

    assert json.loads(out)["root_cause"] == DIAGNOSIS["root_cause"]
    assert "tool calls: get_metrics x1, get_recent_logs x1, get_deploy_history x1 | model calls: 4" in err

    record = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert record["alarm"]["replay"] is True
    assert record["alarm"]["fired_at"] == "2026-10-27T19:05:57.026000+00:00"
    assert record["model"] == {"provider": "anthropic", "model_id": "claude-haiku-5-5", "auth": "federation"}
    assert record["tools_via"] == "local"
    assert record["hypothesis"]["root_cause"] == DIAGNOSIS["root_cause"]
    assert record["trims"] == []  # answers cut, not resent: none here
    assert record["answers_in_text"] == 0  # answers given as text and asked for again: none here
    assert record["tool_calls"] == ["get_metrics", "get_recent_logs", "get_deploy_history"]
    assert record["tool_calls_after_ask"] is None  # it answered without being asked
    assert record["citation_dropped"] is None  # it cited no runbook
    assert len(record["model_calls"]) == 4 and record["cost_usd"] > 0


def test_a_replay_through_the_gateway_gives_the_model_what_a_local_one_does(tmp_path: Path) -> None:
    session, fake = replay_session(), investigating()
    gateway = FakeGateway(ToolEnv.from_session(session))

    def never(session: object) -> KnowledgeBase:
        raise AssertionError("through Gateway, the search's Lambda has the knowledge base")

    def no_build() -> CodeSnapshot:
        raise AssertionError("through Gateway, the read tool's Lambda ships its own snapshot")

    code, out, _ = run(
        [*REPLAY, "--gateway", URL], session, fake, tmp_path / "run.json", gateway_transport=gateway.transport(),
        connect_knowledge_base=never, build_code_snapshot=no_build,
    )

    assert code == main.EXIT_OK
    session.assert_all_called()  # each tool's recorded calls, made by its Lambda handler
    for result, name in zip(fake.tool_results(3), TOOL_FIXTURE_NAMES, strict=True):
        assert unmark(result.rsplit("\n", 1)[0])[1] == load(TOOL_FIXTURES / f"{name}.json")["text"]
    assert [arguments["as_of"] for _, arguments in gateway.calls] == ["2026-10-27T19:05:57.026Z"] * 3
    assert json.loads(out)["root_cause"] == DIAGNOSIS["root_cause"]
    record = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert record["tools_via"] == "gateway"


def test_a_run_here_on_a_knowledge_base_that_fails_its_check_is_refused_before_any_model_call(tmp_path: Path) -> None:
    def stale(session: object) -> KnowledgeBase:
        raise KnowledgeBaseError("sentinelops-runbooks doesn't hold this checkout's runbooks (corpus abc)")

    session, fake = FakeSession(), FakeAnthropic([])
    code, out, err = run(REPLAY, session, fake, tmp_path / "run.json", connect_knowledge_base=stale)

    assert code == main.EXIT_REFUSED and out == ""
    assert err == "Refused: sentinelops-runbooks doesn't hold this checkout's runbooks (corpus abc)\n"
    assert fake.requests == [] and not (tmp_path / "run.json").exists()
    assert session.made == []  # not even the alarm was read


def test_a_run_here_gives_its_tools_the_checked_knowledge_base(tmp_path: Path) -> None:
    search = load(TOOL_FIXTURES / "runbooks_pool.json")
    query = search["calls"][0]["params"]["retrievalQuery"]["text"]
    session = replay_session(tools=[])
    session.replay(search)
    fake = FakeAnthropic([Reply([tool_use("search_runbooks", {"query": query})]), answer({**DIAGNOSIS, "runbook_cited": "bad-deploy.md"})])

    code, _, _ = run(REPLAY, session, fake, tmp_path / "run.json")

    assert code == main.EXIT_OK
    session.assert_all_called()  # the Retrieve, at the knowledge base the check found
    record = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    # Shown by name only in this search: cleared, recorded, never sent back.
    assert record["hypothesis"]["runbook_cited"] is None
    assert record["citation_dropped"] == {"cited": "bad-deploy.md", "reason": "only listed"}


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
            settings=FEDERATED,
            price=HAIKU,
            session=replay_session(tools=["metrics_bad_deploy"]),
            http_client=fake.client(),
            token_http_client=fake.token_client(),
            stdout=stdout,
            stderr=io.StringIO(),
            connect_knowledge_base=lambda session: KnowledgeBase(RECORDED_KB, RECORDED_CORPUS),
            build_code_snapshot=lambda: CODE,
        )
    )
    stdout.flush()

    assert code == main.EXIT_OK
    printed = raw.getvalue().decode("ascii")
    assert json.loads(printed)["root_cause"] == diagnosis["root_cause"]


def test_a_replay_signs_in_once_and_never_shows_a_token(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)  # every logger, libraries included, at their most talkative
    session, fake = replay_session(), investigating()

    code, out, err = run(REPLAY, session, fake, tmp_path / "run.json")

    assert code == main.EXIT_OK
    session.assert_all_called()  # one JWT from STS, then the tools
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

    code, _, err = run(REPLAY, session, fake, tmp_path / "run.json")

    assert code == main.EXIT_REFUSED
    assert err == (
        "Refused: Could not sign in to the Anthropic API by federation: "
        "STS returned OutboundWebIdentityFederationDisabledException.\n"
    )
    assert fake.requests == [] and fake.token_requests == []
    session.assert_all_called()  # and nothing else was asked of AWS


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


def test_without_the_federation_ids_nothing_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in [*FEDERATION_ENV.values(), FEDERATION_ROLE_ENV]:  # this machine's own, which local runs need
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(main, "configure_logging", lambda: None)

    assert main.main(["--alarm", ERRORS]) == main.EXIT_REFUSED
    assert "Refused: The Anthropic API is signed in to by federation only: set " in capsys.readouterr().err


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
    monkeypatch.setenv("SENTINELOPS_MODEL_ID", "claude-opus-5-5")
    for name, value in FEDERATION_IDS.items():  # in place of this machine's own
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(FEDERATION_ROLE_ENV, raising=False)
    monkeypatch.setattr(main, "configure_logging", lambda: None)

    assert main.main(["--alarm", ERRORS]) == main.EXIT_REFUSED
    assert "pricing.PRICES" in capsys.readouterr().err


def test_as_of_needs_a_time_zone() -> None:
    assert main.parse_args(REPLAY).as_of.isoformat() == "2026-10-27T19:06:00+00:00"
    with pytest.raises(SystemExit):
        main.parse_args(["--alarm", ERRORS, "--as-of", "2026-10-27T19:06:00"])


def test_a_run_here_reads_the_code_built_from_each_releases_commit_and_keeps_a_patch_against_it(tmp_path: Path) -> None:
    session = replay_session(tools=[])
    fake = FakeAnthropic(
        [
            Reply([tool_use("read_repo_file", {"version": "1.43.0", "path": "checkout/pricing.py"})]),
            answer({**DIAGNOSIS, "remediation": PATCH}),
        ]
    )

    code, out, _ = run(REPLAY, session, fake, tmp_path / "run.json")

    assert code == main.EXIT_OK
    (result,) = fake.tool_results(1)
    assert unmark(result.rsplit("\n", 1)[0])[:2] == ("c1", checkout_code.render_file("1.43.0", "checkout/pricing.py", PRICING_1_43_0))
    record = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert record["hypothesis"]["remediation"] == CHECKED_PATCH and record["remediation_dropped"] is None
    assert json.loads(out)["remediation"]["diff"] == CHECKED_PATCH["diff"]


def test_a_run_here_whose_code_snapshot_cant_be_built_is_refused_before_any_model_call(tmp_path: Path) -> None:
    def missing() -> CodeSnapshot:
        raise CodeSnapshotError("checkout-api's code snapshot can't be built from this clone:\n1.43.0: commit ee9ef23a8d7e isn't in this clone")

    session, fake = FakeSession(), FakeAnthropic([])
    code, out, err = run(REPLAY, session, fake, tmp_path / "run.json", build_code_snapshot=missing)

    assert code == main.EXIT_REFUSED and out == ""
    assert err == "Refused: checkout-api's code snapshot can't be built from this clone:\n1.43.0: commit ee9ef23a8d7e isn't in this clone\n"
    assert fake.requests == [] and not (tmp_path / "run.json").exists()
    assert session.made == []  # not even the alarm was read
