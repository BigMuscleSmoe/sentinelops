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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from alarm import AlarmEvent
from answers import CHECKED_PATCH, item
import evals.__main__ as evals_cli
from evals import rubrics, score, sweep
from evals.__main__ import main as evals_main
from evals.scenarios import BY_ID, ERRORS, SCENARIOS, Fault, Scenario, Split, load_window
from evals.witness import ModelWitness, same_model
from checked_runs import RUNS
from checker.__main__ import check_run
from fake_anthropic import FEDERATION_IDS, FakeAnthropic, Reply, answer, tool_use
from pricing import price_for
from providers import Settings
from knowledge_base import KnowledgeBase, KnowledgeBaseError
from stubs import RECORDED_KB, TOOL_FIXTURES, FakeSession, load

# The alarms of step 1's 12 saved runs, which the first grader's rules were written on (kept at 57455ab).
SEEN = {"deploy-1027-1905", "deploy-1029-0156", "deploy-1112-2000"}
# The alarms the checker, the rubric, and the answer's wording were tuned on (every sweep through 20261114T221926Z).
TUNED_ON = {
    "pool-1027-1856", "pool-1027-1900", "pool-1027-1901", "deploy-1027-1905", "deploy-1029-0156",
    "deploy-1112-0747", "deploy-1112-0803", "deploy-1112-2000", "deploy-1113-0257",
}  # fmt: skip
# The test alarms the test sweeps of 2026-11-18 spent, moved to tuning on 2026-11-23 (Phase 3, decision 12).
SPENT = [
    "downstream-1114-2357", "deploy-1116-1917", "pool-1116-2058", "pool-1116-2101-lat", "pool-1116-2101-err", "deploy-1116-2238",
    "pool-1117-0231", "pool-1117-0234", "pool-1117-0235", "downstream-1117-0411", "deploy-1117-0551",
]  # fmt: skip
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
    assert len(SCENARIOS) == 20 and len(BY_ID) == 20


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_each_scenario_replays_offline_and_its_culprit_is_in_deploy_history(scenario) -> None:
    window = load_window(scenario.window)
    alarm = AlarmEvent.as_of(window.client("cloudwatch"), scenario.alarm, scenario.fired_at)
    assert alarm.fired_at == scenario.fired_at and alarm.name == scenario.alarm
    if scenario.culprit_at is None:
        # A fault outside this service: nothing recorded from 10 minutes before its onset to the
        # alarm (the rubric's "well before the onset" is 10 minutes or more).
        assert scenario.fault == Fault.DOWNSTREAM_TIMEOUT and scenario.onset is not None
        assert scenario.onset < scenario.fired_at
        since = (scenario.onset - timedelta(minutes=10)).isoformat()
        near = [i["ts"]["S"] for i in window.meta["deploys"]["items"] if since <= i["ts"]["S"] <= scenario.fired_at.isoformat()]
        assert near == []
        return
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


def test_the_tuning_split_is_all_twenty_alarms_and_a_new_row_must_say_its_split() -> None:
    # The nine first tuned on, then the held-out window's alarm and the fresh breaks' (sessions A
    # and B), spent by the test sweeps. The test split waits for the evaluation cycle's breaks.
    assert [s.id for s in SCENARIOS if s.id not in TUNED_ON] == SPENT
    assert all(s.split is Split.TUNING for s in SCENARIOS) and len(SCENARIOS) == 20
    assert {s.fault for s in SCENARIOS if s.id in SPENT} == set(Fault)  # downstream_timeout's only alarms among them
    with pytest.raises(TypeError, match="split"):
        Scenario("new-0101-0000", "2026-11-13-0257", ERRORS, datetime(2027, 1, 1, tzinfo=UTC), Fault.BAD_DEPLOY, "00:00", seen=False)


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
    """A fixture run's answer, changed, scored with the checker's checks of its evidence.

    The fixtures predate the runbook search and patches, so each cites its
    fault's own runbook, and a bad deploy's carries a patch, as a right answer
    now does, unless a change says otherwise.
    """
    transcript = copy.deepcopy(RUNS[name])
    fault = BY_ID[transcript["eval"]["scenario"]].fault
    transcript["hypothesis"]["runbook_cited"] = rubrics.RULES[fault].runbook
    if fault is Fault.BAD_DEPLOY:
        transcript["hypothesis"]["remediation"] = CHECKED_PATCH
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
    assert rubrics.blamed(sentence, rubrics.RULES[Fault.DB_POOL_EXHAUSTION]) == []
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
    assert rubrics.blamed(sentence, rubrics.RULES[Fault.DB_POOL_EXHAUSTION]) == ["Payments-api"]


