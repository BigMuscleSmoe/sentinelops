"""The eval harness: scenarios, rubrics, the model witness, the sweep, and the scorer.

Sweeps here run the real agent loop and tools on the real recorded windows,
with a scripted model and stubbed STS: nothing reaches AWS or the Anthropic API.
"""

import asyncio
import copy
import dataclasses
import gzip
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from alarm import AlarmEvent
from answers import item
from evals import rubrics, score, sweep
from evals.__main__ import main as evals_main
from evals.scenarios import BY_ID, SCENARIOS, Fault, load_window
from evals.witness import ModelWitness, same_model
from checked_runs import RUNS
from checker.__main__ import check_run
from fake_anthropic import FEDERATION_IDS, FakeAnthropic, Reply, answer, tool_use
from pricing import price_for
from providers import Settings
from stubs import FakeSession

# The alarms of step 1's 12 saved runs, which the first grader's rules were written on (kept at 57455ab).
SEEN = {"deploy-1027-1905", "deploy-1029-0156", "deploy-1112-2000"}
FEDERATED = Settings.from_env(FEDERATION_IDS)
HAIKU = price_for("anthropic", "claude-haiku-5-5")

DEPLOY_ANSWER = {
    "root_cause": "Deploy 1.43.0 (Simplify promo pricing) at 02:57:16 made apply_promo subscript None for carts without a promo, so those checkouts raise TypeError and return 500.",
    "confidence": 0.9,
    "evidence": [
        item("value", source="d1", column="version", at="2026-11-13T02:57:16.990Z", value="1.43.0", note="tomas, Simplify promo pricing"),
        item("all", source="l1", column="error_type", value="TypeError", note="NoneType not subscriptable in checkout/pricing.py apply_promo"),
        item("value", source="m1", column="err%", at="02:57:00", value="12.5", note="errors start in the deploy's minute"),
    ],
    "affected_component": "checkout-api 1.43.0, checkout/pricing.py apply_promo",
    "fault_location": "this_service",
}
POOL_ANSWER = {
    "root_cause": "The 18:56:06 config change cut POOL_MAX_SIZE from 20 to 2, so requests wait for one of two connections and pool wait p99 climbs past the threshold. The 1.43.0 deploy at 03:01 was rolled back hours earlier and is not the cause.",
    "confidence": 0.9,
    "evidence": [
        item("value", source="d1", column="POOL_MAX_SIZE", at="2026-10-27T18:56:06.312Z", value="20->2", note="maya's config change"),
        item("value", source="m1", column="pool_wait_p99_ms", at="18:56:00", value="1410", note="pool wait past the threshold"),
    ],
    "affected_component": "checkout-api database connection pool",
    "fault_location": "this_service",
}


# --- scenarios --------------------------------------------------------------------


def test_every_recorded_alarm_is_one_scenario() -> None:
    replays = set()
    for name in {s.window for s in SCENARIOS}:
        window = load_window(name)
        replays |= {(name, r.alarm, r.fired_at) for r in window.replays}
    assert {(s.window, s.alarm, s.fired_at) for s in SCENARIOS} == replays
    assert len(SCENARIOS) == 9 and len(BY_ID) == 9


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_each_scenario_replays_offline_and_its_culprit_is_in_deploy_history(scenario) -> None:
    window = load_window(scenario.window)
    alarm = AlarmEvent.as_of(window.client("cloudwatch"), scenario.alarm, scenario.fired_at)
    assert alarm.fired_at == scenario.fired_at and alarm.name == scenario.alarm
    day = scenario.fired_at.strftime("%Y-%m-%d")
    culprits = [
        i for i in window.meta["deploys"]["items"]
        if i["ts"]["S"].startswith(f"{day}T{scenario.culprit_at}") and i["ts"]["S"] <= scenario.fired_at.isoformat()
    ]
    (record,) = culprits
    change = record["changes"]["L"][0]["M"] if "L" in record["changes"] else record["changes"][0]["M"]
    if scenario.fault == Fault.BAD_DEPLOY:
        assert (change["key"]["S"], change["new"]["S"]) == ("version", "1.43.0")
    else:
        assert (change["key"]["S"], change["old"]["S"], change["new"]["S"]) == ("POOL_MAX_SIZE", "20", "2")


