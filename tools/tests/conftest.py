"""Recorded AWS calls, replayed through botocore's Stubber.

Each fixture in fixtures/ holds the calls a tool made against the live stack on
2026-10-28, reading the 2026-10-27 live runs: every request with its exact
parameters, and AWS's answer. The Stubber fails a test if the tool asks for
anything else, in another order, or leaves a recorded call unmade.
"""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

from sentinelops_tools.env import ToolEnv

FIXTURES = Path(__file__).parent / "fixtures"
# When the fixtures were recorded: CloudWatch had already dropped the runs'
# 10-second data, so a replay of them reads 60-second periods.
RECORDED_AT = datetime(2026, 10, 28, 4, 27, tzinfo=UTC)


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$dt"}:
            return datetime.fromisoformat(value["$dt"])
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


def load(name: str) -> dict[str, Any]:
    return _decode(json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8")))


def client(service: str) -> Any:
    return boto3.client(
        service,
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


class Stubbed:
    """A ToolEnv on stubbed clients, with the investigation's now pinned."""

    def __init__(self, now: datetime, wall_clock: datetime = RECORDED_AT) -> None:
        self.clients = {name: client(name) for name in ("logs", "cloudwatch", "dynamodb")}
        self.stubbers = {name: Stubber(c) for name, c in self.clients.items()}
        self.env = ToolEnv(
            logs=self.clients["logs"],
            cloudwatch=self.clients["cloudwatch"],
            dynamodb=self.clients["dynamodb"],
            now=lambda: now,
            wall_clock=lambda: wall_clock,
        )

    def expect(self, service: str, operation: str, params: dict[str, Any], response: dict[str, Any]) -> None:
        self.stubbers[service].add_response(operation, response, params)

    def replay(self, fixture: dict[str, Any]) -> None:
        for call in fixture["calls"]:
            self.expect(SERVICE_OF[call["operation"]], call["operation"], call["params"], call["response"])

    def __enter__(self) -> "Stubbed":
        for s in self.stubbers.values():
            s.activate()
        return self

    def __exit__(self, *exc: object) -> None:
        for s in self.stubbers.values():
            s.deactivate()
        if exc[0] is None:
            for s in self.stubbers.values():
                s.assert_no_pending_responses()


SERVICE_OF = {
    "filter_log_events": "logs",
    "get_metric_data": "cloudwatch",
    "query": "dynamodb",
}


@pytest.fixture
def recorded() -> Iterator[Any]:
    """Replay a fixture: `with recorded("logs_pool_errors") as (env, fixture): ...`."""

    class _Replay:
        def __init__(self, name: str) -> None:
            self.fixture = load(name)
            now = datetime.fromisoformat(self.fixture["now"])
            self.stubbed = Stubbed(now)
            self.stubbed.replay(self.fixture)

        def __enter__(self) -> tuple[ToolEnv, dict[str, Any]]:
            self.stubbed.__enter__()
            return self.stubbed.env, self.fixture

        def __exit__(self, *exc: object) -> None:
            self.stubbed.__exit__(*exc)

    yield _Replay
