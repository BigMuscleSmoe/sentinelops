"""Gateway lists the tools the model sees.

A change to a tool's name, parameters, or docstring fails here until
infra/gateway_tools.json is rewritten with `uv run python -m gateway_tools`.
"""

import json

from gateway_tools import GATEWAY_TOOLS, gateway_tools


def test_gateway_lists_the_tools_the_model_sees() -> None:
    assert json.loads(GATEWAY_TOOLS.read_text(encoding="utf-8")) == gateway_tools()


def test_every_parameter_has_a_type_gateway_can_check() -> None:
    for listed in gateway_tools():
        for name, schema in listed["inputSchema"]["properties"].items():
            assert schema["type"] in {"string", "integer", "number", "boolean"}, (listed["name"], name)