def test_seen_marks_exactly_the_alarms_the_first_grader_was_written_on() -> None:
    assert {s.id for s in SCENARIOS if s.seen} == SEEN


# --- rubrics ----------------------------------------------------------------------

DEPLOY_RUN = "20261114T190302Z/deploy-1113-0257-r1"
POOL_RUN = "20261114T190302Z/pool-1027-1856-r1"
# The trial runs whose evidence leaves a rubric item unmet, each read by hand:
# the change is cited only through the old 'changed key' column (unresolved) or
# kind config, which doesn't show the change; the latency alarm's only p99 item
# is contradicted (757 at 18:57, below its low); the other states no p99 at all.
THIN = {
    "20261114T055051Z/pool-1027-1900-r1": {"evidence_change", "evidence_symptom"},
    "20261114T055051Z/pool-1027-1901-r1": {"evidence_change"},
    "20261114T190302Z/pool-1027-1900-r1": {"evidence_symptom"},
}


def _rubric(name: str, **changes: Any) -> rubrics.RubricResult:
    """A fixture run's answer, changed, scored with the checker's checks of its evidence."""
    transcript = copy.deepcopy(RUNS[name])
    transcript["hypothesis"].update(changes)
    check = check_run(name, transcript)
    return rubrics.score(BY_ID[transcript["eval"]["scenario"]], transcript["hypothesis"], check.checks)


def _failed(result: rubrics.RubricResult) -> set[str]:
    return {k for k, v in result.items.items() if not v and k != "pr_gate_value"}


@pytest.mark.parametrize("name", sorted(RUNS))
def test_each_trial_diagnosis_passes_its_rubric(name: str) -> None:
    result = _rubric(name)
    assert result.diagnosis and not result.confidently_wrong
    assert _failed(result) == THIN.get(name, set())


def test_a_right_pool_diagnosis_passes_even_naming_the_old_deploy_to_rule_it_out() -> None:
    cause = RUNS[POOL_RUN]["hypothesis"]["root_cause"]
    result = _rubric(POOL_RUN, root_cause=f"{cause} The 1.43.0 deploy at 03:01 was rolled back hours earlier and is not the cause.")
    assert result.passed(), result.items


@pytest.mark.parametrize(
    "sentence",
    [
        # Sweep 20261113T201129Z's two right answers the old words counted as blaming payments-api.
        "Database query time and payments-api were unchanged, and no requests failed.",
        "The database and payments-api are healthy, and no code changed.",
        # Sweep 20261114T221926Z's right answer (pool-1027-1900-r1) the old words counted the same way.
        "Payments-api and DB query time stayed flat throughout.",
        # The rest of the batch added with it.
        "Payments-api latency held steady through the incident.",
        "Payments-api was unaffected, with no timeouts.",
        "Payments-api latency was normal, and the database answered in about 60 ms.",
        "There was no change to payments-api in the window.",
        "No changes reached payments-api in the window.",
    ],
)
def test_ruling_out_words_rule_a_suspect_out(sentence: str) -> None:
    assert rubrics.blamed(sentence, rubrics.RULES[Fault.DB_POOL_EXHAUSTION].wrong_suspects) == []
    cause = RUNS[POOL_RUN]["hypothesis"]["root_cause"]
    assert _rubric(POOL_RUN, root_cause=f"{cause} {sentence}").items["blames_no_other"]


@pytest.mark.parametrize(
    "sentence",
    [
        # Whole words only: these blame the suspect.
        "Payments-api latency was abnormally high, so checkouts timed out.",
        "Payments-api responses turned abnormal at 18:56, failing checkouts.",
        "Payments-api latency was steadily climbing, so checkouts timed out.",
    ],
)
def test_ruling_out_words_match_whole_words_only(sentence: str) -> None:
    assert rubrics.blamed(sentence, rubrics.RULES[Fault.DB_POOL_EXHAUSTION].wrong_suspects) == ["Payments-api"]


# What a downstream_timeout answer names, ruled out or not; that fault's own rules come with step 7.
DOWNSTREAM_SHAPED = re.compile(r"pool wait|database time|payments latency", re.I)