# What a downstream_timeout answer names, ruled out or not, as named suspects of their own.
DOWNSTREAM_SHAPED = dataclasses.replace(
    rubrics.RULES[Fault.BAD_DEPLOY], wrong_suspects=re.compile(r"pool wait|database time|payments latency", re.I)
)


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
        ("Payments-api latency rose above normal, so the connection pool backed up.", ["Payments-api", "connection pool"]),
        ("A steady climb in pool wait followed the POOL_MAX_SIZE change.", ["POOL_MAX_SIZE"]),
    ],
)
def test_an_adjective_doesnt_excuse_a_wrong_suspect(sentence: str, blamed: list[str]) -> None:
    assert rubrics.blamed(sentence, rubrics.RULES[Fault.BAD_DEPLOY]) == blamed


@pytest.mark.parametrize(
    "sentence",
    [
        # payments-api named only to place the failure before it: not blamed.
        "The TypeError fires in apply_promo before any payments-api call.",
        "The request never reached payments-api.",
        # The past tense reads as the present does: it failed first, so it never got there.
        "The request failed before it reached payments-api.",
        "Each checkout without a promo returned its 500 before the request reached the payments-api call.",
        # The five saved bad-deploy sentences that say so of "payment" or "payments", rewritten to name it.
        "Every checkout without a promo raises TypeError and returns 500 before any payments-api call, so the error rate jumped from 0% to about 17% of requests.",
        "The unhandled exception returns HTTP 500 within a few milliseconds, before any payments-api or database work.",
        "When a cart has no promo (has_promo=false), it subscripts a None value and raises TypeError, so POST /checkout returns 500 before any payments-api call.",
        "This happens on the request path before any database or payments-api call.",
        "Its pricing path calls apply_promo on carts that have no promo, dereferences a None promo object, and raises TypeError before the request reaches payments-api.",
        # A list after "reaching": the two saved sentences of tuning sweep 20261123T173506Z the rubric failed.
        "Those requests fail with 500 before reaching the DB pool or payments-api, so err% jumps to about 10% of checkouts (roughly half of checkout traffic in the sampled lines) while latency, pool wait and payments stay normal.",
        "In apply_promo (pricing.py:15), the code subscripts a value that is None whenever has_promo is false, so every such POST /checkout raises a TypeError and returns 500 before reaching the database or payments-api.",
    ],
)
def test_a_suspect_the_failure_never_reached_isnt_blamed(sentence: str) -> None:
    assert rubrics.blamed(sentence, rubrics.RULES[Fault.BAD_DEPLOY]) == []
    cause = RUNS[DEPLOY_RUN]["hypothesis"]["root_cause"]
    result = _rubric(DEPLOY_RUN, root_cause=f"{cause} {sentence}")
    assert result.items["blames_no_other"] and result.diagnosis


@pytest.mark.parametrize(
    "sentence, blamed",
    [
        # Never reached excuses the suspect it names, and nothing else in the clause.
        ("The connection pool was exhausted before any payments-api call.", ["connection pool"]),
        ("Payments-api timed out before any database work.", ["Payments-api"]),
        ("Checkouts failed once requests reached payments-api.", ["payments-api"]),
        # "Before it reached" excuses that mention, as "before it reaches" does, and no other.
        ("The outage began before it reached payments-api, which then timed out.", []),
        ("The outage began before it reached payments-api, which then timed out, so payments-api failed checkouts.", ["payments-api"]),
        ("The connection pool was exhausted before it reached payments-api.", ["connection pool"]),
        # No "before": the request got there.
        ("Every request reached payments-api and timed out there.", ["payments-api"]),
        # A list after "reaching" with no failure before it: the request got there.
        ("Checkouts spend about 2 s in pricing before reaching the DB pool and payments-api, which then time out.", ["payments-api"]),
        # After a failure, the list excuses its suspects, and nothing named outside it.
        ("Requests fail with 500 before reaching the DB pool or payments-api, and payments-api timed out.", ["payments-api"]),
    ],
)
def test_never_reached_excuses_only_the_suspect_it_names(sentence: str, blamed: list[str]) -> None:
    assert rubrics.blamed(sentence, rubrics.RULES[Fault.BAD_DEPLOY]) == blamed


