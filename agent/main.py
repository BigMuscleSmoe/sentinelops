"""Investigate one alarm from this machine.

    uv run python -m main --alarm sentinelops-checkout-errors
    uv run python -m main --alarm sentinelops-checkout-errors --as-of 2026-10-27T19:06:00Z

Live, the alarm must be in ALARM now. With --as-of, the run replays the
alarm's last change into ALARM at or before that time, and every tool reads
as of that moment. The tools run here, unless --gateway (or
SENTINELOPS_GATEWAY_URL) names the AgentCore Gateway to run them. The
Hypothesis goes to stdout as JSON. The summary and the guard's JSON log lines
go to stderr, and the transcript to scratch/runs/.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from investigation import Refused, investigate_alarm, parse_utc
from pricing import ModelPrice, UnknownModelPrice, price_for
from providers import Settings, SettingsError

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2
TRANSCRIPTS = Path("scratch") / "runs"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main", description="Investigate one CloudWatch alarm on checkout-api."
    )
    parser.add_argument("--alarm", required=True, help="the alarm's name, e.g. sentinelops-checkout-errors")
    parser.add_argument(
        "--as-of", type=_utc, help="replay the alarm as of this time, e.g. 2026-10-27T19:06:00Z"
    )
    parser.add_argument(
        "--profile",
        default=os.environ.get("AWS_PROFILE", "sentinelops"),
        help="the AWS CLI profile (default: $AWS_PROFILE, else sentinelops)",
    )
    parser.add_argument(
        "--gateway",
        default=os.environ.get("SENTINELOPS_GATEWAY_URL"),
        help="run the tools through this AgentCore Gateway MCP URL "
        "(default: $SENTINELOPS_GATEWAY_URL; unset, they run here)",
    )
    parser.add_argument("--transcript", type=Path, help="where to write the transcript")
    return parser.parse_args(argv)


def configure_logging() -> None:
    # Ours at INFO: the guard's and the model calls' JSON lines. Libraries at
    # WARNING, so request dumps stay out of the terminal.
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(message)s")
    logging.getLogger("sentinelops").setLevel(logging.INFO)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging()
    try:
        settings = Settings.from_env()
        price = price_for(settings.provider, settings.model_id)
    except (SettingsError, UnknownModelPrice) as e:
        return refuse(str(e), sys.stderr)

    import boto3

    session = boto3.Session(profile_name=args.profile, region_name=settings.region)
    return asyncio.run(run(args, settings=settings, price=price, session=session))


async def run(
    args: argparse.Namespace,
    *,
    settings: Settings,
    price: ModelPrice,
    session: Any,
    http_client: Any = None,
    gateway_transport: Any = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    outcome = await investigate_alarm(
        args.alarm,
        args.as_of,
        gateway_url=args.gateway,
        settings=settings,
        price=price,
        session=session,
        http_client=http_client,
        gateway_transport=gateway_transport,
    )
    if isinstance(outcome, Refused):
        return refuse(outcome.reason, stderr)

    report = outcome.report
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = args.transcript or TRANSCRIPTS / f"{stamp}-{outcome.alarm.name}.json"
    write_transcript(path, outcome.record())
    print(f"{report.summary()} | transcript: {path}", file=stderr)
    if report.hypothesis is None:
        print(f"No Hypothesis: {report.failure}", file=stderr)
        return EXIT_FAILED
    # ASCII-escaped: Windows encodes a redirected stdout as cp1252, which has no
    # arrow, and the model writes them. Printed raw, one ends a paid-for run in
    # UnicodeEncodeError. stderr escapes what it can't encode, so it's safe.
    print(json.dumps(report.hypothesis.model_dump(mode="json"), indent=2), file=stdout)
    return EXIT_OK


def write_transcript(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=1, default=str), encoding="utf-8")


def refuse(reason: str, stderr: TextIO) -> int:
    print(f"Refused: {reason}", file=stderr)
    return EXIT_REFUSED


def _utc(value: str) -> datetime:
    try:
        return parse_utc(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


if __name__ == "__main__":
    sys.exit(main())
