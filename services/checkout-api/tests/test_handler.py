"""handler.py as Lambda runs it, with boto3 swapped for a stub at import."""

import importlib
import json
import sys
import types

import pytest


class StubDynamo:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_item(self, **kwargs):
        self.calls.append("get_item")
        return {"Item": {"pool": {"S": "checkout-api"}, "slots": {"M": {}}, "ends": {"M": {}}}}

    def update_item(self, **kwargs):
        self.calls.append("update_item")
        names, values = kwargs["ExpressionAttributeNames"], kwargs["ExpressionAttributeValues"]
        return {"Attributes": {"slots": {"M": {names["#i"]: values[":release"]}}}}


class Context:
    aws_request_id = "5f0c1d2e-0000-4000-8000-000000000000"


@pytest.fixture
def handler(monkeypatch):
    dynamo = StubDynamo()
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda service, config=None: dynamo
    botocore_config = types.ModuleType("botocore.config")
    botocore_config.Config = lambda **kwargs: kwargs
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "botocore", types.ModuleType("botocore"))
    monkeypatch.setitem(sys.modules, "botocore.config", botocore_config)
    monkeypatch.setenv("POOL_TABLE", "sentinelops-checkout-pool")
    monkeypatch.setenv("POOL_MAX_SIZE", "20")
    monkeypatch.delitem(sys.modules, "handler", raising=False)
    yield importlib.import_module("handler"), dynamo
    sys.modules.pop("handler", None)


def test_only_the_first_request_in_an_environment_is_a_cold_start(handler, capsys):
    module, dynamo = handler
    health = module.handler({"routeKey": "GET /health"}, Context())
    product = module.handler({"routeKey": "GET /products/{id}", "pathParameters": {"id": "p-1001"}}, Context())

    assert (health["statusCode"], product["statusCode"]) == (200, 200)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["cold_start"] for line in lines] == [True, False]
    assert lines[1]["request_id"] == Context.aws_request_id
    assert dynamo.calls == ["get_item", "update_item"]  # health never touches the pool