@pytest.mark.parametrize(
    "fault, sentence, blamed",
    [
        # A ruling-out covers its own clause: the suspect blamed in the other one is still blamed.
        (Fault.DB_POOL_EXHAUSTION, "Release 1.43.0 broke checkout, while database time was normal.", ["1.43.0"]),
        (Fault.DB_POOL_EXHAUSTION, "Database time was normal, but payments-api timed out.", ["payments-api"]),
        (Fault.BAD_DEPLOY, "The cause is not the release: the connection pool was exhausted.", ["connection pool"]),
        (Fault.BAD_DEPLOY, "The connection pool caused the errors, not the deploy.", ["connection pool"]),
        (Fault.BAD_DEPLOY, "Pool wait stayed at 0, yet the POOL_MAX_SIZE change failed checkouts.", ["POOL_MAX_SIZE"]),
        # And a ruling-out in the suspect's own clause still rules it out.
        (Fault.DB_POOL_EXHAUSTION, "The 1.43.0 deploy at 03:01 was rolled back hours earlier and is not the cause.", []),
        (Fault.DB_POOL_EXHAUSTION, "Query time stayed normal, so the wait comes from the pool size, not the database.", []),
        (Fault.DB_POOL_EXHAUSTION, "The 1.43.0 deploy, which was rolled back hours earlier, is unrelated.", []),
        (Fault.BAD_DEPLOY, "1.43.0, not the connection pool, caused the errors.", []),
        (Fault.BAD_DEPLOY, "Pool wait stayed at 0, so the connection pool is not the cause.", []),
        (Fault.BAD_DEPLOY, "Not the pool: POOL_MAX_SIZE is unchanged at 20, while 1.43.0 fails every checkout without a promo.", []),
    ],
)
def test_a_ruling_out_covers_its_own_clause_not_the_sentence(fault: Fault, sentence: str, blamed: list[str]) -> None:
    assert rubrics.blamed(sentence, rubrics.RULES[fault]) == blamed


@pytest.mark.parametrize(
    "fault, sentence, blamed",
    [
        # "Neither X nor Y" rules out both, in its own clause.
        (Fault.BAD_DEPLOY, "Neither the connection pool nor payments-api was involved.", []),
        (Fault.BAD_DEPLOY, "Neither a POOL_MAX_SIZE change nor payments-api explains the 500s.", []),
        (Fault.DB_POOL_EXHAUSTION, "Neither the 1.43.0 deploy nor payments-api caused the wait.", []),
        # "Nor" alone, and with a clause break between the two halves.
        (Fault.DB_POOL_EXHAUSTION, "Pool wait rose first. Nor was 1.43.0 involved.", []),
        (Fault.BAD_DEPLOY, "Neither the connection pool, though it was busy, nor payments-api caused the errors.", []),
        # It covers its clause and no more: the suspect blamed in the next one is still blamed.
        (Fault.BAD_DEPLOY, "Neither the deploy nor the config was at fault: the connection pool was exhausted.", ["connection pool"]),
        (Fault.BAD_DEPLOY, "Neither the database nor the release failed, but payments-api timed out.", ["payments-api"]),
        (Fault.DB_POOL_EXHAUSTION, "Neither the pool nor the database was the problem; 1.43.0 broke checkout.", ["1.43.0"]),
    ],
)
def test_neither_nor_rules_out_both_in_its_own_clause(fault: Fault, sentence: str, blamed: list[str]) -> None:
    assert rubrics.blamed(sentence, rubrics.RULES[fault]) == blamed


def test_a_right_bad_deploy_answer_that_rules_two_suspects_out_with_neither_nor_passes() -> None:
    cause = "Neither the pool nor payments-api was involved; 1.43.0's TypeError in apply_promo fails every checkout without a promo."
    assert rubrics.blamed(cause, rubrics.RULES[Fault.BAD_DEPLOY]) == []
    result = _rubric(DEPLOY_RUN, root_cause=cause)
    assert result.items["blames_no_other"] and result.diagnosis
    # And appended to the saved answer's own root cause.
    saved = RUNS[DEPLOY_RUN]["hypothesis"]["root_cause"]
    result = _rubric(DEPLOY_RUN, root_cause=f"{saved} Neither the pool nor payments-api was involved.")
    assert result.items["blames_no_other"] and result.diagnosis


