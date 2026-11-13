"""Which model answered: read off each Messages response as it streams past.

Strands keeps the model it asked for, not the one the API says answered, so
the sweep sends the API traffic through this transport, which notes the
`model` of every response's message_start and passes every byte on unchanged.

A transport sees the body as sent, before httpx undoes any compression, so the
witness asks for it uncompressed (Accept-Encoding: identity) and, if it comes
compressed anyway, reads a decompressed copy. The first sweep's first run found
no model for want of this: the API gzips its stream (2026-11-13).
"""

import re
import zlib
from collections.abc import AsyncIterator
from typing import Any

import httpx

_MODEL = re.compile(rb'"model"\s*:\s*"([^"]+)"')
# message_start is the first event; past this much, a response has no model to find.
_HEAD_BYTES = 64_000
_SNAPSHOT = re.compile(r"-\d{8}$")
_DECODERS = {"gzip": lambda: zlib.decompressobj(16 + zlib.MAX_WBITS), "deflate": zlib.decompressobj}


def same_model(asked: str, answered: str) -> bool:
    """The model asked for, or a dated snapshot of it."""
    return answered == asked or (answered.startswith(asked + "-") and _SNAPSHOT.search(answered) is not None)


def _messages(request: httpx.Request) -> bool:
    return request.url.path.endswith("/v1/messages")


class ModelWitness(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.models: list[str] = []
        # Each Messages response's status and encoding, to say why a model wasn't found.
        self.responses: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if _messages(request):
            request.headers["Accept-Encoding"] = "identity"
        response = await self.inner.handle_async_request(request)
        if not _messages(request):
            return response
        encoding = response.headers.get("content-encoding", "identity").strip().lower() or "identity"
        self.responses.append(f"{response.status_code} {encoding}")
        if response.status_code != 200 or (encoding != "identity" and encoding not in _DECODERS):
            return response
        decoder = _DECODERS[encoding]() if encoding in _DECODERS else None
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_Tee(response.stream, self.models, decoder),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self.inner.aclose()


class _Tee(httpx.AsyncByteStream):
    def __init__(self, inner: Any, seen: list[str], decoder: Any) -> None:
        self.inner = inner
        self.seen = seen
        self.decoder = decoder
        self.head = b""
        self.looking = True

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self.inner:
            if self.looking:
                self._look(chunk)
            yield chunk

    def _look(self, chunk: bytes) -> None:
        try:
            self.head += self.decoder.decompress(chunk) if self.decoder is not None else chunk
        except zlib.error:
            self.looking = False
            return
        found = _MODEL.search(self.head)
        if found is not None:
            self.seen.append(found.group(1).decode("utf-8", errors="replace"))
            self.looking = False
        elif len(self.head) > _HEAD_BYTES:
            self.looking = False

    async def aclose(self) -> None:
        close = getattr(self.inner, "aclose", None)
        if close is not None:
            await close()
