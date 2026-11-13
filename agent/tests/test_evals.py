"""The eval harness: scenarios, rubrics, the model witness, the sweep, and the scorer.

Sweeps here run the real agent loop and tools on the real recorded windows,
with a scripted model and stubbed STS: nothing reaches AWS or the Anthropic API.
"""

import asyncio
import copy
import dataclasses
import gzip
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from alarm import AlarmEvent
from evals import rubrics, score, sweep
from evals.__main__ import main as evals_main
from evals.scenarios import BY_ID, SCENARIOS, Fault, load_window
from evals.witness import ModelWitness, same_model
from fake_anthropic import FEDERATION_IDS, FakeAnthropic, Reply, answer, tool_use
from grader.checks import Verdict
from pricing import price_for
from providers import Settings
from stubs import FakeSession

FIXTURES = Path(__file__).parent / "fixtures" / "grader"
SAVED = json.loads(gzip.decompress((FIXTURES / "runs.json.gz").read_bytes()).decode("utf-8"))
SLIPS = json.loads((FIXTURES / "slips.json").read_text(encoding="utf-8"))["slips"]
SAVED_SCENARIO = {"2026-10-27T19:05:57": "deploy-1027-1905", "2026-10-29T01:56:07": "deploy-1029-0156", "2026-11-12T20:00:45": "deploy-1112-2000"}
FEDERATED = Settings.from_env(FEDERATION_IDS)
HAIKU = price_for("anthropic", "claude-haiku-5-5")