@pytest.mark.parametrize(
    "fault, sentence, blamed",
    [
        # Another fault's culprit by its bare noun is blamed where the clause says it caused the failure.
        (Fault.BAD_DEPLOY, "The pool caused the errors.", ["pool"]),
        (Fault.BAD_DEPLOY, "The errors are due to the database.", ["database"]),
        (Fault.BAD_DEPLOY, "Payments caused the 500s.", ["Payments"]),
        (Fault.BAD_DEPLOY, "The pool was the root cause of the failed checkouts.", ["pool"]),
        (Fault.BAD_DEPLOY, "Checkouts with no promo fail because the pool is exhausted.", ["pool"]),
        (Fault.BAD_DEPLOY, "The pool caused the errors before any payments-api call.", ["pool"]),
        (Fault.DB_POOL_EXHAUSTION, "The database caused the wait.", ["database"]),
        (Fault.DB_POOL_EXHAUSTION, "The wait is the result of slow responses from payments.", ["payments"]),
        # A plain mention isn't blame, and the verb belongs to the culprit standing beside it.
        (Fault.BAD_DEPLOY, "1.43.0 caused the errors and the pool was fine.", []),
        (Fault.BAD_DEPLOY, "The database errors were caused by 1.43.0.", []),
        (Fault.BAD_DEPLOY, "Deploy 1.43.0 introduced a TypeError in apply_promo before any database work.", []),
        (Fault.BAD_DEPLOY, "The pool was at 10 of 20 connections when 1.43.0 broke checkout.", []),
        (Fault.BAD_DEPLOY, "Pool wait stayed near zero and database time rose 2 ms.", []),
        # The ruling-out words still cover their clause.
        (Fault.BAD_DEPLOY, "The pool did not cause the errors.", []),
        (Fault.BAD_DEPLOY, "Neither the pool nor the database caused the errors.", []),
        (Fault.BAD_DEPLOY, "The pool is not the cause: 1.43.0 fails every checkout without a promo.", []),
        (Fault.BAD_DEPLOY, "The database is not involved, but the pool caused the errors.", ["pool"]),
        # The pool's own culprit: "the DB pool" is the pool, and its change may be called a deploy.
        (Fault.DB_POOL_EXHAUSTION, "Shrinking the DB pool to 2 caused requests to queue.", []),
        (Fault.DB_POOL_EXHAUSTION, "A config change cut the checkout DB connection pool to 2, which caused requests to queue.", []),
        (Fault.DB_POOL_EXHAUSTION, "With two database connections the pool caused every request to wait.", []),
        (Fault.DB_POOL_EXHAUSTION, "The config deploy at 18:56 caused pool exhaustion.", []),
        # A clause that already blames a named suspect lists that one.
        (Fault.DB_POOL_EXHAUSTION, "Release 1.43.0 broke checkout because the database was overloaded.", ["1.43.0"]),
        (Fault.BAD_DEPLOY, "The connection pool caused the errors.", ["connection pool"]),
    ],
)
def test_a_bare_noun_is_blamed_only_where_its_clause_says_it_caused_the_failure(
    fault: Fault, sentence: str, blamed: list[str]
) -> None:
    assert rubrics.blamed(sentence, rubrics.RULES[fault]) == blamed


@pytest.mark.parametrize(
    "fault, sentence, blamed",
    [
        # An earlier incident, on the other faults' rubrics: over before this one.
        (Fault.BAD_DEPLOY, "Pool exhaustion at 18:56 caused errors then, but it was reverted hours before this alarm.", []),
        (Fault.BAD_DEPLOY, "The pool exhaustion at 18:56 caused errors. That change was rolled back at 19:03.", []),
        (Fault.BAD_DEPLOY, "Pool exhaustion at 18:56 caused errors then.", ["Pool exhaust"]),
        (Fault.DB_POOL_EXHAUSTION, "Release 1.43.0 broke checkout at 18:50, but it was rolled back well before this alarm.", []),
        (Fault.DB_POOL_EXHAUSTION, "Release 1.43.0 broke checkout.", ["1.43.0"]),
        # A clock time between a bare noun and its verb, or between a denial and the noun, changes nothing.
        (Fault.BAD_DEPLOY, "The pool at 02:57 caused the errors.", ["pool"]),
        (Fault.BAD_DEPLOY, "No 02:57 pool change caused the errors.", []),
    ],
)
def test_an_earlier_incident_is_ruled_out_and_a_clock_time_alone_is_not(
    fault: Fault, sentence: str, blamed: list[str]
) -> None:
    assert rubrics.blamed(sentence, rubrics.RULES[fault]) == blamed


