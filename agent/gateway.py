"""The agent's tools, run by the AgentCore Gateway instead of in this process.

Each tool keeps the name, parameters, and description the model knows: the
wrappers take them from the local tools, so the model sees the same specs
either way, and only the bodies differ. A body asks Gateway to run its tool,
signed with SigV4, and returns the text the tool's Lambda handler answered.

Every call opens an MCP session of its own. Gateway keeps no state between
calls, and a session's task group fails as a whole: a dropped connection in a
session shared by the whole run would cancel the run's own task. The HTTP
client is shared, so its connection stays warm between calls.

A failure reaches the model as one line naming its kind, never its message,
which can name an ARN, and with it the account ID. The message goes to the log.
"""

import functools
import inspect
import json
import logging
from collections.abc import Awaitable, Callable, Generator
from datetime import datetime
from typing import Any

import httpx2
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from sentinelops_tools.env import ToolEnv, iso_ms
from sentinelops_tools.handlers import AS_OF, SEPARATOR
from sentinelops_tools.toolset import make_tools

logger = logging.getLogger("sentinelops.gateway")

SERVICE = "bedrock-agentcore"
# A tool's Lambda times out within 60 s; a call still open past this has failed.
READ_TIMEOUT_S = 75.0
CONNECT_TIMEOUT_S = 5.0
MAX_DETAIL_CHARS = 300
# Clients the wrappers never call: make_tools needs them only to exist.
SPECS_ONLY = ToolEnv(logs=None, cloudwatch=None, dynamodb=None)


class SigV4(httpx2.Auth):
    """Signs each request with the caller's AWS credentials, for AgentCore.

    Only the host, the date, a session token, and the body's hash are signed,
    so headers the MCP client adds can't break the signature.
    """

    requires_request_body = True

    def __init__(self, credentials: Any, region: str) -> None:
        self._credentials = credentials
        self._region = region

    def auth_flow(self, request: httpx2.Request) -> Generator[httpx2.Request, httpx2.Response, None]:
        signed = AWSRequest(method=request.method, url=str(request.url), data=request.content)
        # Frozen per request: a role's credentials can refresh between calls.
        SigV4Auth(self._credentials.get_frozen_credentials(), SERVICE, self._region).add_auth(signed)
        for name in ("Authorization", "X-Amz-Date", "X-Amz-Security-Token"):
            if name in signed.headers:
                request.headers[name] = signed.headers[name]
        yield request


class Gateway:
    """The tools gateway at `url`, signed for with `credentials` (botocore's)."""

    def __init__(self, url: str, credentials: Any, region: str, *, transport: Any = None) -> None:
        if credentials is None:
            raise ValueError("No AWS credentials to sign the gateway's requests with.")
        self.url = url
        self._auth = SigV4(credentials, region)
        self._transport = transport  # for tests
        self._http: httpx2.AsyncClient | None = None

    def tools(self, *, as_of: datetime | None = None) -> list[Callable[..., Awaitable[str]]]:
        """The agent's three tools, run by Gateway. For a replay, `as_of` ends every read there."""
        stamp = None if as_of is None else iso_ms(as_of)
        return [self._remote(local, stamp) for local in make_tools(SPECS_ONLY)]

    async def call(self, tool: str, arguments: dict[str, Any]) -> str:
        """Run one tool in a session of its own and return its text, or a line saying why not."""
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        try:
            async with (
                streamable_http_client(self.url, http_client=self._client()) as (read, write),
                ClientSession(read, write, read_timeout_seconds=READ_TIMEOUT_S) as session,
            ):
                await session.initialize()
                # The session lists the tools before its first call either way,
                # to check the result against an output schema; the listing also
                # gives the tool's name with its target's prefix.
                listed = (await session.list_tools()).tools
                result = await session.call_tool(_listed_name(listed, tool), arguments)
        except Exception as e:
            return _failed(tool, _kind(e), repr(e))
        if result.is_error:
            return _failed(tool, "an error result", _texts(result))
        text = _tool_text(result)
        if text is None:
            return _failed(tool, "an unreadable result", _texts(result))
        return text

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _client(self) -> httpx2.AsyncClient:
        if self._http is None:
            self._http = httpx2.AsyncClient(
                auth=self._auth,
                timeout=httpx2.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
                transport=self._transport,
            )
        return self._http

    def _remote(self, local: Callable[..., str], as_of: str | None) -> Callable[..., Awaitable[str]]:
        name = local.__name__
        signature = inspect.signature(local)

        @functools.wraps(local)
        async def remote(*args: Any, **kwargs: Any) -> str:
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            # Gateway's schema has no null, so a None is left out, and the
            # handler's tool takes its own default, which is None.
            arguments = {k: v for k, v in bound.arguments.items() if v is not None}
            if as_of is not None:
                arguments[AS_OF] = as_of
            return await self.call(name, arguments)

        return remote


def _listed_name(listed: list[Any], tool: str) -> str:
    names = [t.name for t in listed if t.name == tool or t.name.endswith(SEPARATOR + tool)]
    if len(names) != 1:
        raise LookupError(f"the gateway lists {len(names)} tools named {tool}")
    return names[0]


def _tool_text(result: Any) -> str | None:
    """The handler's {"text": ...}, which Gateway passes back as a single text block."""
    blocks = [b for b in result.content if getattr(b, "type", None) == "text"]
    if len(blocks) != 1:
        return None
    try:
        answer = json.loads(blocks[0].text)
    except json.JSONDecodeError:
        return None
    text = answer.get("text") if isinstance(answer, dict) else None
    return text if isinstance(text, str) else None


def _texts(result: Any) -> str:
    return " ".join(getattr(b, "text", "") for b in result.content)


def _kind(error: BaseException) -> str:
    """The first exception's type, through any task groups' exception groups."""
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return type(error).__name__


def _failed(tool: str, kind: str, detail: str) -> str:
    logger.warning(
        json.dumps({"event": "gateway_failed", "tool": tool, "kind": kind, "detail": detail[:MAX_DETAIL_CHARS]})
    )
    return f"Could not run {tool} through the gateway: {kind}."
