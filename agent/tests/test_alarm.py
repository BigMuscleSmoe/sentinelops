"""The alarm a run starts from: live, or replayed as of a moment, against recorded CloudWatch calls."""

import copy
import json
from datetime import UTC, datetime

import pytest

from alarm import AlarmEvent, AlarmNotFiring
from stubs import AGENT_FIXTURES, FakeSession, load

ERRORS = "sentinelops-checkout-errors"
DESCRIBE = {"AlarmNames": [ERRORS], "AlarmTypes": ["MetricAlarm"]}


def fixture(name: str) -> dict:
    return load(AGENT_FIXTURES / f"{name}.json")


def described(state: str = "ALARM", **changes: object) -> dict:
    """The recorded errors alarm, as describe_alarms returned it, with changes."""
    response = copy.deepcopy(fixture("alarm_bad_deploy")["calls"][0]["response"])
    response["MetricAlarms"][0].update(StateValue=state, **changes)
    return response


def history(state: str, at: datetime) -> dict:
    data = {"newState": {"stateValue": state, "stateReason": f"went {state}"}}
    return {
        "AlarmHistoryItems": [
            {
                "AlarmName": ERRORS,
                "Timestamp": at,
                "HistoryItemType": "StateUpdate",
                "HistorySummary": f"Alarm updated to {state}",
                "HistoryData": json.dumps(data),
            }
        ]
    }


@pytest.mark.parametrize(
    ("name", "fired_at"),
    [
        ("alarm_bad_deploy", datetime(2026, 10, 27, 19, 5, 57, 26_000, tzinfo=UTC)),
        ("alarm_pool", datetime(2026, 10, 27, 18, 56, 46, 774_000, tzinfo=UTC)),
    ],
)
def test_a_replay_starts_from_the_last_change_into_alarm(name: str, fired_at: datetime) -> None:
    recorded = fixture(name)
    session = FakeSession()
    session.replay(recorded)
    alarm_name = recorded["calls"][0]["params"]["AlarmNames"][0]

    moment = datetime.fromisoformat(recorded["moment"])
    alarm = AlarmEvent.as_of(session.client("cloudwatch"), alarm_name, moment)

    session.assert_all_called()
    assert alarm.fired_at == fired_at
    assert alarm.prompt() == recorded["prompt"]


def test_the_condition_reads_like_the_alarm() -> None:
    session = FakeSession()
    session.replay(fixture("alarm_pool"))
    recorded = fixture("alarm_pool")

    moment = datetime.fromisoformat(recorded["moment"])
    alarm = AlarmEvent.as_of(session.client("cloudwatch"), "sentinelops-checkout-pool-wait", moment)

    assert alarm.condition == "pool_wait_time_ms p99 > 100.0 over 10 s periods, in 2 of 3 periods."


def test_a_live_run_starts_from_the_alarm_as_it_is() -> None:
    session = FakeSession()
    at = datetime(2026, 10, 28, 3, 0, 1, tzinfo=UTC)
    session.expect("describe_alarms", DESCRIBE, described(StateUpdatedTimestamp=at, StateReason="now"))

    alarm = AlarmEvent.live(session.client("cloudwatch"), ERRORS)

    assert (alarm.fired_at, alarm.reason) == (at, "now")


def test_a_live_run_refuses_an_alarm_that_is_not_firing() -> None:
    session = FakeSession()
    session.expect("describe_alarms", DESCRIBE, described("OK"))

    with pytest.raises(AlarmNotFiring, match=f"^{ERRORS} is OK, not ALARM.$"):
        AlarmEvent.live(session.client("cloudwatch"), ERRORS)


def test_an_unknown_alarm_is_refused() -> None:
    session = FakeSession()
    session.expect("describe_alarms", DESCRIBE, {"MetricAlarms": []})

    with pytest.raises(AlarmNotFiring, match="No metric alarm named"):
        AlarmEvent.live(session.client("cloudwatch"), ERRORS)


def test_a_replay_refuses_a_moment_when_the_alarm_was_ok() -> None:
    moment = datetime(2026, 10, 27, 19, 20, tzinfo=UTC)
    session = FakeSession()
    session.expect("describe_alarms", DESCRIBE, described("OK"))
    session.expect("describe_alarm_history", None, history("OK", datetime(2026, 10, 27, 19, 12, 17, tzinfo=UTC)))

    with pytest.raises(AlarmNotFiring, match="was OK at 2026-10-27T19:20:00.000Z, not ALARM"):
        AlarmEvent.as_of(session.client("cloudwatch"), ERRORS, moment)


def test_a_replay_refuses_a_day_with_no_changes() -> None:
    session = FakeSession()
    session.expect("describe_alarms", DESCRIBE, described("OK"))
    session.expect("describe_alarm_history", None, {"AlarmHistoryItems": []})

    with pytest.raises(AlarmNotFiring, match="didn't change state in the day before"):
        AlarmEvent.as_of(session.client("cloudwatch"), ERRORS, datetime(2026, 10, 20, tzinfo=UTC))


def test_long_alarm_text_is_cut() -> None:
    session = FakeSession()
    session.expect("describe_alarms", DESCRIBE, described(AlarmDescription="d" * 5_000, StateReason="r" * 5_000))

    prompt = AlarmEvent.live(session.client("cloudwatch"), ERRORS).prompt()

    assert len(prompt) < 2_600
    assert prompt.count(" ...[cut]") == 2
