"""Recorded AWS calls, replayed through botocore's Stubber, for the agent's tests.

The alarm fixtures live here; the tools' fixtures, recorded the same night from
the same runs, live in tools/tests/fixtures.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import boto3
from botocore.stub import Stubber

AGENT_FIXTURES = Path(__file__).parent / "fixtures"
TOOL_FIXTURES = Path(__file__).resolve().parents[2] / "tools" / "tests" / "fixtures"
SERVICE_OF = {
    "describe_alarms": "cloudwatch",
    "describe_alarm_history": "cloudwatch",
    "get_metric_data": "cloudwatch",
    "filter_log_events": "logs",
    "query": "dynamodb",
    "get_secret_value": "secretsmanager",
}


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$dt"}:
            return datetime.fromisoformat(value["$dt"])
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


def load(path: Path) -> dict[str, Any]:
    return _decode(json.loads(path.read_text(encoding="utf-8")))


def client(service: str) -> Any:
    return boto3.client(
        service, region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing"
    )


class FakeSession:
    """Stands in for boto3.Session: one stubbed client per service, whatever config is asked for."""

    def __init__(self) -> None:
        self.clients = {name: client(name) for name in set(SERVICE_OF.values())}
        self.stubbers = {name: Stubber(c) for name, c in self.clients.items()}
        for stubber in self.stubbers.values():
            stubber.activate()

    def client(self, service: str, **_: Any) -> Any:
        return self.clients[service]

    def expect(self, operation: str, params: dict[str, Any] | None, response: dict[str, Any]) -> None:
        self.stubbers[SERVICE_OF[operation]].add_response(operation, response, params)

    def fail(self, operation: str, code: str, message: str = "") -> None:
        self.stubbers[SERVICE_OF[operation]].add_client_error(
            operation, service_error_code=code, service_message=message
        )

    def replay(self, fixture: dict[str, Any]) -> None:
        for call in fixture["calls"]:
            self.expect(call["operation"], call["params"], call["response"])

    def assert_all_called(self) -> None:
        for stubber in self.stubbers.values():
            stubber.assert_no_pending_responses()