@pytest.mark.parametrize(
    "sentence, blamed",
    [
        # "normal" and "steady" as a state rule out what the sentence names...
        ("Pool wait stayed normal through the incident.", []),
        ("Database time held steady at about 60 ms.", []),
        ("Pool wait remained normal while checkouts slowed.", []),
        ("Database time was steady at 49.8 to 58 ms.", []),
        ("Database time stayed in its normal range.", []),
        ("Pool wait and database time were normal.", []),
        ("Pool wait and database time are steady.", []),
        # ...and as an adjective rule nothing out.
        ("Payments latency rose above normal at 22:28:30.", ["Payments latency"]),
        ("A steady climb in pool wait began at 18:56.", ["pool wait"]),
        ("Payments latency is far above its normal level.", ["Payments latency"]),
    ],
)
def test_normal_and_steady_rule_out_only_as_a_state(sentence: str, blamed: list[str]) -> None:
    assert rubrics.blamed(sentence, DOWNSTREAM_SHAPED) == blamed


@pytest.mark.parametrize(
    "sentence, blamed",
    [
        # The false pass a bare "normal" allowed: the pool blamed beside "above normal".
        ("Payments-api latency rose above normal, so the connection pool backed up.", ["connection pool"]),
        ("A steady climb in pool wait followed the POOL_MAX_SIZE change.", ["POOL_MAX_SIZE"]),
    ],
)
def test_an_adjective_doesnt_excuse_a_wrong_suspect(sentence: str, blamed: list[str]) -> None:
    assert rubrics.blamed(sentence, rubrics.RULES[Fault.BAD_DEPLOY].wrong_suspects) == blamed


def _culprit_cited_by(evidence: list[dict[str, Any]], minute: str) -> list[int]:
    return [i for i, e in enumerate(evidence) if (e.get("source") or "").startswith("d") and minute in str(e.get("at"))]


@pytest.mark.parametrize(
    "name, changes, failed",
    [
        # The bad deploy blamed on the pool change that came before it.
        (DEPLOY_RUN, {"root_cause": "The config change set POOL_MAX_SIZE to 2, so the pool is exhausted and checkouts fail."}, {"names_culprit", "names_mechanism", "blames_no_other"}),
        # The pool fault blamed on the old 1.43.0 deploy.
        (POOL_RUN, {"root_cause": "Release 1.43.0 broke promo pricing; checkouts without a promo raise TypeError."}, {"names_culprit", "names_mechanism", "blames_no_other"}),
        # Both changes blamed together.
        (DEPLOY_RUN, {"root_cause": "Deploy 1.43.0 broke apply_promo on None promos, and the CART_TTL_HOURS change made it worse."}, {"blames_no_other"}),
        # The symptom, not the mechanism.
        (DEPLOY_RUN, {"root_cause": "Checkout requests started failing with 500s at 02:57."}, {"names_culprit", "names_mechanism"}),
        # The upstream blamed, by name or by location; by location, no PR either.
        (DEPLOY_RUN, {"affected_component": "payments-api"}, {"location"}),
        (DEPLOY_RUN, {"fault_location": "upstream"}, {"location", "pr_gate"}),
        # Not confident enough to open a PR, and a citation with no search.
        (DEPLOY_RUN, {"confidence": 0.5}, {"pr_gate"}),
        (DEPLOY_RUN, {"runbook_cited": "bad-deploy.md"}, {"runbook"}),
        # Thin evidence: counts that hold show no change, failure, or metric.
        (DEPLOY_RUN, {"evidence": [item("count", source="d1", count=10, note="ten records"), item("count", source="l1", count=30, note="thirty lines")], "inferences": []}, {"evidence_change", "evidence_failure", "evidence_symptom"}),
    ],
)
def test_a_wrong_or_thin_answer_fails_the_items_it_should(name: str, changes: dict[str, Any], failed: set[str]) -> None:
    assert _failed(_rubric(name, **changes)) == failed


