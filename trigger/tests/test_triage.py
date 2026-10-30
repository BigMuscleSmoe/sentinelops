"""Triage on the alarm changes recorded on 2026-10-27 and in the live test, and on its edges."""

import json
from datetime import timedelta

from recorded_alarms import ERRORS, LATENCY, LIVE, OCT_27, POOL, at
from triage import (
    ALARM,
    MAX_OPEN,
    OK,
    QUIET,
    Change,
    ServiceState,
    Verdict,
    classify,
    incident_id,
    service_for,
)

SERVICE = "checkout-api"
NEW, DUPLICATE, FLAPPING, SEEN = Verdict.NEW, Verdict.DUPLICATE, Verdict.FLAPPING, Verdict.SEEN


def run(changes: list[Change], state: ServiceState | None = None) -> list:
    state = state or ServiceState()
    results = []
    for change in changes:
        result = classify(state, change, SERVICE)
        results.append(result)
        state = result.state
    return results


def test_the_pool_faults_later_alarms_join_its_incident() -> None:
    results = run(OCT_27[:6])

    assert [r.verdict for r in results] == [NEW, DUPLICATE, DUPLICATE, Verdict.OK, Verdict.OK, Verdict.OK]
    assert {r.incident.id for r in results} == {"checkout-api-20261027T185646Z"}
    assert results[-1].state.incident.alarmed == (POOL, LATENCY, ERRORS)
    # Only once the last alarm clears is the service quiet.
    assert [r.state.quiet_since for r in results[3:]] == [None, None, at("2026-10-27T19:02:47.091")]


def test_a_break_3_minutes_after_the_last_joins_its_incident_and_starts_nothing() -> None:
    results = run(OCT_27)

    assert [r.verdict for r in results][6:] == [FLAPPING, Verdict.OK, FLAPPING, Verdict.OK]
    assert [r.verdict for r in results].count(NEW) == 1
    assert {r.incident.id for r in results} == {"checkout-api-20261027T185646Z"}


def test_the_same_break_after_10_quiet_minutes_opens_its_own_incident() -> None:
    state = run(OCT_27[:6])[-1].state
    quiet_since = at("2026-10-27T19:02:47.091")

    just_before = classify(state, Change(ERRORS, ALARM, quiet_since + QUIET - timedelta(milliseconds=1)), SERVICE)
    on_time = classify(state, Change(ERRORS, ALARM, quiet_since + QUIET), SERVICE)

    assert just_before.verdict is FLAPPING
    assert on_time.verdict is NEW
    assert on_time.incident.id == "checkout-api-20261027T191247Z"
    assert on_time.incident.alarmed == (ERRORS,)
    assert on_time.state.quiet_since is None


def test_the_live_tests_refire_is_flapping() -> None:
    results = run(LIVE)

    assert [r.verdict for r in results] == [NEW, Verdict.OK, FLAPPING, Verdict.OK]
    assert results[-1].state.quiet_since == at("2026-10-29T02:03:17.025")


def test_an_incident_closes_60_minutes_after_it_opened_even_while_alarms_keep_firing() -> None:
    opened = at("2026-10-30T10:00:00.000")
    changes = [Change(POOL, ALARM, opened)]
    # The errors alarm flaps every 5 minutes, never 10 quiet minutes in a row.
    for minute in range(5, 60, 5):
        changes.append(Change(ERRORS, ALARM, opened + timedelta(minutes=minute)))
        changes.append(Change(ERRORS, OK, opened + timedelta(minutes=minute, seconds=30)))
    state = run(changes)[-1].state

    assert classify(state, Change(LATENCY, ALARM, opened + MAX_OPEN - timedelta(milliseconds=1)), SERVICE).verdict is DUPLICATE
    later = classify(state, Change(LATENCY, ALARM, opened + MAX_OPEN), SERVICE)
    assert later.verdict is NEW
    assert later.incident.id == "checkout-api-20261030T110000Z"


def test_an_alarm_still_firing_keeps_its_incident_open() -> None:
    opened = at("2026-10-30T10:00:00.000")
    results = run(
        [
            Change(POOL, ALARM, opened),
            Change(LATENCY, ALARM, opened + timedelta(minutes=1)),
            Change(LATENCY, OK, opened + timedelta(minutes=2)),
            # Half an hour on, the pool-wait alarm has never cleared.
            Change(ERRORS, ALARM, opened + timedelta(minutes=30)),
        ]
    )

    assert [r.verdict for r in results] == [NEW, DUPLICATE, Verdict.OK, DUPLICATE]
    assert results[2].state.quiet_since is None


def test_a_change_seen_before_or_older_than_its_alarms_latest_is_ignored() -> None:
    state = run(OCT_27[:3])[-1].state

    again = classify(state, OCT_27[2], SERVICE)
    older = classify(state, Change(LATENCY, OK, at("2026-10-27T19:00:00.000")), SERVICE)

    assert (again.verdict, again.state, again.incident) == (SEEN, state, None)
    assert (older.verdict, older.state) == (SEEN, state)


def test_ok_with_no_incident_open_records_the_quiet() -> None:
    result = classify(ServiceState(), Change(ERRORS, OK, at("2026-10-30T10:00:00.000")), SERVICE)

    assert (result.verdict, result.incident) == (Verdict.OK, None)
    assert result.state.quiet_since == at("2026-10-30T10:00:00.000")


def test_classify_leaves_the_state_it_was_given_alone() -> None:
    state = run(OCT_27[:2])[-1].state
    before = state.to_dict()

    classify(state, OCT_27[2], SERVICE)

    assert state.to_dict() == before


def test_the_state_round_trips_through_json() -> None:
    for state in (ServiceState(), run(OCT_27)[-1].state, run(OCT_27[:4])[-1].state):
        assert ServiceState.from_dict(json.loads(json.dumps(state.to_dict()))) == state


def test_only_our_alarms_have_a_service() -> None:
    assert service_for(ERRORS) == "checkout-api"
    assert service_for("sentinelops-other-errors") is None
    assert service_for("x-sentinelops-checkout-errors") is None


def test_an_incidents_id_is_its_service_and_opening_second_in_utc() -> None:
    assert incident_id(SERVICE, at("2026-10-27T19:05:57.026")) == "checkout-api-20261027T190557Z"