DEPLOY_ANSWER = {
    "root_cause": "Deploy 1.43.0 (Simplify promo pricing) at 02:57:16 made apply_promo subscript None for carts without a promo, so those checkouts raise TypeError and return 500.",
    "confidence": 0.9,
    "evidence": [
        "Deploy history: 1.43.0 deployed at 02:57:16.990Z by tomas, 'Simplify promo pricing'.",
        "Logs: every failure is TypeError 'NoneType' object is not subscriptable in checkout/pricing.py apply_promo.",
        "Metrics: err% 0.0 at 02:56, then 12.5% at 02:57.",
    ],
    "affected_component": "checkout-api 1.43.0, checkout/pricing.py apply_promo",
}
POOL_ANSWER = {
    "root_cause": "The 18:56:06 config change cut POOL_MAX_SIZE from 20 to 2, so requests wait for one of two connections and pool wait p99 climbs past the threshold. The 1.43.0 deploy at 03:01 was rolled back hours earlier and is not the cause.",
    "confidence": 0.9,
    "evidence": [
        "Deploy history: config POOL_MAX_SIZE 20 -> 2 at 18:56:06 (maya).",
        "Metrics: pool_wait_p99 rose from 0.4 ms to 1,410 ms at 18:56, with pool_active_max at 2.",
    ],
    "affected_component": "checkout-api database connection pool",
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


def test_seen_marks_exactly_the_alarms_the_grader_was_written_on() -> None:
    assert {s.id for s in SCENARIOS if s.seen} == set(SAVED_SCENARIO.values())
    assert {SAVED_SCENARIO[r["alarm"]["fired_at"][:19]] for r in SAVED.values()} == set(SAVED_SCENARIO.values())


# --- rubrics ----------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SAVED))
def test_each_saved_diagnosis_passes_its_rubric(name: str) -> None:
    run = SAVED[name]
    result = rubrics.score(BY_ID[SAVED_SCENARIO[run["alarm"]["fired_at"][:19]]], run["hypothesis"])
    assert result.passed(), {k: v for k, v in result.items.items() if not v}
    assert result.diagnosis and not result.confidently_wrong


def _with(answer_: dict[str, Any], **changes: Any) -> dict[str, Any]:
    out = copy.deepcopy(answer_)
    out.update(changes)
    return out


def test_a_right_pool_diagnosis_passes_even_naming_the_old_deploy_to_rule_it_out() -> None:
    result = rubrics.score(BY_ID["pool-1027-1856"], POOL_ANSWER)
    assert result.passed(), result.items


@pytest.mark.parametrize(
    "scenario, hypothesis, failed",
    [
        # The bad deploy blamed on the pool change that came before it.
        (
            "deploy-1113-0257",
            _with(DEPLOY_ANSWER, root_cause="The config change set POOL_MAX_SIZE to 2, so the pool is exhausted and checkouts fail."),
            {"names_culprit", "names_mechanism", "blames_no_other"},
        ),
        # The pool fault blamed on the old 1.43.0 deploy.
        (
            "pool-1027-1856",
            _with(POOL_ANSWER, root_cause="Release 1.43.0 broke promo pricing; checkouts without a promo raise TypeError."),
            {"names_culprit", "names_mechanism", "blames_no_other"},
        ),
        # Both changes blamed together.
        (
            "deploy-1113-0257",
            _with(DEPLOY_ANSWER, root_cause="Deploy 1.43.0 broke apply_promo on None promos, and the CART_TTL_HOURS change made it worse."),
            {"blames_no_other"},
        ),
        # The symptom, not the mechanism.
        ("deploy-1113-0257", _with(DEPLOY_ANSWER, root_cause="Checkout requests started failing with 500s at 02:57."), {"names_culprit", "names_mechanism"}),
        # The upstream blamed.
        ("deploy-1113-0257", _with(DEPLOY_ANSWER, affected_component="payments-api"), {"location"}),
        # Not confident enough to open a PR, a citation with no search, and thin evidence.
        ("deploy-1113-0257", _with(DEPLOY_ANSWER, confidence=0.5), {"pr_gate"}),
        ("deploy-1113-0257", _with(DEPLOY_ANSWER, runbook_cited="bad-deploy.md"), {"runbook"}),
        (
            "deploy-1113-0257",
            _with(DEPLOY_ANSWER, evidence=["A deploy happened.", "Errors went up."]),
            {"evidence_change", "evidence_failure", "evidence_symptom"},
        ),
        # The right change at the wrong time: the 07:46 deploy for the 08:03 alarm.
        (
            "deploy-1112-0803",
            _with(DEPLOY_ANSWER, evidence=["Deploy 1.43.0 at 07:46:29 (Simplify promo pricing).", *DEPLOY_ANSWER["evidence"][1:]]),
            {"evidence_change"},
        ),
    ],
)
def test_a_wrong_or_thin_answer_fails_the_items_it_should(scenario: str, hypothesis: dict[str, Any], failed: set[str]) -> None:
    result = rubrics.score(BY_ID[scenario], hypothesis)
    assert {k for k, v in result.items.items() if not v and k != "pr_gate_value"} == failed


def test_a_confident_wrong_diagnosis_is_confidently_wrong() -> None:
    wrong = _with(POOL_ANSWER, root_cause="Release 1.43.0 broke promo pricing; checkouts without a promo raise TypeError.")
    result = rubrics.score(BY_ID["pool-1027-1901"], wrong)
    assert not result.diagnosis and result.confidently_wrong
    halted = _with(wrong, halted_by="step_limit")
    assert not rubrics.score(BY_ID["pool-1027-1901"], halted).confidently_wrong


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


def _saved_as_eval(name: str) -> dict[str, Any]:
    run = dict(SAVED[name])
    run["eval"] = {"run": name, "scenario": SAVED_SCENARIO[run["alarm"]["fired_at"][:19]]}
    return run


def _audit_of(name: str) -> dict[str, Any]:
    """Step 1's hand-audited slips for one saved run, in the blind audit's form: where, quote, kind."""
    slips = [
        {"where": s["where"], "quote": s["quote"], "kind": "loose" if "audited loose" in s["note"] else "strict"}
        for s in SLIPS
        if s["run"] == name and s["source"] == "audit"
    ]
    return {"diagnosis": "right", "slips": slips}


def test_the_matching_rule_scores_step_1s_audits_as_the_labeled_tests_do() -> None:
    """Scored by quote, as a blind audit would be, the saved runs give step 1's numbers:
    26 of 29 strict slips caught, 2 of 5 loose ones graded loose, and the flags no audit
    label matches are exactly the 6 slips the grader found that the audits missed."""
    scores = [score.score_run(_saved_as_eval(n), _audit_of(n)) for n in sorted(SAVED)]
    assert sum(s.strict_caught for s in scores) == 26 and sum(s.strict_labels for s in scores) == 29
    assert sum(s.loose_graded_loose for s in scores) == 2 and sum(s.loose_labels for s in scores) == 5
    unmatched = sorted((s.run, f["where"]) for s in scores for f in s.unmatched_flags)
    new = sorted((s["run"], s["where"]) for s in SLIPS if s["source"] == "new" and s["expect"] == "flagged")
    assert unmatched == new


def test_the_summary_splits_unseen_from_seen_and_waits_for_the_recheck() -> None:
    scores = [score.score_run(_saved_as_eval(n), _audit_of(n)) for n in sorted(SAVED)]
    without = score.summarize(scores, None)
    assert without["unseen"] == {"runs": 0} and without["seen"]["runs"] == 12
    assert "false_flags" not in without["all"]
    recheck = {s.run: [{"call": "audit_miss"} for _ in s.unmatched_flags] for s in scores}
    done = score.summarize(scores, recheck)["all"]
    assert (done["false_flags"], done["audit_misses"], done["recheck_missing"]) == (0, 6, 0)
    assert done["diagnosis_right"] == 12 and done["rubric_agrees_with_audit"] == 12


def test_distinctive_tokens() -> None:
    assert score.distinctive("db_query_p50_ms stays at 61 to 65 ms") == {"db_query_p50_ms", "61", "65"}
    assert score.distinctive("the first failing log is at 19:05:33") == {"19:05:33"}
    assert score.distinctive("0.5 s, 1.04 s, 5% and 1,500 ms") == {"0.5", "1.04", "1500"}


def _has_commit(commit: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=score.REPO, capture_output=True).returncode == 0


@pytest.mark.skipif(not _has_commit(score.FROZEN_GRADER), reason="a shallow clone lacks the frozen commit")
def test_grading_refuses_a_changed_grader(tmp_path: Path) -> None:
    score.check_frozen()  # unchanged since the commit it was frozen at
    with pytest.raises(score.Refusal, match="differs from"):
        score.check_frozen("de44c42")  # before the grader existed


def test_grading_refuses_an_audit_that_isnt_committed(tmp_path: Path) -> None:
    loose = score.REPO / "agent" / "scratch" / "uncommitted-audit.json"
    loose.parent.mkdir(parents=True, exist_ok=True)
    loose.write_text("{}", encoding="utf-8")
    try:
        with pytest.raises(score.Refusal, match="isn't committed"):
            score.check_committed(loose)
    finally:
        loose.unlink()


def test_verdicts_counted_as_flags_are_the_graders() -> None:
    assert {Verdict.UNSUPPORTED, Verdict.CONTRADICTED} == {v for v in Verdict if v not in (Verdict.SUPPORTED, Verdict.LOOSE)}