def test_evidence_that_doesnt_hold_carries_nothing() -> None:
    evidence = copy.deepcopy(RUNS[DEPLOY_RUN]["hypothesis"]["evidence"])
    cited = _culprit_cited_by(evidence, "02:57:16")
    assert cited and _failed(_rubric(DEPLOY_RUN, evidence=evidence)) == set()
    for i in cited:  # each item on the culprit's record now states the version it replaced
        evidence[i] = {**evidence[i], "column": "version", "value": "1.41.0"}
    result = _rubric(DEPLOY_RUN, evidence=evidence)
    assert _failed(result) == {"evidence_change"}


def test_the_right_change_at_the_wrong_time_isnt_the_culprit() -> None:
    """The 07:46 1.43.0 deploy for the 08:03 alarm: the same version, but not the record that caused this one."""
    name = "20261114T190302Z/deploy-1112-0803-r1"
    evidence = copy.deepcopy(RUNS[name]["hypothesis"]["evidence"])
    evidence = [e for i, e in enumerate(evidence) if i not in _culprit_cited_by(evidence, "08:03:")]
    evidence.append(item("value", source="d1", column="version", at="2026-11-12T07:46:29.556Z", value="1.43.0", note="Simplify promo pricing"))
    assert _failed(_rubric(name, evidence=evidence, inferences=[])) == {"evidence_change"}


def test_a_confident_wrong_diagnosis_is_confidently_wrong() -> None:
    wrong = {"root_cause": "Release 1.43.0 broke promo pricing; checkouts without a promo raise TypeError."}
    result = _rubric(POOL_RUN, **wrong)
    assert not result.diagnosis and result.confidently_wrong
    assert not _rubric(POOL_RUN, **wrong, halted_by="step_limit").confidently_wrong


# --- which model answered ---------------------------------------------------------


def test_same_model_accepts_the_alias_or_a_dated_snapshot_only() -> None:
    assert same_model("claude-haiku-5-5", "claude-haiku-5-5")
    assert same_model("claude-haiku-5-5", "claude-haiku-5-5-20261001")
    assert not same_model("claude-haiku-5-5", "claude-sonnet-5-5")
    assert not same_model("claude-haiku-5-5", "claude-haiku-5-5-preview")


def test_the_witness_notes_the_answering_model_and_passes_the_stream_unchanged() -> None:
    fake = FakeAnthropic([Reply([tool_use("get_metrics")])])

    async def go() -> tuple[bytes, bytes, list[str]]:
        witness = ModelWitness(httpx.MockTransport(fake._handle))
        plain = httpx.AsyncClient(transport=httpx.MockTransport(FakeAnthropic([Reply([tool_use("get_metrics")])])._handle))
        async with httpx.AsyncClient(transport=witness) as client:
            seen = (await client.post("https://api.anthropic.com/v1/messages", json={})).content
        async with plain:
            expected = (await plain.post("https://api.anthropic.com/v1/messages", json={})).content
        return seen, expected, witness.models

    seen, expected, models = asyncio.run(go())
    assert seen == expected
    assert models == ["claude-haiku-5-5"]
    assert fake.requests[0]["headers"]["accept-encoding"] == "identity"


def _encoded(encoding: str, body: bytes) -> httpx.MockTransport:
    """An API that sends its stream compressed whatever it's asked for."""
    return httpx.MockTransport(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream", "content-encoding": encoding}, content=body
        )
    )


def _through(witness: ModelWitness) -> bytes:
    async def go() -> bytes:
        async with httpx.AsyncClient(transport=witness) as client:
            return (await client.post("https://api.anthropic.com/v1/messages", json={})).content

    return asyncio.run(go())


def test_the_witness_reads_a_gzipped_stream_and_the_client_still_gets_it_whole() -> None:
    stream = b'event: message_start\ndata: {"type": "message_start", "message": {"model": "claude-haiku-5-5"}}\n\n'
    witness = ModelWitness(_encoded("gzip", gzip.compress(stream)))
    assert _through(witness) == stream  # httpx decodes it as it always does
    assert (witness.models, witness.responses) == (["claude-haiku-5-5"], ["200 gzip"])


def test_an_encoding_the_witness_cant_read_leaves_the_model_unseen_and_says_so() -> None:
    witness = ModelWitness(_encoded("br", b"\x8b\x02\x80"))
    _through(witness)
    assert (witness.models, witness.responses) == ([], ["200 br"])


