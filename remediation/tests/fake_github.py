"""A stand-in for api.github.com and for KMS: recorded answers served in order, every request kept.

Recorded answers come from tests/fixtures/github/ (record_responses.py): the
public reads without a token, and the App's own (GET /app, its installations,
a token's grant, the installation's repositories, the repository, a check run,
the fix branch's PRs) as the App, 2026-11-25, with the token and every ID
replaced. No token here is real.
"""

import hashlib
import json
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sentinelops_remediation import github

FIXTURES = Path(__file__).parent / "fixtures" / "github"
NOW = datetime(2026, 11, 25, 3, 0, tzinfo=UTC).timestamp()
CLIENT_ID = "Iv23liTestClientId"
INSTALLATION_ID = 4242
TOKEN = "test-installation-token-not-real"



def recorded(slug: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{slug}.json").read_text(encoding="utf-8"))


# POST /app/installations/{id}/access_tokens as recorded: a token scoped to this
# repository and the App's four permissions. Its token is the tests' own, and
# its expiry is moved to an hour after the fake clock's NOW, as GitHub's was.
TOKEN_ANSWER = {**recorded("installation_token")["json"], "token": TOKEN, "expires_at": "2026-11-25T04:00:00Z"}


class FakeKMS:
    """kms:Sign, answering a stand-in signature: the SHA-256 of what it was asked to sign."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def sign(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"Signature": hashlib.sha256(kwargs["Message"]).digest(), "SigningAlgorithm": kwargs["SigningAlgorithm"]}


class FakeGitHub:
    """Answers queued per method and URL, served in order; a request nothing was queued for fails the test."""

    def __init__(self) -> None:
        self.queued: dict[tuple[str, str], deque[github.Response | Exception]] = {}
        self.requests: list[github.Request] = []

    def add(self, method: str, url: str, status: int = 200, body: Any = None, headers: dict[str, str] | None = None) -> None:
        raw = b"" if body is None else json.dumps(body).encode("utf-8")
        self.queued.setdefault((method, url), deque()).append(github.Response(status, headers or {}, raw))

    def fail(self, method: str, url: str) -> None:
        """No answer at all: a timeout."""
        self.queued.setdefault((method, url), deque()).append(github.TransportError("TimeoutError"))

    def add_recorded(self, slug: str, body: Any = None) -> None:
        """A recorded exchange, optionally with its JSON replaced."""
        exchange = recorded(slug)
        target = github.url(exchange["endpoint"], exchange["params"], exchange["query"])
        method = exchange.get("method", "GET")
        self.add(method, target, exchange["status"], exchange["json"] if body is None else body)

    def add_token(self, answer: dict[str, Any] | None = None) -> None:
        target = github.url("installation_token", {"installation_id": INSTALLATION_ID})
        self.add("POST", target, 201, TOKEN_ANSWER if answer is None else answer)

    def add_revoke(self) -> None:
        self.add("DELETE", github.url("revoke_token"), 204)

    def __call__(self, request: github.Request) -> github.Response:
        self.requests.append(request)
        queue = self.queued.get((request.method, request.url))
        if not queue:
            raise AssertionError(f"unexpected request: {request.method} {request.url}")
        answer = queue.popleft()
        if isinstance(answer, Exception):
            raise answer
        return answer

    def unused(self) -> list[tuple[str, str]]:
        return [key for key, queue in self.queued.items() if queue]

    def sent(self) -> list[tuple[str, str]]:
        return [(r.method, r.url) for r in self.requests]


def app(fake: FakeGitHub, kms: Any = None, clock: Any = None, sleeps: list[float] | None = None) -> github.GitHubApp:
    waits = sleeps if sleeps is not None else []
    return github.GitHubApp(
        CLIENT_ID,
        INSTALLATION_ID,
        kms if kms is not None else FakeKMS(),
        transport=fake,
        clock=clock or (lambda: NOW),
        sleep=waits.append,
    )
