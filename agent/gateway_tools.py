"""The tools Gateway lists: the model's tool specs, as Gateway's schema can state them.

infra/gateway_tools.json holds each tool's name, description, and parameters
with their types and descriptions. Gateway's schema has no place for enums or
defaults, so those stay with the specs the model sees, and the tools check them
themselves. Each tool also takes as_of, which the agent sets for a replay and
the model never sees. Terraform builds the Gateway targets from the file, and
tests/test_gateway_tools.py fails until it matches the specs again:

    uv run python -m gateway_tools
"""

import json
import sys
from pathlib import Path
from typing import Any

from guards import RunGuard, guarded_tool
from sentinelops_tools.env import ToolEnv
from sentinelops_tools.handlers import AS_OF, AS_OF_DESCRIPTION
from sentinelops_tools.toolset import make_tools

GATEWAY_TOOLS = Path(__file__).resolve().parents[1] / "infra" / "gateway_tools.json"


def gateway_tools() -> list[dict[str, Any]]:
    """The specs Strands sends the model, built as the runner builds them, in Gateway's terms."""
    from strands import tool

    guard = RunGuard()
    env = ToolEnv(logs=None, cloudwatch=None, dynamodb=None)
    listed = []
    for fn in make_tools(env):
        spec = tool(guarded_tool(guard, fn)).tool_spec
        schema = spec["inputSchema"]["json"]
        properties = {
            name: {"type": p["type"], "description": p["description"]}
            for name, p in schema["properties"].items()
        }
        properties[AS_OF] = {"type": "string", "description": AS_OF_DESCRIPTION}
        listed.append(
            {
                "name": spec["name"],
                "description": spec["description"],
                "inputSchema": {
                    "type": "object",
                    "properties": properties,
                    "required": schema.get("required", []),
                },
            }
        )
    return listed


if __name__ == "__main__":
    GATEWAY_TOOLS.write_text(json.dumps(gateway_tools(), indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {GATEWAY_TOOLS}", file=sys.stderr)
