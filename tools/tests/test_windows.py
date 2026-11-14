"""The recorded windows, replayed on the recording alone.

record_window.py called every tool on a grid of arguments against the live
stack and against the recording, and saved the window only when every call
matched. Here the same calls run on the recording alone, so a change to a fake,
a tool, or the data fails until the window's checks are refreshed on purpose.
"""

import gzip
import hashlib
import json
import re
from datetime import timedelta

import pytest
from window import PLACEHOLDER_ACCOUNT, WINDOWS, OutsideWindow, Window, _pattern, parse_time

from sentinelops_tools.deploys import history
from sentinelops_tools.logs import filter_pattern, recent_lines
from sentinelops_tools.metrics import metrics_table

TOOLS = {"get_metrics": metrics_table, "get_recent_logs": recent_lines, "get_deploy_history": history}
NAMES = sorted(
    p.name for p in WINDOWS.iterdir() if (p / "window.json").exists() and not p.name.endswith(".new")
)
ACCOUNT_IN_ARN = re.compile(r"arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:(\d{12}):")


@pytest.fixture(scope="module", params=NAMES)
def window(request: pytest.FixtureRequest) -> Window:
    return Window(request.param)


def test_every_check_returns_what_the_live_stack_did(window: Window) -> None:
    differs = []
    for check in window.meta["checks"]:
        text = TOOLS[check["tool"]](window.env(parse_time(check["now"])), **check["args"])
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != check["sha256"]:
            differs.append(f"{check['tool']} {check['args']} at {check['now']}: {text.splitlines()[0]}")

    assert window.meta["checks"]
    assert differs == []


def test_each_replay_starts_from_its_alarm_going_into_alarm(window: Window) -> None:
    cloudwatch = window.client("cloudwatch")
    for replay in window.replays:
        # What the agent's AlarmEvent.as_of asks for.
        (change,) = cloudwatch.describe_alarm_history(
            AlarmName=replay.alarm,
            HistoryItemType="StateUpdate",
            StartDate=replay.fired_at - timedelta(days=1),
            EndDate=replay.fired_at,
            ScanBy="TimestampDescending",
            MaxRecords=1,
        )["AlarmHistoryItems"]
        (alarm,) = cloudwatch.describe_alarms(AlarmNames=[replay.alarm], AlarmTypes=["MetricAlarm"])[
            "MetricAlarms"
        ]

        assert change["Timestamp"] == replay.fired_at
        assert json.loads(change["HistoryData"])["newState"]["stateValue"] == "ALARM"
        assert alarm["Threshold"] > 0


def test_no_account_id_but_the_placeholder(window: Window) -> None:
    folder = WINDOWS / window.name
    text = (folder / "window.json").read_text(encoding="utf-8")
    text += gzip.decompress((folder / window.meta["logs"]["file"]).read_bytes()).decode("utf-8")

    assert set(ACCOUNT_IN_ARN.findall(text)) <= {PLACEHOLDER_ACCOUNT}


def test_a_read_past_the_recording_fails_loudly(window: Window) -> None:
    env = window.env(window.replays[-1].fired_at + timedelta(minutes=5))

    for tool in TOOLS.values():
        with pytest.raises(OutsideWindow):
            tool(env)


def test_the_error_filter_matches_any_5xx_whatever_its_error_field() -> None:
    """No recorded window holds a 504 yet, so the matcher is held to it here:
    an upstream timeout's line has error 0 and is still a failed request."""
    matches = _pattern(filter_pattern("checkout-api", errors_only=True, route=None), "checkout-api")
    line = {"service": "checkout-api", "route": "POST /checkout", "status": 504, "error": 0}
    assert matches(line)
    assert matches({**line, "status": 500, "error": 1})
    assert not matches({**line, "status": 404})
    assert not matches({**line, "status": 200})
    assert not matches({**line, "status": True})
    assert not matches({"service": "checkout-api"})
    assert not matches({**line, "service": "payments-api"})
    assert not matches(None)
    on_route = _pattern(filter_pattern("checkout-api", errors_only=True, route="POST /cart/items"), "checkout-api")
    assert not on_route(line) and on_route({**line, "route": "POST /cart/items"})


@pytest.mark.parametrize(
    "pattern",
    [
        '{ $.service = "checkout-api" && $.status >= "500" }',
        '{ $.service = "checkout-api" && $.status > 500 }',
        "{ $.service >= 1 }",
    ],
)
def test_a_pattern_the_log_tool_doesnt_build_fails_loudly(pattern: str) -> None:
    with pytest.raises(OutsideWindow):
        _pattern(pattern, "checkout-api")