def test_a_bad_deploy_answer_that_also_blames_the_pool_by_its_bare_noun_fails() -> None:
    saved = RUNS[DEPLOY_RUN]["hypothesis"]["root_cause"]
    result = _rubric(DEPLOY_RUN, root_cause=f"{saved} The pool caused the errors.")
    assert not result.items["blames_no_other"] and not result.diagnosis
    assert result.notes["blames_no_other"] == "blames pool"
    # A plain mention beside the same right answer changes nothing.
    result = _rubric(DEPLOY_RUN, root_cause=f"{saved} Pool wait stayed near zero and database time rose 2 ms.")
    assert result.items["blames_no_other"] and result.diagnosis


def test_the_rubric_s_record_kinds_are_the_recorded_history_s() -> None:
    """A split "nothing changed" must rule out every kind deploy history holds."""
    kinds = {
        item["kind"]["S"]
        for name in {scenario.window for scenario in SCENARIOS}
        for item in load_window(name).meta["deploys"]["items"]
    }
    assert kinds == rubrics.RECORD_KINDS


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
        # Not confident enough to open a PR; no runbook cited, or another fault's.
        (DEPLOY_RUN, {"confidence": 0.5}, {"pr_gate"}),
        (DEPLOY_RUN, {"runbook_cited": None}, {"runbook"}),
        (DEPLOY_RUN, {"runbook_cited": "db-pool-exhaustion.md"}, {"runbook"}),
        (POOL_RUN, {"runbook_cited": "slow-database.md"}, {"runbook"}),
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
    # A pool fault blamed on the bad release, with the release's patch: a PR would open.
    wrong = {"root_cause": "Release 1.43.0 broke promo pricing; checkouts without a promo raise TypeError.", "remediation": CHECKED_PATCH}
    result = _rubric(POOL_RUN, **wrong)
    assert not result.diagnosis and result.confidently_wrong
    assert not _rubric(POOL_RUN, **wrong, halted_by="step_limit").confidently_wrong
    # Without a patch nothing opens (Phase 4): wrong, but not confidently so.
    assert not _rubric(POOL_RUN, **{**wrong, "remediation": None}).confidently_wrong


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
    assert saved["split"] == "tuning" and saved["commit"] == sweep.repo.head()
    transcript = json.loads((tmp_path / "deploy-1113-0257-r1.json").read_text(encoding="utf-8"))
    assert transcript["eval"]["scenario"] == "deploy-1113-0257" and transcript["eval"]["seen"] is False
    assert transcript["eval"]["split"] == "tuning"
    assert transcript["tool_calls_after_ask"] is None  # each answered without being asked
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


def test_a_sweep_is_one_splits_and_says_which(tmp_path: Path) -> None:
    tuning, held_out = BY_ID["deploy-1113-0257"], dataclasses.replace(BY_ID["pool-1027-1856"], split=Split.TEST)
    with pytest.raises(ValueError, match="a sweep is one split's: these scenarios are test and tuning"):
        _sweep([tuning, held_out], 1, FakeAnthropic([]), FakeSession(), tmp_path)
    assert not (tmp_path / "sweep.json").exists()  # refused before anything ran

    fake, sts = _script(1, POOL_ANSWER), _sts(1)
    _sweep([held_out], 1, fake, sts, tmp_path)
    saved = json.loads((tmp_path / "sweep.json").read_text(encoding="utf-8"))
    transcript = json.loads((tmp_path / "pool-1027-1856-r1.json").read_text(encoding="utf-8"))
    assert saved["split"] == "test" and transcript["eval"]["split"] == "test"


def _begun(folder: Path, **changes: Any) -> dict[str, Any]:
    """A sweep.json as a tuning Haiku sweep begun on this commit and tree leaves it, changed."""
    meta = {
        "sweep": folder.name, "arm": "haiku", "split": "tuning", "commit": sweep.repo.head(),
        "dirty": sweep.repo.dirty(), "records": [], **changes,
    }  # fmt: skip
    (folder / "sweep.json").write_text(json.dumps(meta), encoding="utf-8")
    return meta


