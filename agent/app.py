"""The agent on AgentCore Runtime: the HTTP app the Runtime's contract asks for.

    POST /invocations  {"alarm": "sentinelops-checkout-errors"}
                       {"alarm": "sentinelops-checkout-errors", "as_of": "2026-10-27T19:06:00Z"}
    GET  /ping

An invocation runs one investigation, live or as a replay, and answers with
its outcome and record: "done" or "failed" with the transcript main.py writes,
"refused" with the reason nothing ran, or "invalid" (400) for a body it can't
use. The tools always run through the Gateway at SENTINELOPS_GATEWAY_URL: the
runtime's role may call the Gateway and read none of what the tools read.
The model is signed in to by federation as the runtime's role once Terraform
sets the SENTINELOPS_ANTHROPIC_* IDs, else with the API key.
Only the trigger invokes this, one run at a time; the global limits live
there.

Started as the Runtime starts it: python app.py, on 0.0.0.0:8080.
"""

import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from investigation import Refused, investigate_alarm, parse_utc
from main import configure_logging
from pricing import UnknownModelPrice, price_for
from providers import Settings, SettingsError

GATEWAY_URL = "SENTINELOPS_GATEWAY_URL"
FIELDS = {"alarm", "as_of"}
SESSION_HEADER = "x-amzn-bedrock-agentcore-runtime-session-id"

logger = logging.getLogger("sentinelops.app")


def create_app(
    *,
    environ: Mapping[str, str] = os.environ,
    session_factory: Callable[[str], Any] | None = None,
    http_client: Any = None,
    token_http_client: Any = None,
    gateway_transport: Any = None,
) -> Starlette:
    """The app. Tests pass the environment, an AWS session, and stand-ins for the
    model, the token endpoint, and the Gateway."""
    started = int(time.time())

    async def ping(request: Request) -> Response:
        # Healthy, and unchanged since start: a time that moved on every ping
        # would read as fresh activity and keep an idle session alive.
        return JSONResponse({"status": "Healthy", "time_of_last_update": started})

    async def invocations(request: Request) -> Response:
        session_id = request.headers.get(SESSION_HEADER)
        try:
            alarm_name, as_of = parse_body(await request.body())
        except ValueError as e:
            _log(session_id, "invalid", reason=str(e))
            return _answer({"outcome": "invalid", "reason": str(e)}, status_code=400)

        gateway_url = environ.get(GATEWAY_URL)
        if not gateway_url:
            return _refuse(session_id, f"{GATEWAY_URL} is not set, and here the tools run only through it.")
        try:
            settings = Settings.from_env(environ)
            price = price_for(settings.provider, settings.model_id)
        except (SettingsError, UnknownModelPrice) as e:
            return _refuse(session_id, str(e))

        outcome = await investigate_alarm(
            alarm_name,
            as_of,
            gateway_url=gateway_url,
            settings=settings,
            price=price,
            session=(session_factory or _aws_session)(settings.region),
            http_client=http_client,
            token_http_client=token_http_client,
            gateway_transport=gateway_transport,
        )
        if isinstance(outcome, Refused):
            return _refuse(session_id, outcome.reason)
        report = outcome.report
        result = "done" if report.hypothesis is not None else "failed"
        _log(session_id, result, alarm=alarm_name, replay=as_of is not None, summary=report.summary())
        return _answer({"outcome": result, **outcome.record()})

    return Starlette(
        routes=[
            Route("/ping", ping, methods=["GET"]),
            Route("/invocations", invocations, methods=["POST"]),
        ]
    )


def parse_body(body: bytes) -> tuple[str, datetime | None]:
    """The alarm's name, and as_of for a replay. Raises ValueError on anything else."""
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise ValueError('The body must be a JSON object, like {"alarm": "<name>"}.')
    unknown = sorted(set(payload) - FIELDS)
    if unknown:
        raise ValueError(f"Unknown fields: {', '.join(unknown)}. Send only alarm, and as_of for a replay.")
    alarm = payload.get("alarm")
    if not isinstance(alarm, str) or not alarm:
        raise ValueError("alarm must be the alarm's name.")
    as_of = payload.get("as_of")
    if as_of is None:
        return alarm, None
    if not isinstance(as_of, str):
        raise ValueError("as_of must be an ISO 8601 time with its time zone.")
    return alarm, parse_utc(as_of)


def _aws_session(region: str) -> Any:
    import boto3

    # The runtime's role, through the Runtime's credentials provider.
    return boto3.Session(region_name=region)


def _refuse(session_id: str | None, reason: str) -> Response:
    _log(session_id, "refused", reason=reason)
    return _answer({"outcome": "refused", "reason": reason})


def _answer(body: dict[str, Any], status_code: int = 200) -> Response:
    # ASCII-escaped, as main.py prints it; default=str as the transcript is written.
    return Response(json.dumps(body, default=str), status_code=status_code, media_type="application/json")


def _log(session_id: str | None, outcome: str, **fields: Any) -> None:
    logger.info(json.dumps({"event": "invocation", "session": session_id, "outcome": outcome, **fields}))


if __name__ == "__main__":
    import uvicorn

    configure_logging()
    # The Runtime's contract: plain HTTP on 0.0.0.0:8080. Uvicorn logs through
    # ours, without a line per request: the Runtime pings every few seconds.
    uvicorn.run(create_app(), host="0.0.0.0", port=8080, log_config=None, access_log=False)
