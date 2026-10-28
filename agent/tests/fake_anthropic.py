"""A scripted Messages API, so the agent loop runs in tests with no network.

Each request gets the next scripted reply, streamed as server-sent events the
way the API streams them. Every request is kept, headers and body, to assert on.

Usage defaults to the worst case the byte bound allows for: one input token
per byte of the request, and one output token per byte of the reply.

count_tokens requests are kept apart, in count_requests, and each gets the next
scripted count: a number, a response such as a 500, or an exception for the
transport to raise. Unscripted, the count is what the next reply will bill if
it says, or else the count request's own bytes: a few bytes under what the
reply bills, since the count request has no max_tokens or stream.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

Count = int | httpx.Response | Exception | None


@dataclass
class Reply:
    blocks: list[dict[str, Any]]
    stop_reason: str = "tool_use"
    input_tokens: int | None = None
    output_tokens: int | None = None


def thinking(text: str) -> dict[str, Any]:
    return {"type": "thinking", "thinking": text}


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def tool_use(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"type": "tool_use", "name": name, "input": arguments or {}}


def answer(hypothesis: dict[str, Any]) -> Reply:
    """The model calling the structured-output tool."""
    return Reply([tool_use("Hypothesis", hypothesis)])


class FakeAnthropic:
    def __init__(
        self, replies: list[Reply | Callable[[dict[str, Any]], Reply]], counts: list[Count] | None = None
    ) -> None:
        self.replies = list(replies)
        self.counts = list(counts or [])
        self.requests: list[dict[str, Any]] = []
        self.count_requests: list[dict[str, Any]] = []

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        kept = {
            "path": request.url.path,
            "headers": dict(request.headers),
            "body": body,
            "bytes": len(request.content),
            "timeout": request.extensions.get("timeout"),
        }
        if request.url.path == "/v1/messages/count_tokens":
            self.count_requests.append(kept)
            return self._count(len(request.content))
        self.requests.append(kept)
        if not self.replies:
            # A 400, not a 500: the SDK would retry a 500, slowly.
            return httpx.Response(
                400,
                json={"type": "error", "error": {"type": "invalid_request_error", "message": "script ran out"}},
            )
        reply = self.replies.pop(0)
        if callable(reply):
            reply = reply(body)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_stream(reply, len(request.content), len(self.requests)),
        )

    def _count(self, request_bytes: int) -> httpx.Response:
        count = self.counts.pop(0) if self.counts else None
        if isinstance(count, Exception):
            raise count
        if isinstance(count, httpx.Response):
            return count
        if count is None:
            upcoming = self.replies[0] if self.replies else None
            billed = upcoming.input_tokens if isinstance(upcoming, Reply) else None
            count = request_bytes if billed is None else billed
        return httpx.Response(200, json={"input_tokens": count})

    def tool_results(self, request_index: int) -> list[str]:
        """The text of every tool result sent in one request."""
        results = []
        for message in self.requests[request_index]["body"]["messages"]:
            if message["role"] != "user" or isinstance(message["content"], str):
                continue
            for block in message["content"]:
                if block.get("type") == "tool_result":
                    content = block["content"]
                    if isinstance(content, list):
                        content = "".join(c.get("text", "") for c in content)
                    results.append(content)
        return results


def _stream(reply: Reply, request_bytes: int, n: int) -> bytes:
    input_tokens = request_bytes if reply.input_tokens is None else reply.input_tokens
    output_tokens = (
        len(json.dumps(reply.blocks)) if reply.output_tokens is None else reply.output_tokens
    )
    events: list[tuple[str, dict[str, Any]]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": f"msg_{n}",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-haiku-5-5",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": input_tokens,
                        "output_tokens": 1,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
            },
        )
    ]
    for i, block in enumerate(reply.blocks):
        if block["type"] == "thinking":
            start = {"type": "thinking", "thinking": "", "signature": ""}
            deltas = [
                {"type": "thinking_delta", "thinking": block["thinking"]},
                {"type": "signature_delta", "signature": f"sig-{n}-{i}"},
            ]
        elif block["type"] == "text":
            start = {"type": "text", "text": ""}
            deltas = [{"type": "text_delta", "text": block["text"]}]
        else:
            start = {"type": "tool_use", "id": f"toolu_{n}_{i}", "name": block["name"], "input": {}}
            deltas = [{"type": "input_json_delta", "partial_json": json.dumps(block["input"])}]
        events.append(("content_block_start", {"type": "content_block_start", "index": i, "content_block": start}))
        events += [("content_block_delta", {"type": "content_block_delta", "index": i, "delta": d}) for d in deltas]
        events.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
    events.append(
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": reply.stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": output_tokens},
            },
        )
    )
    events.append(("message_stop", {"type": "message_stop"}))
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()