# --- the sweep, with a scripted model on the real windows -------------------------


def _script(n_runs: int, hypothesis: dict[str, Any] = DEPLOY_ANSWER) -> FakeAnthropic:
    replies = []
    for _ in range(n_runs):
        replies += [
            Reply([tool_use("get_metrics", {"minutes": 10, "period_seconds": 60})]),
            Reply([tool_use("get_recent_logs")]),
            Reply([tool_use("get_deploy_history")]),
            answer(hypothesis),
        ]
    return FakeAnthropic(replies)


def _sts(n_runs: int) -> FakeSession:
    session = FakeSession()
    for _ in range(n_runs):
        session.expect_web_identity_token()
    return session


def _sweep(scenarios, runs, fake, sts, out_dir, settings=FEDERATED, price=HAIKU, **kwargs: Any):
    return asyncio.run(
        sweep.run_sweep(
            scenarios,
            runs,
            settings=settings,
            price=price,
            sign_in=sts,
            out_dir=out_dir,
            api_transport=lambda: httpx.MockTransport(fake._handle),
            token_http_client=fake.token_client(),
            pause_s=0,
            say=lambda _: None,
            **kwargs,
        )
    )


def test_a_sweep_replays_each_alarm_through_the_real_loop_and_keeps_every_transcript(tmp_path: Path) -> None:
    scenarios = [BY_ID["deploy-1113-0257"], BY_ID["pool-1027-1856"]]
    fake, sts = _script(2), _sts(2)

    records = _sweep(scenarios, 1, fake, sts, tmp_path)

    sts.assert_all_called()  # one sign-in per run, and no other AWS call
    assert [(r.run, r.status, r.tool_calls, r.model_calls) for r in records] == [
        ("deploy-1113-0257-r1", "answered", 3, 4),
        ("pool-1027-1856-r1", "answered", 3, 4),
    ]
    assert all(r.models_answered == ["claude-haiku-5-5"] * 4 and r.cost_usd > 0 for r in records)
    # The first run's prompt is its alarm; its tools read the 02:57 window as of the alarm.
    first = fake.requests[0]["body"]["messages"][0]["content"][0]["text"]
    assert "Fired at: 2026-11-13T02:57:45.007Z" in first
    history = fake.tool_results(3)[2]
    assert "02:57:16" in history and "1.43.0" in history
    pool_history = fake.tool_results(7)[2]
    assert "POOL_MAX_SIZE" in pool_history and "19:05:22" not in pool_history  # nothing after the alarm

    saved = json.loads((tmp_path / "sweep.json").read_text(encoding="utf-8"))
    assert [r["run"] for r in saved["records"]] == ["deploy-1113-0257-r1", "pool-1027-1856-r1"]
    assert (saved["arm"], saved["per_run_usd"], saved["cap_usd"]) == ("haiku", 0.10, 1.00)
    transcript = json.loads((tmp_path / "deploy-1113-0257-r1.json").read_text(encoding="utf-8"))
    assert transcript["eval"]["scenario"] == "deploy-1113-0257" and transcript["eval"]["seen"] is False
    assert transcript["hypothesis"]["root_cause"] == DEPLOY_ANSWER["root_cause"]
    assert transcript["alarm"]["replay"] is True and transcript["tools_via"] == "local"


def test_runs_go_round_by_round_so_a_stopped_sweep_covers_every_scenario() -> None:
    a, b = BY_ID["deploy-1113-0257"], BY_ID["pool-1027-1856"]
    assert [(s.id, n) for s, n in sweep.plan([a, b], 2)] == [(a.id, 1), (b.id, 1), (a.id, 2), (b.id, 2)]


def test_the_sweep_cap_stops_before_a_run_it_cant_cover(tmp_path: Path) -> None:
    fake, sts = _script(2), _sts(1)
    records = _sweep([BY_ID["deploy-1113-0257"]], 2, fake, sts, tmp_path, cap_usd=0.10 + 1e-7, per_run_usd=0.10)
    assert [r.run for r in records] == ["deploy-1113-0257-r1"]
    assert len(fake.requests) == 4


