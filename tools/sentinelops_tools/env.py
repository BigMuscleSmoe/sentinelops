"""What the tools read from, bound once per investigation."""

import functools
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

SERVICE = "checkout-api"
LOG_GROUP = "/aws/lambda/sentinelops-checkout-api"
HISTORY_TABLE = "sentinelops-deploy-history"
NAMESPACE = "checkout-api"


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ToolEnv:
    logs: Any  # boto3 clients
    cloudwatch: Any
    dynamodb: Any
    # The investigation's "now". A replay pins it to the moment the alarm fired,
    # so no tool reads anything later than that.
    now: Callable[[], datetime] = field(default=utc_now)
    # The real time, which decides what CloudWatch still keeps. Only a test sets it.
    wall_clock: Callable[[], datetime] = field(default=utc_now)
    service: str = SERVICE
    log_group: str = LOG_GROUP
    history_table: str = HISTORY_TABLE
    namespace: str = NAMESPACE

    @classmethod
    def from_session(cls, session: Any, now: Callable[[], datetime] = utc_now) -> "ToolEnv":
        from botocore.config import Config

        # A tool call has seconds, not minutes: fail fast and let the model move on.
        config = Config(
            connect_timeout=3, read_timeout=10, retries={"mode": "standard", "max_attempts": 3}
        )
        return cls(
            logs=session.client("logs", config=config),
            cloudwatch=session.client("cloudwatch", config=config),
            dynamodb=session.client("dynamodb", config=config),
            now=now,
        )


def iso_ms(moment: datetime) -> str:
    """UTC to the millisecond, as deploy history writes its sort keys."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def clock(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%H:%M:%S")


def aws_errors_as_text[F: Callable[..., str]](what: str) -> Callable[[F], F]:
    """Turn an AWS failure into a one-line result the model can act on.

    Only the error code gets through. AWS error messages can name the caller's
    ARN, and with it the account ID, and everything a tool returns lands in the
    model's context and the run's transcript.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    def decorate(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> str:
            try:
                return fn(*args, **kwargs)
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "unknown")
                return f"Could not read {what}: AWS returned {code}."
            except BotoCoreError as e:
                return f"Could not read {what}: {type(e).__name__}."

        return wrapper  # type: ignore[return-value]

    return decorate
