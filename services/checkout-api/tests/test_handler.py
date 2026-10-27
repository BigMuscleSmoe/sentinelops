"""handler.py as Lambda runs it, with boto3 swapped for a stub at import."""

import importlib
import json
import sys
import time
import types

import pytest

from checkout import VERSION
from sim import releases

FLAGS_TABLE = "sentinelops-checkout-flags"
POOL_TABLE = "sentinelops-checkout-pool"


class StubDynamo:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.flag: dict | None = None  # the flags table's one item

    def get_item(self, **kwargs):
        self.calls.append(("get_item", kwargs["TableName"]))
        if kwargs["TableName"] == FLAGS_TABLE:
            return {"Item": self.flag} if self.flag else {}
        return {"Item": {"pool": {"S": "checkout-api"}, "slots": {"M": {}}, "ends": {"M": {}}}}

    def update_item(self, **kwargs):
        self.calls.append(("update_item", kwargs["TableName"]))
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
    monkeypatch.setenv("POOL_TABLE", POOL_TABLE)
    monkeypatch.setenv("FLAGS_TABLE", FLAGS_TABLE)
    monkeypatch.setenv("POOL_MAX_SIZE", "20")
    monkeypatch.delitem(sys.modules, "handler", raising=False)
    yield importlib.import_module("handler"), dynamo
    sys.modules.pop("handler", None)
    releases.deploy(VERSION)


def flag(overrides):
    return {
        "flag": {"S": "active"},
        "overrides": {"M": overrides},
        "set_at": {"N": str(time.time())},
        "expires_at": {"N": str(time.time() + 600)},
    }


def lines(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_only_the_first_request_in_an_environment_is_a_cold_start(handler, capsys):
    module, dynamo = handler
    health = module.handler({"routeKey": "GET /health"}, Context())
    product = module.handler({"routeKey": "GET /products/{id}", "pathParameters": {"id": "p-1001"}}, Context())

    assert (health["statusCode"], product["statusCode"]) == (200, 200)
    logged = lines(capsys)
    assert [line["cold_start"] for line in logged] == [True, False]
    assert logged[1]["request_id"] == Context.aws_request_id
    # The flag is read once for both (it's cached for 2 s), and health never touches the pool.
    assert dynamo.calls == [("get_item", FLAGS_TABLE), ("get_item", POOL_TABLE), ("update_item", POOL_TABLE)]


def test_a_pool_override_reaches_the_request(handler, capsys):
    module, dynamo = handler
    dynamo.flag = flag({"POOL_MAX_SIZE": {"N": "2"}})
    module.handler({"routeKey": "GET /products/{id}", "pathParameters": {"id": "p-1001"}}, Context())
    (line,) = lines(capsys)
    assert (line["pool_max_size"], line["version"]) == (2, VERSION)


def test_a_release_override_runs_1_43_0_whose_checkouts_fail_without_a_promo(handler, capsys):
    module, dynamo = handler
    dynamo.flag = flag({"release": {"S": "1.43.0"}})
    body = json.dumps({"items": [{"product_id": "p-1003", "quantity": 1}]})
    response = module.handler({"routeKey": "POST /checkout", "body": body}, Context())

    assert response["statusCode"] == 500
    (line,) = lines(capsys)
    assert (line["version"], line["error"], line["error_type"], line["has_promo"]) == ("1.43.0", 1, "TypeError", False)
    assert line["message"] == "TypeError: 'NoneType' object is not subscriptable"
    assert line["stack"][-1].startswith("checkout/pricing.py:") and line["stack"][-1].endswith(" in apply_promo")
    assert line["stack"][-2].startswith("checkout/pricing.py:") and line["stack"][-2].endswith(" in price_cart")
    assert not any("sim/" in frame for frame in line["stack"])
    assert "pool_wait_time_ms" not in line  # it failed before asking for a connection
    assert ("get_item", POOL_TABLE) not in dynamo.calls


def test_when_the_flag_ends_the_healthy_release_is_back(handler, capsys):
    module, dynamo = handler
    dynamo.flag = flag({"release": {"S": "1.43.0"}})
    dynamo.flag["expires_at"] = {"N": str(time.time() + 0.5)}
    module.handler({"routeKey": "GET /health"}, Context())
    time.sleep(0.6)  # past expires_at, though the cached item hasn't changed
    body = json.dumps({"items": [{"product_id": "p-1003", "quantity": 1}]})
    response = module.handler({"routeKey": "POST /checkout", "body": body}, Context())
    assert response["statusCode"] == 200
    assert [line["version"] for line in lines(capsys)] == ["1.43.0", VERSION]