def test_a_failed_sign_in_is_an_error_and_a_rerun_retries_only_it(tmp_path: Path) -> None:
    scenario = BY_ID["deploy-1113-0257"]
    fake = FakeAnthropic([], tokens=[httpx.Response(401, json={"error": "nope"})])
    first = _sweep([scenario], 1, fake, _sts(1), tmp_path)
    assert [(r.status, r.model_calls) for r in first] == [("error", 0)]
    assert first[0].detail is not None and first[0].detail.startswith("refused: Could not sign in")

    fake, sts = _script(1), _sts(1)
    second = _sweep([scenario], 1, fake, sts, tmp_path)
    assert [(r.attempt, r.status) for r in second] == [(1, "error"), (2, "answered")]
    assert (tmp_path / "deploy-1113-0257-r1.attempt2.json").exists()
    # Run again: everything has an outcome, so nothing runs.
    assert _sweep([scenario], 1, FakeAnthropic([]), FakeSession(), tmp_path) == second


def test_an_answer_from_another_model_is_an_error_not_a_score(tmp_path: Path) -> None:
    fake = _script(1)

    def another_model(request: httpx.Request) -> httpx.Response:
        response = fake._handle(request)
        swapped = response.content.replace(b'"model": "claude-haiku-5-5"', b'"model": "claude-haiku-4-5"')
        return httpx.Response(response.status_code, headers=response.headers, content=swapped)

    records = asyncio.run(
        sweep.run_sweep(
            [BY_ID["deploy-1113-0257"]], 1, settings=FEDERATED, price=HAIKU, sign_in=_sts(1), out_dir=tmp_path,
            api_transport=lambda: httpx.MockTransport(another_model), token_http_client=fake.token_client(),
            pause_s=0, say=lambda _: None,
        )
    )
    assert [(r.status, r.detail) for r in records] == [
        ("error", "answered by claude-haiku-4-5, asked for claude-haiku-5-5")
    ]


def test_a_read_the_recording_cant_answer_is_an_error(tmp_path: Path) -> None:
    late = dataclasses.replace(BY_ID["deploy-1113-0257"], fired_at=datetime(2026, 11, 13, 4, 0, tzinfo=UTC))
    records = _sweep([late], 1, FakeAnthropic([]), FakeSession(), tmp_path)
    assert records[0].status == "error" and records[0].detail.startswith("OutsideWindow: alarm history")

    session = sweep.WindowSession(load_window("2026-11-13-0257"), FakeSession())
    with pytest.raises(Exception, match="recorded from"):
        session.client("logs").filter_log_events(
            logGroupName="/aws/lambda/sentinelops-checkout-api", startTime=0, endTime=1, filterPattern="{ $.service = \"checkout-api\" }"
        )
    assert len(session.gaps) == 1 and session.gaps[0].startswith("filter_log_events: ")


def test_a_run_that_ends_without_a_hypothesis_is_an_outcome_and_an_api_failure_an_error() -> None:
    assert sweep.classify("The model refused to go on with the investigation.") == "model_refused"
    assert sweep.classify("RuntimeError: The run ended without a Hypothesis.") == "no_hypothesis"
    assert sweep.classify("APIStatusError: Error code: 529 - overloaded") == "error"
    assert sweep.classify("ModelThrottledException: rate limited") == "error"


def test_without_go_the_sweep_command_only_plans(capsys: pytest.CaptureFixture[str]) -> None:
    assert evals_main(["sweep", "--runs", "3"]) == 2
    out = capsys.readouterr().out
    assert "27 runs (9 scenarios x 3)" in out and "at most $1.00" in out and "Nothing ran" in out


# --- the scorer -------------------------------------------------------------------


def test_grading_refuses_an_audit_that_isnt_committed(tmp_path: Path) -> None:
    loose = score.REPO / "agent" / "scratch" / "uncommitted-audit.json"
    loose.parent.mkdir(parents=True, exist_ok=True)
    loose.write_text("{}", encoding="utf-8")
    try:
        with pytest.raises(score.Refusal, match="isn't committed"):
            score.check_committed(loose)
    finally:
        loose.unlink()

