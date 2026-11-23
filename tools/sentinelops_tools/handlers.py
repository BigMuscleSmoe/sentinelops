"""Each tool as a Lambda function behind AgentCore Gateway: one thin handler per tool.

Gateway invokes a tool's function with the tool's arguments as the event, and
the tool's name, `<target>___<tool>`, in the client context. A handler runs only
its own tool, the same function the agent calls when it imports the tools
directly, and answers {"text": ...}. Gateway passes that JSON to the caller as
the tool's result.

`as_of` is for replays: the investigation's now, so every read ends there. The
agent sets it, never the model. A time past the function's own clock reads as
of that clock, so no caller can see a record dated in the future, such as the
rollback a break schedules, before its time.
"""

import dataclasses
import inspect
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sentinelops_tools import repo
from sentinelops_tools.env import KNOWLEDGE_BASE_ENV, ToolEnv, utc_now
from sentinelops_tools.toolset import make_tools

AS_OF = "as_of"
AS_OF_DESCRIPTION = (
    "Replays only: the investigation's now, in ISO 8601 with a time zone. Every read ends "
    "there. Left out, reads end at the function's own clock, which also caps a later time."
)
SEPARATOR = "___"
# Where the function's zip unpacks: the package's parent.
ZIP_ROOT = Path(__file__).resolve().parent.parent

_clients: ToolEnv | None = None


def get_metrics(event: dict[str, Any], context: Any) -> dict[str, str]:
    return handle("get_metrics", event, context)


def get_recent_logs(event: dict[str, Any], context: Any) -> dict[str, str]:
    return handle("get_recent_logs", event, context)


def get_deploy_history(event: dict[str, Any], context: Any) -> dict[str, str]:
    return handle("get_deploy_history", event, context)


def search_runbooks(event: dict[str, Any], context: Any) -> dict[str, str]:
    return handle("search_runbooks", event, context)


def read_repo_file(event: dict[str, Any], context: Any) -> dict[str, str]:
    return handle("read_repo_file", event, context)


def handle(
    tool: str,
    event: dict[str, Any],
    context: Any,
    *,
    env: ToolEnv | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> dict[str, str]:
    """Run `tool` on the event's arguments. `env` and `clock` are for tests."""
    called = called_tool(context)
    if called is not None and called != tool:
        # A target pointed at the wrong function: a deploy error, not the model's.
        raise LookupError(f"This function runs {tool}, not {called}.")
    now = _now(event.get(AS_OF), clock)
    env = dataclasses.replace(env or _clients_once(), now=now)
    fn = {f.__name__: f for f in make_tools(env)}[tool]

    args = {k: v for k, v in event.items() if k != AS_OF}
    unknown = sorted(set(args) - set(inspect.signature(fn).parameters))
    if unknown:
        return {"text": f"{tool} has no argument {', '.join(unknown)}."}
    return {"text": fn(**args)}


def called_tool(context: Any) -> str | None:
    """The tool Gateway called, without its target's prefix. None when invoked directly."""
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    name = custom.get("bedrockAgentCoreToolName")
    if name is None:
        return None
    return name.split(SEPARATOR, 1)[1] if SEPARATOR in name else name


def _now(as_of: Any, clock: Callable[[], datetime]) -> Callable[[], datetime]:
    if as_of is None:
        return clock
    moment = datetime.fromisoformat(as_of)
    if moment.tzinfo is None:
        raise ValueError(f"{AS_OF} needs a time zone: {as_of}")
    pinned = min(moment.astimezone(UTC), clock())
    return lambda: pinned


def _clients_once() -> ToolEnv:
    """The AWS clients, made once per container and kept for its warm invocations."""
    global _clients
    if _clients is None:
        import boto3

        # Only the search tool's function has a knowledge base set, and only the
        # read tool's a code snapshot: a file at the zip's root, named in its environment.
        snapshot = os.environ.get(repo.CODE_SNAPSHOT_ENV)
        _clients = ToolEnv.from_session(
            boto3.Session(),
            knowledge_base_id=os.environ.get(KNOWLEDGE_BASE_ENV),
            code=repo.load(ZIP_ROOT / snapshot) if snapshot else None,
        )
    return _clients
