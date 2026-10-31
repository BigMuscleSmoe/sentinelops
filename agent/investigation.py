"""One investigation of one alarm, for both ways to start one: main.py from this
machine, and app.py on AgentCore Runtime.

It reads the alarm, live or as of a past moment, signs in to the model (by
federation once its IDs are set, else with the API key), picks the tools
(through the Gateway when a URL is given, else run in this process), and runs
the guarded investigation. A refusal comes back as a Refused, never an
exception, so each entry point reports it its own way.
"""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from alarm import AlarmEvent, AlarmNotFiring
from pricing import ModelPrice
from providers import (
    FederationFailed,
    KeyUnavailable,
    Settings,
    SettingsError,
    build_model,
    federated_credentials,
    read_api_key,
)
from runner import RunReport, investigate
from sentinelops_tools.env import ToolEnv, utc_now
from sentinelops_tools.toolset import make_tools


@dataclass(frozen=True)
class Refused:
    """Nothing ran: the alarm isn't firing, or the model can't be set up."""

    reason: str


@dataclass(frozen=True)
class Investigation:
    alarm: AlarmEvent
    settings: Settings
    report: RunReport
    replay: bool
    gateway: bool

    def record(self) -> dict[str, Any]:
        """The run as JSON: the transcript main.py writes and the Runtime answers with."""
        report = self.report
        return {
            "alarm": {
                "name": self.alarm.name,
                "fired_at": self.alarm.fired_at.isoformat(),
                "condition": self.alarm.condition,
                "reason": self.alarm.reason,
                "replay": self.replay,
            },
            "model": {
                "provider": self.settings.provider,
                "model_id": self.settings.model_id,
                "auth": self.settings.auth,
            },
            "tools_via": "gateway" if self.gateway else "local",
            "hypothesis": report.hypothesis.model_dump(mode="json") if report.hypothesis else None,
            "failure": report.failure,
            "tool_calls": report.tool_calls,
            "model_calls": [asdict(call) for call in report.model_calls],
            "input_tokens": report.input_tokens,
            "cost_usd": round(report.cost_usd, 6),
            "seconds": round(report.seconds, 1),
            "messages": report.messages,
        }


def parse_utc(value: str) -> datetime:
    """An ISO 8601 time that names its time zone, in UTC. Raises ValueError otherwise."""
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError(f"{value} has no time zone; end it with Z for UTC")
    return moment.astimezone(UTC)


async def investigate_alarm(
    alarm_name: str,
    as_of: datetime | None,
    *,
    gateway_url: str | None,
    settings: Settings,
    price: ModelPrice,
    session: Any,
    http_client: Any = None,
    token_http_client: Any = None,
    gateway_transport: Any = None,
) -> Investigation | Refused:
    """Investigate the alarm live, or as of a past moment, where every tool reads as of then.

    http_client, token_http_client, and gateway_transport are for tests: a fake
    Messages API, token endpoint, and Gateway.
    """
    cloudwatch = session.client("cloudwatch")
    try:
        if as_of is None:
            alarm = AlarmEvent.live(cloudwatch, alarm_name)
        else:
            alarm = AlarmEvent.as_of(cloudwatch, alarm_name, as_of)
    except AlarmNotFiring as e:
        return Refused(str(e))

    try:
        model = _signed_in_model(settings, session, http_client, token_http_client)
    except (FederationFailed, KeyUnavailable, SettingsError) as e:
        return Refused(str(e))

    fired_at = alarm.fired_at
    replay = as_of is not None
    gateway = None
    if gateway_url:
        from gateway import Gateway

        gateway = Gateway(gateway_url, session.get_credentials(), settings.region, transport=gateway_transport)
        tools = gateway.tools(as_of=fired_at if replay else None)
    else:
        env = ToolEnv.from_session(session, now=(lambda: fired_at) if replay else utc_now)
        tools = make_tools(env)
    try:
        report = await investigate(
            alarm.prompt(),
            model=model,
            tools=tools,
            price=price,
            max_output_tokens=settings.max_output_tokens,
        )
    finally:
        await model.client.close()
        if gateway is not None:
            await gateway.aclose()
    return Investigation(alarm, settings, report, replay=replay, gateway=gateway is not None)


def _signed_in_model(settings: Settings, session: Any, http_client: Any, token_http_client: Any) -> Any:
    """The model, signed in by federation when its IDs are set, else with the key.

    With federation set, the key is never read: half a setup was refused with
    the settings, and a failed sign-in is a refusal, not a fallback.
    """
    if settings.provider != "anthropic":
        return build_model(settings, http_client=http_client)
    if settings.federation is not None:
        credentials = federated_credentials(settings.federation, session, http_client=token_http_client)
        return build_model(settings, http_client=http_client, credentials=credentials)
    # The key goes straight to the model client, never bound to a name here.
    return build_model(settings, read_api_key(session.client("secretsmanager")), http_client=http_client)