@pytest.mark.parametrize(
    ("changes", "refusal"),
    [
        # sweep.json holds one commit for all its runs: resumed elsewhere, it would name the wrong one.
        ({"commit": "0" * 40}, "ran on 0000000, and HEAD is [0-9a-f]{7}: a sweep is one commit's, so start a new one"),
        ({"commit": None}, "ran on no recorded commit, and HEAD is [0-9a-f]{7}"),
        ({"split": "test"}, "is a test sweep, not a tuning one"),
        ({"arm": "sonnet"}, "is the sonnet arm's sweep, not haiku's"),
    ],
)
def test_a_sweep_is_resumed_only_as_what_it_is(tmp_path: Path, changes: dict[str, Any], refusal: str) -> None:
    before = _begun(tmp_path, **changes)
    with pytest.raises(ValueError, match=refusal):
        _sweep([BY_ID["deploy-1113-0257"]], 1, FakeAnthropic([]), FakeSession(), tmp_path)
    assert json.loads((tmp_path / "sweep.json").read_text(encoding="utf-8")) == before  # not rewritten, nothing run


def test_a_sweep_begun_on_a_clean_tree_isnt_resumed_on_a_changed_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _begun(tmp_path, dirty=[])
    monkeypatch.setattr(sweep.repo, "dirty", lambda: ["agent/runner.py"])
    with pytest.raises(ValueError, match="ran on a clean tree, and it has uncommitted changes now: agent/runner.py"):
        _sweep([BY_ID["deploy-1113-0257"]], 1, FakeAnthropic([]), FakeSession(), tmp_path)


def test_a_sweep_on_its_own_commit_resumes_and_one_from_before_the_split_is_a_tuning_one(tmp_path: Path) -> None:
    meta = _begun(tmp_path)
    del meta["split"]
    (tmp_path / "sweep.json").write_text(json.dumps(meta), encoding="utf-8")
    fake, sts = _script(1), _sts(1)

    records = _sweep([BY_ID["deploy-1113-0257"]], 1, fake, sts, tmp_path)

    assert [r.status for r in records] == ["answered"]
    saved = json.loads((tmp_path / "sweep.json").read_text(encoding="utf-8"))
    assert (saved["split"], saved["commit"]) == ("tuning", meta["commit"])


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


def test_a_sweep_searches_the_checked_knowledge_base_through_the_sign_in_and_records_its_corpus(tmp_path: Path) -> None:
    """The window answers every read but the search, which goes to the live knowledge base."""
    search = load(TOOL_FIXTURES / "runbooks_pool.json")
    query = search["calls"][0]["params"]["retrievalQuery"]["text"]
    fake = FakeAnthropic(
        [
            Reply([tool_use("get_metrics", {"minutes": 10, "period_seconds": 60})]),
            Reply([tool_use("search_runbooks", {"query": query})]),
            answer({**POOL_ANSWER, "runbook_cited": "db-pool-exhaustion.md"}),
        ]
    )
    sts = _sts(1)
    sts.replay(search)  # the recorded Retrieve, asked of the sign-in session's client
    runbooks = KnowledgeBase(RECORDED_KB, "197a0201c9eb")

    (record,) = _sweep([BY_ID["pool-1027-1856"]], 1, fake, sts, tmp_path, knowledge_base=runbooks)

    sts.assert_all_called()
    assert (record.status, record.tool_calls) == ("answered", 2)
    assert fake.tool_results(2)[1].startswith("[source r1]\n" + search["text"])
    transcript = json.loads((tmp_path / "pool-1027-1856-r1.json").read_text(encoding="utf-8"))
    assert transcript["hypothesis"]["runbook_cited"] == "db-pool-exhaustion.md" and transcript["citation_dropped"] is None
    assert json.loads((tmp_path / "sweep.json").read_text(encoding="utf-8"))["runbooks_corpus"] == "197a0201c9eb"

    # Resumed, the sweep must search the same runbooks.
    with pytest.raises(ValueError, match="searched runbooks corpus 197a0201c9eb, and this run's is 0123456789ab"):
        _sweep([BY_ID["pool-1027-1856"]], 1, FakeAnthropic([]), FakeSession(), tmp_path, knowledge_base=KnowledgeBase(RECORDED_KB, "0123456789ab"))


