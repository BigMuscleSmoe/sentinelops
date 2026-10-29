"""Investigate one alarm from this machine.

    uv run python -m main --alarm sentinelops-checkout-errors
    uv run python -m main --alarm sentinelops-checkout-errors --as-of 2026-10-27T19:06:00Z

Live, the alarm must be in ALARM now. With --as-of, the run replays the
alarm's last change into ALARM at or before that time, and every tool reads
as of that moment. The Hypothesis goes to stdout as JSON. The summary and the
guard's JSON log lines go to stderr, and the transcript to scratch/runs/.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from alarm import AlarmEvent, AlarmNotFiring
from pricing import ModelPrice, UnknownModelPrice, price_for
from providers import KeyUnavailable, Settings, SettingsError, build_model, read_api_key
from runner import RunReport, investigate
from sentinelops_tools.env import ToolEnv, utc_now
from sentinelops_tools.toolset import make_tools

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
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    cloudwatch = session.client("cloudwatch")
    try:
        if args.as_of is None:
            alarm = AlarmEvent.live(cloudwatch, args.alarm)
        else:
            alarm = AlarmEvent.as_of(cloudwatch, args.alarm, args.as_of)
    except AlarmNotFiring as e:
        return refuse(str(e), stderr)

    try:
        api_key = (
            read_api_key(session.client("secretsmanager")) if settings.provider == "anthropic" else None
        )
        model = build_model(settings, api_key, http_client=http_client)
    except (KeyUnavailable, SettingsError) as e:
        return refuse(str(e), stderr)
    del api_key  # the model client holds it now

    fired_at = alarm.fired_at
    env = ToolEnv.from_session(session, now=(lambda: fired_at) if args.as_of else utc_now)
    try:
        report = await investigate(
            alarm.prompt(),
            model=model,
            tools=make_tools(env),
            price=price,
            max_output_tokens=settings.max_output_tokens,
        )
    finally:
        await model.client.close()

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = args.transcript or TRANSCRIPTS / f"{stamp}-{alarm.name}.json"
    write_transcript(path, alarm, settings, report, replay=args.as_of is not None)
    print(f"{report.summary()} | transcript: {path}", file=stderr)
    if report.hypothesis is None:
        print(f"No Hypothesis: {report.failure}", file=stderr)
        return EXIT_FAILED
    # ASCII-escaped: Windows encodes a redirected stdout as cp1252, which has no
    # arrow, and the model writes them. Printed raw, one ends a paid-for run in
    # UnicodeEncodeError. stderr escapes what it can't encode, so it's safe.
    print(json.dumps(report.hypothesis.model_dump(mode="json"), indent=2), file=stdout)
    return EXIT_OK


def write_transcript(
    path: Path, alarm: AlarmEvent, settings: Settings, report: RunReport, replay: bool
) -> None:
    record = {
        "alarm": {
            "name": alarm.name,
            "fired_at": alarm.fired_at.isoformat(),
            "condition": alarm.condition,
            "reason": alarm.reason,
            "replay": replay,
        },
        "model": {"provider": settings.provider, "model_id": settings.model_id},
        "hypothesis": report.hypothesis.model_dump(mode="json") if report.hypothesis else None,
        "failure": report.failure,
        "tool_calls": report.tool_calls,
        "model_calls": [asdict(call) for call in report.model_calls],
        "input_tokens": report.input_tokens,
        "cost_usd": round(report.cost_usd, 6),
        "seconds": round(report.seconds, 1),
        "messages": report.messages,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=1, default=str), encoding="utf-8")


def refuse(reason: str, stderr: TextIO) -> int:
    print(f"Refused: {reason}", file=stderr)
    return EXIT_REFUSED


def _utc(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise argparse.ArgumentTypeError(f"{value} has no time zone; end it with Z for UTC")
    return moment.astimezone(UTC)


if __name__ == "__main__":
    sys.exit(main())
