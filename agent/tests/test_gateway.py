"""The Gateway client: the model's tools as they are, run by a stand-in for the gateway.

The stand-in answers as the live gateway did, and runs each call through the
tool's own Lambda handler on recorded AWS calls. No test here reaches AWS.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

import httpx2
import pytest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from fake_gateway import URL, FakeGateway, text_result
from strands import tool
from stubs import TOOL_FIXTURES, FakeSession, load

import gateway as gateway_module
from gateway import Gateway
from guards import RunGuard, guarded_tool
from sentinelops_tools.env import ToolEnv
from sentinelops_tools.toolset import make_tools

CREDENTIALS = Credentials("testing", "testing", "testing")
FIXTURES = ["metrics_bad_deploy", "logs_bad_deploy_errors", "history_bad_deploy"]
NOW = load(TOOL_FIXTURES / "history_bad_deploy.json")["now"]
AS_OF = "2026-10-27T19:05:57.026Z"


def gateway(fake: FakeGateway) -> Gateway:
    return Gateway(URL, CREDENTIALS, "us-east-1", transport=fake.transport())


def call_all(client: Gateway, *, as_of: Any = None, **kwargs: Any) -> list[str]:
    async def go() -> list[str]:
        try:
            return [await fn(**kwargs) for fn in client.tools(as_of=as_of)]
        finally:
            await client.aclose()

    return asyncio.run(go())


def call_one(client: Gateway, index: int, **kwargs: Any) -> str:
    async def go() -> str:
        try:
            return await client.tools()[index](**kwargs)
        finally:
            await client.aclose()

    return asyncio.run(go())


def test_the_model_sees_the_same_tools_either_way() -> None:
    guard = RunGuard()
    here = make_tools(ToolEnv(logs=None, cloudwatch=None, dynamodb=None))
    remote = Gateway(URL, CREDENTIALS, "us-east-1").tools(as_of=None)

    assert [tool(guarded_tool(guard, fn)).tool_spec for fn in remote] == [
        tool(guarded_tool(guard, fn)).tool_spec for fn in here
    ]


def test_each_tool_returns_what_it_returns_here() -> None:
    session = FakeSession()
    for name in FIXTURES:
        session.replay(load(TOOL_FIXTURES / f"{name}.json"))
        assert load(TOOL_FIXTURES / f"{name}.json")["now"] == NOW
    fake = FakeGateway(ToolEnv.from_session(session))

    texts = call_all(gateway(fake), as_of=datetime.fromisoformat(NOW))

    assert texts == [load(TOOL_FIXTURES / f"{name}.json")["text"] for name in FIXTURES]
    session.assert_all_called()  # each handler made its tool's recorded calls, with their parameters
    # A session per call: the handshake, the listing the SDK makes anyway, then the call.
    assert fake.methods() == ["initialize", "notifications/initialized", "tools/list", "tools/call"] * 3
    assert fake.calls == [
        ("metrics___get_metrics", {"minutes": 10, "period_seconds": 10, "as_of": AS_OF}),
        ("logs___get_recent_logs", {"minutes": 10, "errors_only": True, "limit": 20, "as_of": AS_OF}),
        ("deploys___get_deploy_history", {"limit": 10, "as_of": AS_OF}),
    ]


def test_a_none_is_left_out_and_a_live_run_sends_no_as_of() -> None:
    fake = FakeGateway(answer=lambda name, arguments: text_result('{"text":"no lines"}'))

    assert call_one(gateway(fake), 1, route=None, minutes=5) == "no lines"
    assert fake.calls == [("logs___get_recent_logs", {"minutes": 5, "errors_only": True, "limit": 20})]


def test_every_request_is_signed_for_agentcore_over_what_was_sent() -> None:
    fake = FakeGateway(answer=lambda name, arguments: text_result('{"text":"ok"}'))
    call_one(gateway(fake), 2)

    assert len(fake.requests) == 4
    for request in fake.requests:
        authorization = request.headers["Authorization"]
        assert authorization.startswith("AWS4-HMAC-SHA256 Credential=testing/")
        assert "/us-east-1/bedrock-agentcore/aws4_request, SignedHeaders=host;x-amz-date;x-amz-security-token," in authorization
        # Signed again here, from the method, URL, and body the gateway received.
        again = AWSRequest(method=request.method, url=str(request.url), data=request.content)
        again.headers["X-Amz-Date"] = request.headers["X-Amz-Date"]
        again.headers["X-Amz-Security-Token"] = request.headers["X-Amz-Security-Token"]
        again.context["timestamp"] = request.headers["X-Amz-Date"]
        signer = SigV4Auth(CREDENTIALS.get_frozen_credentials(), "bedrock-agentcore", "us-east-1")
        signature = signer.signature(signer.string_to_sign(again, signer.canonical_request(again)), again)
        assert authorization.endswith(f"Signature={signature}")


def test_an_error_result_reaches_the_model_as_its_kind_only(caplog: pytest.LogCaptureFixture) -> None:
    arn = "arn:aws:lambda:us-east-1:123456789012:function:sentinelops-tool-metrics"
    fake = FakeGateway(answer=lambda name, arguments: text_result(f"AccessDeniedException: {arn}", error=True))

    with caplog.at_level(logging.WARNING, logger="sentinelops.gateway"):
        text = call_one(gateway(fake), 0)

    assert text == "Could not run get_metrics through the gateway: an error result."
    (line,) = [json.loads(r.getMessage()) for r in caplog.records]
    assert line["event"] == "gateway_failed" and line["kind"] == "an error result"


def test_a_result_that_is_not_the_handlers_json_is_reported() -> None:
    fake = FakeGateway(answer=lambda name, arguments: text_result("plain text"))

    assert call_one(gateway(fake), 2) == "Could not run get_deploy_history through the gateway: an unreadable result."


def refuse(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(403, json={"Message": "User: arn:aws:iam::123456789012:user/x is not authorized"})


def drop(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("connection refused", request=request)


@pytest.mark.parametrize(("handler", "kind"), [(refuse, "MCPError"), (drop, "ConnectError")])
def test_a_refused_or_dropped_request_reaches_the_model_as_its_kind(
    handler: Any, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway_module, "READ_TIMEOUT_S", 5.0)  # a hang fails in seconds, not minutes
    client = Gateway(URL, CREDENTIALS, "us-east-1", transport=httpx2.MockTransport(handler))

    text = call_one(client, 0)

    assert text == f"Could not run get_metrics through the gateway: {kind}."
    assert "123456789012" not in text


def test_no_credentials_is_refused_up_front() -> None:
    with pytest.raises(ValueError, match="No AWS credentials"):
        Gateway(URL, None, "us-east-1")
