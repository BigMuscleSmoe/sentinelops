"""The Lambda handlers: each tool's own text, as of the caller's clock, and nothing more.

They run on the live test's recorded window, so each handler's answer can be
compared with what the tool the agent imports returns on the same data.
"""

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from window import Window

from sentinelops_tools import handlers
from sentinelops_tools.toolset import make_tools

WINDOW = Window("2026-10-29-live")
FIRED = WINDOW.replays[0].fired_at
LATER = WINDOW.recorded_at
CASES = [
    ("get_metrics", {"minutes": 30, "period_seconds": 60}),
    ("get_recent_logs", {"minutes": 15, "errors_only": True, "route": "POST /checkout", "limit": 30}),
    ("get_deploy_history", {"limit": 4}),
]


def gateway(tool: str) -> SimpleNamespace:
    """The context Gateway passes: the tool's name behind its target's."""
    custom = {"bedrockAgentCoreToolName": f"target___{tool}", "bedrockAgentCoreGatewayId": "gw"}
    return SimpleNamespace(client_context=SimpleNamespace(custom=custom))


def imported(tool: str, **args: Any) -> str:
    """What the tool the agent imports returns, as of the alarm."""
    return {f.__name__: f for f in make_tools(WINDOW.env(FIRED))}[tool](**args)


def handle(tool: str, event: dict[str, Any], context: Any = None, clock: Any = lambda: LATER) -> Any:
    return handlers.handle(tool, event, context, env=WINDOW.env(LATER), clock=clock)


@pytest.mark.parametrize(("tool", "args"), CASES, ids=[case[0] for case in CASES])
def test_a_replay_answers_with_the_tool_text_as_of_the_alarm(tool: str, args: dict[str, Any]) -> None:
    answer = handle(tool, {**args, "as_of": FIRED.isoformat()}, gateway(tool))

    assert answer == {"text": imported(tool, **args)}


def test_without_as_of_reads_end_at_the_function_clock() -> None:
    answer = handle("get_deploy_history", {}, gateway("get_deploy_history"), clock=lambda: FIRED)

    assert answer == {"text": imported("get_deploy_history")}


def test_an_as_of_past_the_clock_reads_as_of_the_clock() -> None:
    # Unclamped, a day later would read past the recording, and live, a scheduled rollback.
    event = {"as_of": (FIRED + timedelta(days=1)).isoformat()}

    answer = handle("get_deploy_history", event, gateway("get_deploy_history"), clock=lambda: FIRED)

    assert answer == {"text": imported("get_deploy_history")}


def test_an_as_of_without_a_time_zone_is_refused() -> None:
    with pytest.raises(ValueError, match="time zone"):
        handle("get_metrics", {"as_of": "2026-10-29T01:56:07"}, gateway("get_metrics"))


def test_a_tool_called_by_another_name_is_refused() -> None:
    # A target pointed at the wrong function: fail, rather than answer for another tool.
    with pytest.raises(LookupError, match="runs get_metrics, not get_recent_logs"):
        handle("get_metrics", {}, gateway("get_recent_logs"))


def test_invoked_directly_it_runs_its_own_tool() -> None:
    answer = handle("get_deploy_history", {"as_of": FIRED.isoformat()}, context=None)

    assert answer == {"text": imported("get_deploy_history")}


def test_an_unknown_argument_is_named_and_nothing_runs() -> None:
    answer = handle("get_metrics", {"minutes": 10, "log_group": "/aws/lambda/other"}, gateway("get_metrics"))

    assert answer == {"text": "get_metrics has no argument log_group."}


@pytest.mark.parametrize("tool", [case[0] for case in CASES])
def test_each_entry_point_runs_its_own_tool(tool: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers, "handle", lambda name, event, context: {"text": name})

    assert getattr(handlers, tool)({}, None) == {"text": tool}
