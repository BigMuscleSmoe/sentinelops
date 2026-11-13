"""Which model answered: read off each Messages response as it streams past.

Strands keeps the model it asked for, not the one the API says answered, so
the sweep sends the API traffic through this transport, which notes the
`model` of every response's message_start and changes nothing it passes on.
"""

import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

_MODEL = re.compile(rb'"model"\s*:\s*"([^"]+)"')
# message_start is the first event; past this much, a response has no model to find.
_HEAD_BYTES = 64_000
_SNAPSHOT = re.compile(r"-\d{8}$")


def same_model(asked: str, answered: str) -> bool:
    """The model asked for, or a dated snapshot of it."""
    return answered == asked or (answered.startswith(asked + "-") and _SNAPSHOT.search(answered) is not None)


class ModelWitness(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.models: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        if request.url.path != "/v1/messages" or response.status_code != 200:
            return response
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_Tee(response.stream, self.models),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self.inner.aclose()


class _Tee(httpx.AsyncByteStream):
    def __init__(self, inner: Any, seen: list[str]) -> None:
        self.inner = inner
        self.seen = seen
        self.head = b""
        self.looking = True

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self.inner:
            if self.looking:
                self.head += chunk
                found = _MODEL.search(self.head)
                if found is not None:
                    self.seen.append(found.group(1).decode("utf-8", errors="replace"))
                    self.looking = False
                elif len(self.head) > _HEAD_BYTES:
                    self.looking = False
            yield chunk

    async def aclose(self) -> None:
        close = getattr(self.inner, "aclose", None)
        if close is not None:
            await close()
