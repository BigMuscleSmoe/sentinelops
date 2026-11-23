"""The downstream_timeout rubric, dry-run on hand-written answers before any agent run on the fault.

No window holds the fault yet (its measuring break is the held-out test
window), so each answer's checks are written by hand, as the checker would
return them for an answer whose items all hold.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from answers import CONFIG_1_42_0, checked
from checker.checks import ItemCheck, Verdict
from checker.resolve import Entry
from evals import rubrics
from evals.scenarios import LATENCY, Fault, Scenario, Split

FIRED = datetime(2026, 11, 15, 22, 29, 5, tzinfo=UTC)
ONSET = datetime(2026, 11, 15, 22, 28, 29, tzinfo=UTC)  # the break
# Hand-written, and what the rubric's rules were tuned on: never a test scenario.
SCENARIO = Scenario(
    "downstream-dry-run", "none", LATENCY, FIRED, Fault.DOWNSTREAM_TIMEOUT, None, seen=False, onset=ONSET, split=Split.TUNING
)
ROW = datetime(2026, 11, 15, 22, 28, 40, tzinfo=UTC)
FIELDS = ("at", "column", "count", "end", "high", "kind", "lines", "low", "note", "seconds", "source", "start", "value")


def item(**stated: Any) -> dict[str, Any]:
    """An evidence item as the strict schema sends it: every field, null where unset."""
    return {name: stated.get(name) for name in FIELDS}


def row(column: str, value: float) -> Entry:
    return Entry(f"m1 {ROW:%H:%M:%S}", ROW, timedelta(seconds=10), {column: value})


# A right answer's evidence: nothing changed before the onset, payments-api's
# p99 at its timeout, the alarm's p99, and this service's pool wait at zero.
EVIDENCE = [
    item(kind="none", source="d1", start="22:20:00", end="22:29:05", note="no deploy or config change since 22:20"),
    item(kind="value", source="m1", column="payments_p99_ms", at="22:28:40", value="2000", note="payments-api p99 at the 2 s timeout"),
    item(kind="value", source="m1", column="p99_ms", at="22:28:40", value="2210", note="p99 over the 1.5 s threshold"),
    item(kind="range", source="m1", column="pool_wait_p99_ms", start="22:28:30", end="22:29:00", low="0", high="0", note="no pool wait"),
]
CHECKS = [
    ItemCheck("e1", "none", "d1", Verdict.HOLDS, reason="nothing matches in the window (0 records in it)"),
    ItemCheck("e2", "value", "m1", Verdict.HOLDS, column="payments_p99_ms", entries=[row("payments_p99_ms", 2000.0)]),
    ItemCheck("e3", "value", "m1", Verdict.HOLDS, column="p99_ms", entries=[row("p99_ms", 2210.0)]),
    ItemCheck("e4", "range", "m1", Verdict.HOLDS, column="pool_wait_p99_ms", entries=[row("pool_wait_p99_ms", 0.0)]),
]
RIGHT = {
    "root_cause": (
        "payments-api slowed down at 22:28:30: its p99 rose to 2,000 ms and about 4% of checkouts passed "
        "checkout-api's 2 s client timeout (PAYMENTS_TIMEOUT_MS) and returned 504 UpstreamTimeout, with and "
        "without a promo code. This service's own work is healthy: pool wait stayed at 0 ms and database time "
        "held steady near 300 ms. Nothing in deploy history changed in the 9 minutes before the alarm; the "
        "newest record, LOG_SAMPLE_RATE at 17:46, is hours earlier and unrelated."
    ),
    "confidence": 0.85,
    "affected_component": "payments-api",
    "fault_location": "upstream",
    "evidence": EVIDENCE,
    "inferences": [{"claim": "The slowness is payments-api's, not this service's.", "rests_on": ["e1", "e2", "e4"]}],
    "not_checked": ["Why payments-api slowed down: its own metrics and logs aren't readable from here."],
    "runbook_cited": "downstream-timeout.md",
    "halted_by": None,
}


# What a wrong in-service diagnosis of a slow payments-api would patch: a longer
# client timeout, as the run's check would keep it. From Phase 4 a PR needs a
# kept patch, so the confidently wrong answers below carry this one; without
# it, nothing would open.
WRONG_PATCH = checked(
    {
        "source": "c1",
        "file_path": "checkout/config.py",
        "edits": [{"old": 'env.get("PAYMENTS_TIMEOUT_MS", "2000")', "new": 'env.get("PAYMENTS_TIMEOUT_MS", "5000")'}],
        "rationale": "payments-api takes more than 2 s to answer; a 5 s client timeout lets checkouts wait for it.",
    },
    CONFIG_1_42_0,
    "1.42.0",
)


def answer(**changes: Any) -> dict[str, Any]:
    return {**RIGHT, **changes}


def score(hypothesis: dict[str, Any], checks: list[ItemCheck] = CHECKS) -> rubrics.RubricResult:
    return rubrics.score(SCENARIO, hypothesis, checks)


def test_a_right_upstream_answer_passes_every_item():
    result = score(RIGHT)
    assert result.items == {
        "names_culprit": True,
        "names_mechanism": True,
        "blames_no_other": True,
        "location": True,
        "pr_gate_value": False,
        "pr_gate": True,
        "runbook": True,
        "evidence_change": True,
        "evidence_failure": True,
        "evidence_symptom": True,
        "evidence_health": True,
    }
    assert result.diagnosis and result.passed() and not result.confidently_wrong


def test_blaming_the_decoy_is_wrong_and_confidently_so():
    result = score(answer(
        root_cause="The LOG_SAMPLE_RATE change raised debug logging to a quarter of requests, which slowed checkouts past 1.5 s.",
        affected_component="checkout-api",
        fault_location="this_service",
        confidence=0.8,
        remediation=WRONG_PATCH,
    ))
    assert not result.items["names_culprit"] and not result.items["blames_no_other"] and not result.items["location"]
    assert result.notes["blames_no_other"] == "blames LOG_SAMPLE_RATE"
    assert not result.diagnosis and result.confidently_wrong


def test_blaming_the_pool_is_wrong_and_confidently_so():
    result = score(answer(
        root_cause="The connection pool is exhausted under the burst, so checkouts queue for a connection and time out.",
        affected_component="checkout-api DB connection pool",
        fault_location="this_service",
        confidence=0.75,
        remediation=WRONG_PATCH,
    ))
    assert not result.items["blames_no_other"] and not result.items["location"]
    assert result.notes["blames_no_other"] == "blames connection pool"
    assert not result.diagnosis and result.confidently_wrong


def test_a_confident_wrong_answer_with_no_patch_would_open_no_pr():
    # From Phase 4, can_open_pr needs a patch the run's check kept: the same wrong,
    # confident answer without one is wrong, but opens nothing.
    result = score(answer(
        root_cause="The connection pool is exhausted under the burst, so checkouts queue for a connection and time out.",
        affected_component="checkout-api DB connection pool",
        fault_location="this_service",
        confidence=0.75,
        remediation=None,
    ))
    assert not result.diagnosis and not result.confidently_wrong
    assert not result.items["pr_gate_value"] and result.items["pr_gate"]


def test_payments_api_named_but_placed_in_this_service_is_the_failure_that_would_open_a_pr():
    """The key failure: the right culprit and mechanism, the wrong side of the boundary,
    confident enough to pass the PR gate against healthy code."""
    result = score(answer(
        root_cause=(
            "Checkouts time out waiting on payments-api: checkout-api's 2 s payments timeout is too short "
            "for payments-api's slower responses, so about 4% of checkouts return 504."
        ),
        affected_component="checkout-api payments client",
        fault_location="this_service",
        confidence=0.9,
        remediation=WRONG_PATCH,
    ))
    assert result.items["names_culprit"] and result.items["names_mechanism"] and result.items["blames_no_other"]
    assert not result.items["location"]
    assert result.items["pr_gate_value"] and not result.items["pr_gate"]
    assert not result.diagnosis and result.confidently_wrong


def test_undetermined_is_not_upstream_but_is_not_confidently_wrong():
    result = score(answer(fault_location="undetermined"))
    assert not result.items["location"] and not result.diagnosis
    assert not result.items["pr_gate_value"] and not result.confidently_wrong


def test_a_suspect_blamed_beside_a_ruling_out_phrase_is_still_blamed():
    """One sentence blames the pool and rules the database out: the ruling-out
    covers the database, not the pool."""
    result = score(answer(
        root_cause="Pool exhaustion caused the timeouts, while database time was normal.",
        affected_component="checkout-api",
        fault_location="this_service",
        remediation=WRONG_PATCH,
    ))
    assert not result.items["blames_no_other"]
    assert result.notes["blames_no_other"] == "blames Pool exhaust"
    assert not result.diagnosis and result.confidently_wrong


def test_a_right_answer_that_rules_the_pool_and_the_database_out_with_neither_nor_passes():
    cause = (
        "payments-api slowed down and about 4% of checkouts hit the 2000 ms timeout and returned 504. "
        "Neither the connection pool nor the database caused the slowdown."
    )
    result = score(answer(root_cause=cause))
    assert result.items["blames_no_other"] and result.diagnosis


@pytest.mark.parametrize(
    "sentence, blamed",
    [
        # A bare noun is blamed where its clause says it caused the failure...
        ("The database caused the slowdown, and payments-api timed out.", ["database"]),
        ("The database was the root cause of the slow checkouts.", ["database"]),
        ("The slowdown was due to the database.", ["database"]),
        ("The root cause is the database.", ["database"]),
        ("Checkouts slowed because the pool ran out of connections.", ["pool"]),
        ("The latency was caused by contention in the pool.", ["pool"]),
        ("The pool is to blame for the 504s.", ["pool"]),
        ("Pool wait caused the latency.", ["Pool"]),
        ("A recent deploy introduced the slowdown.", ["deploy"]),
        ("The slowdown stems from the last release.", ["release"]),
        # ...and nowhere else: a plain mention, a reading given as a reason, a verb that is payments-api's.
        ("Database time rose 2 ms while payments-api timed out.", []),
        ("Pool wait stayed near zero and database time rose 2 ms.", []),
        ("Because pool wait stayed under 1 ms and database time was level, the fault is upstream.", []),
        ("payments-api is at fault because the deploy history is empty.", []),
        ("payments-api caused the timeouts, with the pool at 10 of 20 connections.", []),
        ("The 504s were caused by payments-api, and the pool and the database were quiet.", []),
        ("The pool was at 10 of 20 connections when payments-api caused the timeouts.", []),
        ("The order's connection goes back to the pool before the charge.", []),
        # Not asserted of it: denied, or only supposed.
        ("No deploy caused the slowdown.", []),
        ("Nothing in the pool or the database caused it.", []),
        ("If the pool were the cause, pool wait would have risen.", []),
        ("The pool did not cause the timeouts.", []),
        ("The database was not the cause: payments-api timed out.", []),
    ],
)
def test_a_bare_noun_is_blamed_only_where_its_clause_says_it_caused_the_failure(sentence, blamed):
    assert rubrics.blamed(sentence, rubrics.RULES[Fault.DOWNSTREAM_TIMEOUT]) == blamed


def test_an_answer_that_blames_the_database_beside_payments_api_fails():
    """It names the culprit and the mechanism, and puts the fault upstream; it still blames the database."""
    result = score(answer(root_cause="The database caused the slowdown, and payments-api timed out."))
    assert result.items["names_culprit"] and result.items["names_mechanism"] and result.items["location"]
    assert not result.items["blames_no_other"] and not result.diagnosis
    assert result.notes["blames_no_other"] == "blames database"
    # The plain mention beside the same culprit passes.
    result = score(answer(root_cause="Database time rose 2 ms while payments-api timed out."))
    assert result.items["blames_no_other"] and result.diagnosis


@pytest.mark.parametrize(
    "sentence, blamed",
    [
        ("Database time was normal, but pool exhaustion caused the timeouts.", ["pool exhaust"]),
        ("The cause is not the database: the connection pool was exhausted.", ["connection pool"]),
        ("Pool exhaustion caused the timeouts, not payments-api.", ["Pool exhaust"]),
        ("Payments-api latency rose above normal, so the connection pool backed up.", ["connection pool"]),
        # Right answers' ruling-outs, each in the suspect's own clause.
        ("Not the pool: pool wait stayed at 0 ms, while payments-api p99 hit 2 s.", []),
        ("Pool wait stayed at 0, so the connection pool is not the cause.", []),
        ("The newest record, LOG_SAMPLE_RATE at 17:46, is hours earlier and unrelated.", []),
        ("Rather than pool exhaustion, the timeouts came from payments-api.", []),
        ("The slowness is in payments-api, not the connection pool or the database.", []),
        ("The 1.43.0 deploy, which was rolled back hours earlier, is unrelated.", []),
        ("The 504s are returned before any TypeError could be raised.", []),
        # "Neither X nor Y" rules out both, in its own clause.
        ("Neither the connection pool nor the database caused the slowdown.", []),
        ("Neither the connection pool nor a slow database caused the slowdown.", []),
        ("Neither pool exhaustion nor the 1.43.0 deploy explains the 504s, while payments-api p99 hit 2 s.", []),
        ("Neither the deploy history nor the logs show a change, yet the connection pool backed up.", ["connection pool"]),
        ("Neither payments-api nor the network was slow: pool exhaustion caused the timeouts.", ["pool exhaust"]),
        # Never reached, in the past tense.
        ("The request timed out before it reached apply_promo.", []),
    ],
)
def test_a_ruling_out_covers_its_own_clause(sentence, blamed):
    assert rubrics.blamed(sentence, rubrics.RULES[Fault.DOWNSTREAM_TIMEOUT]) == blamed


@pytest.mark.parametrize(
    "sentence, blamed",
    [
        # Rolled back, reverted, or resolved: the earlier incident is over.
        ("1.43.0 caused 500s at 22:28, but it was rolled back well before onset", []),
        ("The 22:28 deploy caused errors then and was reverted", []),
        ("1.43.0 caused 500s at 22:28 and was rolled back at 22:29.", []),
        ("1.43.0 was deployed at 22:28:29Z, with a rollback to 1.42.0 at 22:29:53Z.", []),
        # A time well before the onset.
        ("1.43.0 caused 500s at 22:28, hours before this alarm.", []),
        ("Deploy 1.43.0 broke checkout 88 minutes before the alarm.", []),
        ("1.43.0 caused 500s at 22:28, before this alarm.", []),
        ("The deploy of 1.43.0 happened before the onset.", []),
        ("The 1.43.0 deploy broke checkout for 84 s and ended before this incident.", []),
        # Said of "it" in the clause that follows, in the same sentence or the next.
        ("The 22:28 deploy caused 500s, but that happened before the onset.", []),
        ("The 22:28 deploy caused 500s then, but was resolved long before the onset.", []),
        ("1.43.0 caused 500s at 22:28. It was rolled back at 22:29:53.", []),
        ("1.43.0 caused 500s at 22:28; that deploy was reverted a minute later.", []),
        # A time alone rules nothing out.
        ("A deploy at 23:56 caused the slowdown", ["deploy"]),
        ("The 22:28 deploy caused 500s then.", ["deploy"]),
        # "Before the onset" after a short interval, or with what it caused, is how a cause is told.
        ("A deploy 30 s before the onset caused the slowdown.", ["deploy"]),
        ("A deploy just before the alarm caused the slowdown.", ["deploy"]),
        ("A deploy before the alarm caused the slowdown.", ["deploy"]),
        ("A deploy 2 minutes before the onset caused the slowdown.", ["deploy"]),
        ("The deploy that happened before the onset caused the slowdown.", ["deploy"]),
        ("1.43.0 was deployed 40 seconds before the onset.", ["1.43.0"]),
        ("1.43.0 was deployed 1.25 minutes before the onset.", ["1.43.0"]),
        ("The connection pool was exhausted before the alarm fired.", ["connection pool"]),
        ("POOL_MAX_SIZE was cut to 2 before the alarm.", ["POOL_MAX_SIZE"]),
        # The clause after covers the one before only when it speaks of "it", names no suspect of its
        # own, and tells of an earlier incident: no other ruling-out word reaches back.
        ("The pool caused the timeouts, while the earlier errors were resolved before the onset.", ["pool"]),
        ("Pool exhaustion caused the timeouts, but 1.43.0 was rolled back well before onset.", ["Pool exhaust"]),
        ("A deploy at 23:56 caused the slowdown, while 1.43.0 was rolled back at 22:29.", ["deploy"]),
        ("Pool exhaustion caused the timeouts, but it was not obvious from err%.", ["Pool exhaust"]),
        ("Pool exhaustion caused the timeouts, but that is unrelated to the config change.", ["Pool exhaust"]),
    ],
)
def test_a_clause_that_tells_of_an_earlier_incident_is_ruled_out(sentence, blamed):
    assert rubrics.blamed(sentence, rubrics.RULES[Fault.DOWNSTREAM_TIMEOUT]) == blamed


def test_a_right_answer_that_dismisses_the_earlier_break_passes_and_one_blaming_a_deploy_fails():
    """The held-out window holds the 22:28 bad deploy: a right answer may tell of it to set it aside."""
    right = RIGHT["root_cause"]
    for dismissal in (
        "1.43.0 caused 500s at 22:28, but it was rolled back well before onset.",
        "The 22:28 deploy caused errors then and was reverted.",
    ):
        result = score(answer(root_cause=f"{right} {dismissal}"))
        assert result.items["blames_no_other"] and result.diagnosis
    result = score(answer(root_cause=f"{right} A deploy at 23:56 caused the slowdown."))
    assert not result.items["blames_no_other"] and not result.diagnosis
    assert result.notes["blames_no_other"] == "blames deploy"


@pytest.mark.parametrize(
    "sentence, blamed",
    [
        ("Checkouts passed checkout-api's 2000 ms PAYMENTS_TIMEOUT_MS and returned 504.", []),
        ("The 504s came with and without a promo code.", []),
        ("PAYMENTS_TIMEOUT_MS was lowered to 500 ms, so checkouts time out.", ["PAYMENTS_TIMEOUT_MS was lowered"]),
        ("Release 1.43.0 broke checkout.", ["1.43.0"]),
        ("Slow queries held connections longer.", ["Slow quer"]),
    ],
)
def test_naming_the_timeout_or_a_promo_isnt_blame_but_a_change_to_it_is(sentence, blamed):
    assert rubrics.blamed(sentence, rubrics.RULES[Fault.DOWNSTREAM_TIMEOUT]) == blamed


@pytest.mark.parametrize(
    "stated, verdict, quiet",
    [
        ({"start": "22:20:00", "end": "22:29:05"}, Verdict.HOLDS, True),
        ({"start": "21:00", "end": "22:29"}, Verdict.HOLDS, True),  # wider is fine; 22:29 runs to 22:29:59
        ({"start": "2026-11-15T22:23:29Z", "end": "2026-11-15T22:28:29Z"}, Verdict.HOLDS, True),  # exactly the 5 minutes
        ({"start": "22:26:00", "end": "22:29:05"}, Verdict.HOLDS, False),  # only 2.5 minutes before the onset
        ({"start": "22:10:00", "end": "22:25:00"}, Verdict.HOLDS, False),  # ends before the onset
        ({"start": "22:20:00", "end": "22:29:05", "value": "deploy", "column": "kind"}, Verdict.HOLDS, False),  # no deploy, but a config change?
        ({"start": "22:20:00", "end": "22:29:05", "column": "kind"}, Verdict.HOLDS, True),  # no record has a kind: no record
        ({"start": "22:20:00", "end": "22:29:05", "column": "version", "value": "1.43.0"}, Verdict.HOLDS, False),  # only that no record is 1.43.0
        ({"start": "22:20:00", "end": "22:29:05", "column": "POOL_MAX_SIZE"}, Verdict.HOLDS, False),  # only that key
        ({"start": "22:20:00", "end": "22:29:05"}, Verdict.CONTRADICTED, False),
    ],
)
def test_nothing_changed_needs_a_none_containing_the_5_minutes_before_the_onset(stated, verdict, quiet):
    evidence = [item(kind="none", source="d1", note="no change in the window", **stated), *EVIDENCE[1:]]
    checks = [ItemCheck("e1", "none", "d1", verdict), *CHECKS[1:]]
    assert score(answer(evidence=evidence), checks).items["evidence_change"] is quiet


def _split(deploys: dict[str, Any], config: dict[str, Any], verdicts=(Verdict.HOLDS, Verdict.HOLDS)) -> bool:
    """evidence_change for an answer that rules out deploys and config changes in two nones."""
    window = {"start": "22:20:00", "end": "22:29:05"}
    evidence = [
        item(**{"kind": "none", "source": "d1", "column": "kind", "value": "deploy", "note": "no deploy", **window, **deploys}),
        item(**{"kind": "none", "source": "d1", "column": "kind", "value": "config", "note": "no config change", **window, **config}),
        *EVIDENCE[1:],
    ]
    checks = [
        ItemCheck("e1", "none", "d1", verdicts[0], column="kind"),
        ItemCheck("e2", "none", "d1", verdicts[1], column="kind"),
        ItemCheck("e3", "value", "m1", Verdict.HOLDS, column="payments_p99_ms", entries=[row("payments_p99_ms", 2000.0)]),
        ItemCheck("e4", "value", "m1", Verdict.HOLDS, column="p99_ms", entries=[row("p99_ms", 2210.0)]),
        ItemCheck("e5", "range", "m1", Verdict.HOLDS, column="pool_wait_p99_ms", entries=[row("pool_wait_p99_ms", 0.0)]),
    ]
    inferences = [{"claim": "The slowness is payments-api's, not this service's.", "rests_on": ["e1", "e2", "e3"]}]
    return score(answer(evidence=evidence, inferences=inferences), checks).items["evidence_change"]


def test_nothing_changed_split_into_a_none_for_each_kind_of_record_passes():
    """No deploy and no config change, as two items, say what one none with no column says."""
    assert _split({}, {})
    assert _split({"start": "21:00"}, {"end": "22:28:29"})  # their windows needn't match, only hold the 5 minutes
    assert _split({"value": "Deploy"}, {"value": "'config'"})  # as the model may write the value


@pytest.mark.parametrize(
    "deploys, config, verdicts",
    [
        ({}, {"value": "deploy"}, (Verdict.HOLDS, Verdict.HOLDS)),  # deploys twice, config changes never
        ({}, {"start": "22:26:00"}, (Verdict.HOLDS, Verdict.HOLDS)),  # the config none covers only 2.5 minutes
        ({"end": "22:25:00"}, {}, (Verdict.HOLDS, Verdict.HOLDS)),  # the deploy none ends before the onset
        ({}, {}, (Verdict.HOLDS, Verdict.CONTRADICTED)),  # a config change is in the window
        ({}, {}, (Verdict.OUTSIDE_READ, Verdict.HOLDS)),
        ({}, {"value": "rollback"}, (Verdict.HOLDS, Verdict.HOLDS)),  # not a kind the history holds
    ],
)
def test_a_split_that_leaves_a_kind_of_record_open_doesnt_pass(deploys, config, verdicts):
    assert not _split(deploys, config, verdicts)


def test_a_none_on_the_metrics_isnt_the_history_s_silence():
    evidence = [item(kind="none", source="m1", column="err%", start="22:20:00", end="22:29:05", note="no errors"), *EVIDENCE[1:]]
    checks = [ItemCheck("e1", "none", "m1", Verdict.HOLDS, column="err%"), *CHECKS[1:]]
    assert not score(answer(evidence=evidence), checks).items["evidence_change"]


@pytest.mark.parametrize(
    "check, failure, health",
    [
        (ItemCheck("e2", "count", "l1", Verdict.HOLDS, column="error_type", entries=[Entry("l1[1]", ROW, timedelta(0), {"error_type": "UpstreamTimeout"})]), True, False),
        (ItemCheck("e2", "value", "m1", Verdict.HOLDS, column="payments_timeouts", entries=[row("payments_timeouts", 2.0)]), True, False),
        (ItemCheck("e2", "value", "m1", Verdict.HOLDS, column="db_query_p50_ms", entries=[row("db_query_p50_ms", 301.0)]), False, True),
        (ItemCheck("e2", "value", "m1", Verdict.HOLDS, column="err%", entries=[row("err%", 0.0)]), False, True),
        (ItemCheck("e2", "value", "m1", Verdict.CONTRADICTED, column="payments_p99_ms", entries=[row("payments_p99_ms", 2000.0)]), False, False),
    ],
)
def test_the_failure_and_health_items_read_what_holds(check, failure, health):
    checks = [CHECKS[0], check, CHECKS[2]]
    items = score(RIGHT, checks).items
    assert (items["evidence_failure"], items["evidence_health"]) == (failure, health)


def test_the_faults_inside_this_service_keep_their_rules():
    # Both in this service; only the bad deploy's fix is code, so only it opens a PR (Phase 4, decision 10).
    for fault, expect_pr in ((Fault.BAD_DEPLOY, True), (Fault.DB_POOL_EXHAUSTION, False)):
        rules = rubrics.RULES[fault]
        assert (rules.location, rules.expect_pr, rules.no_culprit_record, rules.health_column) == (
            "this_service", expect_pr, False, None
        )
