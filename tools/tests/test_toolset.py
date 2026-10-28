import inspect
from datetime import UTC, datetime
from typing import get_args, get_type_hints

from conftest import Stubbed

from sentinelops_tools import logs
from sentinelops_tools.toolset import Route, make_tools


def test_the_three_tools_the_model_gets() -> None:
    tools = make_tools(Stubbed(datetime(2026, 10, 28, tzinfo=UTC)).env)

    assert [t.__name__ for t in tools] == ["get_metrics", "get_recent_logs", "get_deploy_history"]
    for tool in tools:
        assert tool.__doc__ and "Args:" in tool.__doc__
        params = inspect.signature(tool).parameters
        assert "env" not in params, "the model must never see the AWS clients"
        documented = tool.__doc__.split("Args:")[1]
        for name in params:
            assert f"{name}:" in documented, f"{tool.__name__}({name}) is undocumented"


def test_the_route_choices_are_the_routes_the_tool_accepts() -> None:
    assert get_args(Route) == logs.ROUTES
    tools = {t.__name__: t for t in make_tools(Stubbed(datetime(2026, 10, 28, tzinfo=UTC)).env)}
    assert get_type_hints(tools["get_recent_logs"])["route"] == Route | None


def test_each_tool_passes_its_arguments_through(recorded) -> None:
    with recorded("history_pool") as (env, _):
        get_deploy_history = make_tools(env)[2]
        assert get_deploy_history(limit=4).startswith("The 4 most recent deploys")