def test_a_window_session_hands_out_only_the_search_and_the_sign_in_from_aws() -> None:
    sign_in = FakeSession()
    session = sweep.WindowSession(load_window("2026-11-13-0257"), sign_in)
    assert session.client("bedrock-agent-runtime") is sign_in.clients["bedrock-agent-runtime"]
    assert session.client("sts") is sign_in.clients["sts"]
    for service in ("bedrock-agent", "lambda", "s3"):
        with pytest.raises(ValueError, match=f"a replay has no {service} client"):
            session.client(service)


def test_a_sweep_on_a_knowledge_base_that_fails_its_check_is_refused_before_it_spends(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_sweep: None
) -> None:
    def stale(session: Any) -> KnowledgeBase:
        raise KnowledgeBaseError("sentinelops-runbooks doesn't hold this checkout's runbooks (corpus abc)")

    monkeypatch.setattr(evals_cli, "connect_knowledge_base", stale)
    monkeypatch.setattr("boto3.Session", lambda **kwargs: object())  # no profile needed: nothing reaches AWS
    monkeypatch.delenv("SENTINELOPS_MODEL_ID", raising=False)
    for name, value in FEDERATION_IDS.items():  # CI has none of its own
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(tmp_path)  # a sweep that got past the refusal would write here, not in scratch/

    assert evals_main(["sweep", "--go", "--runs", "1", "--scenario", "pool-1027-1856"]) == 1

    assert "Refused: sentinelops-runbooks doesn't hold this checkout's runbooks (corpus abc)" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []  # no sweep folder: nothing started


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
    assert "60 runs (20 scenarios x 3) of the tuning split" in out and "at most $1.00" in out and "Nothing ran" in out


@pytest.fixture
def no_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test of a refusal passes --go: if the refusal ever goes, the test fails here and no run starts."""

    def started(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a sweep started")

    monkeypatch.setattr(evals_cli, "run_sweep", started)


def test_a_plan_takes_one_split_the_tuning_one_unless_asked(capsys: pytest.CaptureFixture[str]) -> None:
    assert evals_main(["plan"]) == 0
    out = capsys.readouterr().out
    assert "60 runs (20 scenarios x 3) of the tuning split" in out
    # The spent test alarms are tuning ones since 2026-11-23.
    assert "downstream-1114-2357  downstream_timeout  tuning unseen sentinelops-checkout-latency at 2026-11-14 23:57:37Z" in out
    # The test split is empty until the evaluation cycle's test breaks.
    assert evals_main(["plan", "--split", "test"]) == 1
    assert "The test split has no scenarios yet." in capsys.readouterr().err
    # A named scenario must be in the split asked for.
    assert evals_main(["plan", "--scenario", "downstream-1114-2357", "--split", "test"]) == 1
    assert "Refused: not in the test split: downstream-1114-2357" in capsys.readouterr().err
    assert evals_main(["plan", "--scenario", "pool-1027-1856", "--scenario", "downstream-1114-2357"]) == 0


def test_a_split_with_no_scenarios_plans_nothing(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, no_sweep: None
) -> None:
    monkeypatch.setattr(evals_cli, "SCENARIOS", [s for s in SCENARIOS if s.split is Split.TUNING])
    assert evals_main(["sweep", "--go", "--split", "test"]) == 1
    assert "The test split has no scenarios yet." in capsys.readouterr().err


def test_a_test_sweep_is_refused_before_the_freeze_and_before_it_spends(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_sweep: None
) -> None:
    bar = tmp_path / "bar.json"
    bar.write_text(json.dumps({**json.loads(score.PASS_BAR.read_text(encoding="utf-8")), "frozen": False}), encoding="utf-8")
    monkeypatch.setattr(score, "PASS_BAR", bar)  # whatever the committed bar says by now
    monkeypatch.chdir(tmp_path)  # a sweep that got past the refusal would write here, not in scratch/
    # The test split is empty since 2026-11-23; a held-out alarm stands in for the next one.
    monkeypatch.setattr(evals_cli, "SCENARIOS", [dataclasses.replace(BY_ID["downstream-1114-2357"], split=Split.TEST)])

    assert evals_main(["sweep", "--go", "--split", "test"]) == 1

    assert "Refused: the test split runs only after the freeze, and the pass bar isn't frozen" in capsys.readouterr().err
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bar.json"]  # no sweep folder: nothing started


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

