"""Each fixture's recorded text is exactly what its tool returns: what the model reads.

A change to a tool's output fails here until the fixture's text is refreshed,
so a reworded header or a moved column is always a deliberate change.
"""

import json

import pytest
from conftest import FIXTURES

from sentinelops_tools.deploys import history
from sentinelops_tools.logs import recent_lines
from sentinelops_tools.metrics import metrics_table

CASES = [
    ("metrics_bad_deploy", metrics_table, {}),
    ("metrics_pool", metrics_table, {}),
    ("logs_bad_deploy_errors", recent_lines, {}),
    ("logs_pool_errors", recent_lines, {}),
    ("history_bad_deploy", history, {}),
    ("history_pool", history, {"limit": 4}),
]


@pytest.mark.parametrize(("name", "tool", "kwargs"), CASES, ids=[case[0] for case in CASES])
def test_the_recorded_text_is_what_the_tool_returns(recorded, name, tool, kwargs) -> None:
    with recorded(name) as (env, fixture):
        text = tool(env, **kwargs)

    assert text == fixture["text"]


def test_every_recorded_text_is_checked() -> None:
    with_text = {
        path.stem
        for path in FIXTURES.glob("*.json")
        if "text" in json.loads(path.read_text(encoding="utf-8"))
    }
    assert with_text == {case[0] for case in CASES}
