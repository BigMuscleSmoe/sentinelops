"""A stand-in for the AgentCore Gateway, answering as the live one did on 2026-10-30.

JSON answers and no session ID; 202 for a notification; the handshake settled
at 2025-03-26; the tools listed as <target>___<tool> from infra/gateway_tools.json;
and each tools/call run by the tool's own Lambda handler, its answer passed back
as compact JSON in one text block. A handler that raises gets Gateway's generic
error result. No network: it's an httpx2.MockTransport.
"""

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2

from sentinelops_tools import handlers
from sentinelops_tools.env import ToolEnv

GATEWAY_TOOLS = Path(__file__).resolve().parents[2] / "infra" / "gateway_tools.json"
# The targets as infra/agent_tools.tf names them.
TARGETS = {"get_metrics": "metrics", "get_recent_logs": "logs", "get_deploy_history": "deploys"}
URL = "https://sentinelops-tools-0123456789.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
INITIALIZED = {
    "protocolVersion": "2025-03-26",
    "capabilities": {
        "tools": {"listChanged": False},
        "prompts": {"listChanged": False},
        "resources": {"listChanged": False},
        "extensions": {"io.modelcontextprotocol/tasks": {}},
    },
    "serverInfo": {"version": "1.0.0", "name": "sentinelops-tools"},
}
INTERNAL_ERROR = "An internal error occurred. Please retry later."

Answer = Callable[[str, dict[str, Any]], dict[str, Any]]


def text_result(text: str, *, error: bool = False) -> dict[str, Any]:
    return {"isError": error, "content": [{"type": "text", "text": text}]}


class FakeGateway:
    """Runs each call on `env` through the real handler, or answers with `answer(name, arguments)`."""

    def __init__(self, env: ToolEnv | None = None, *, answer: Answer | None = None) -> None:
        self.env = env
        self.answer = answer
        self.requests: list[httpx2.Request] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def methods(self) -> list[str]:
        return [json.loads(r.content)["method"] for r in self.requests]

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        message = json.loads(request.content)
        if "id" not in message:
            return httpx2.Response(202)
        match message["method"]:
            case "initialize":
                result = INITIALIZED
            case "tools/list":
                result = {"tools": [self._listed(t) for t in json.loads(GATEWAY_TOOLS.read_text(encoding="utf-8"))]}
            case "tools/call":
                name, arguments = message["params"]["name"], message["params"].get("arguments") or {}
                self.calls.append((name, arguments))
                result = self.answer(name, arguments) if self.answer else self._run(name, arguments)
            case method:
                raise AssertionError(f"the gateway was sent {method}")
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})

    def _run(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = name.split(handlers.SEPARATOR, 1)[1]
        context = SimpleNamespace(client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": name}))
        try:
            answer = handlers.handle(tool, dict(arguments), context, env=self.env)
        except Exception:
            return text_result(INTERNAL_ERROR, error=True)
        return text_result(json.dumps(answer, separators=(",", ":")))

    @staticmethod
    def _listed(tool: dict[str, Any]) -> dict[str, Any]:
        schema = {k: v for k, v in tool["inputSchema"].items() if v != []}  # live leaves out an empty required
        return {**tool, "name": f"{TARGETS[tool['name']]}{handlers.SEPARATOR}{tool['name']}", "inputSchema": schema}
