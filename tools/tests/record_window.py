"""Record a window of the live stack, so its alarms can be replayed after CloudWatch drops it.

    cd tools
    uv run python tests/record_window.py 2026-10-27 2026-10-27T18:50:00Z 2026-10-27T19:06:00Z

Every alarm that went into ALARM between the two times becomes a replay. The
window holds what a replay of any of them can read: checkout-api's request log
lines for the 180 minutes before the first (the log tool's longest reach), its
60-second metrics for the same span, its deploy history up to the last, and the
alarms with their state changes until 20 minutes after it. The account ID is
replaced wherever AWS wrote it.

Read-only: DescribeAlarms, DescribeAlarmHistory, GetMetricData, FilterLogEvents,
Query, and GetCallerIdentity, to know which account ID to replace. It prints
counts and the tools' first lines, never a log line or the account ID.

Then it checks the recording. At each replay, every tool is called on a grid of
arguments against the live stack and against the recording, and the two must
return the same text. The live log tool gets three tries, since FilterLogEvents
can silently return fewer events. The calls, with a hash of what they returned,
become the window's checks, which test_windows.py replays.
"""

import argparse
import gzip
import hashlib
import io
import json
import shutil
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from window import PLACEHOLDER_ACCOUNT, WINDOWS, Window, parse_time, to_ms

from sentinelops_tools import deploys, logs, metrics
from sentinelops_tools.env import HISTORY_TABLE, LOG_GROUP, NAMESPACE, SERVICE, ToolEnv, iso_ms

ALARM_PREFIX = "sentinelops-checkout-"
REACH = timedelta(minutes=logs.MAX_MINUTES)
HISTORY_AFTER = timedelta(minutes=20)
CHUNK_MS = 10 * 60_000
PERIOD = 60
LIVE_TRIES = 3
ALARM_FIELDS = (
    "AlarmName",
    "AlarmDescription",
    "Namespace",
    "MetricName",
    "Dimensions",
    "Statistic",
    "ExtendedStatistic",
    "Period",
    "EvaluationPeriods",
    "DatapointsToAlarm",
    "Threshold",
    "ComparisonOperator",
    "TreatMissingData",
    "EvaluateLowSampleCountPercentile",
)
HISTORY_FIELDS = ("AlarmName", "AlarmType", "Timestamp", "HistoryItemType", "HistorySummary", "HistoryData")
TOOLS = {
    "get_metrics": metrics.metrics_table,
    "get_recent_logs": logs.recent_lines,
    "get_deploy_history": deploys.history,
}


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record a window of the live stack for offline replays.")
    parser.add_argument("name", help="the window's folder under fixtures/windows/, e.g. 2026-10-27")
    parser.add_argument("start", type=parse_time, help="replay every alarm into ALARM from this time...")
    parser.add_argument("end", type=parse_time, help="...to this one")
    parser.add_argument("--about", default="", help="what happened in the window, for whoever replays it")
    parser.add_argument("--profile", default="sentinelops")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    session = boto3.Session(profile_name=args.profile, region_name="us-east-1")
    try:
        return record(args, session)
    except ClientError as e:
        # Only the code: AWS error messages can name the caller's ARN.
        print(f"AWS returned {e.response.get('Error', {}).get('Code', 'unknown')}.", file=sys.stderr)
    except BotoCoreError as e:
        print(f"AWS call failed: {type(e).__name__}.", file=sys.stderr)
    return 1


def record(args: argparse.Namespace, session: Any) -> int:
    config = Config(retries={"mode": "standard", "max_attempts": 8})
    cloudwatch = session.client("cloudwatch", config=config)
    account = session.client("sts").get_caller_identity()["Account"]
    recorded_at = datetime.now(UTC)

    replays = find_replays(cloudwatch, args.start, args.end)
    if not replays:
        print("No alarm went into ALARM between those times.", file=sys.stderr)
        return 2
    first, last = parse_time(replays[0]["fired_at"]), parse_time(replays[-1]["fired_at"])
    # A minute's slack: the log tool's millisecond arithmetic can land a hair
    # before a span that starts exactly 180 minutes back.
    logs_from, history_to = first - REACH - timedelta(minutes=1), last + HISTORY_AFTER
    for replay in replays:
        print(f"replay: {replay['alarm']} at {replay['fired_at']}")

    events, passes = record_logs(session.client("logs", config=config), to_ms(logs_from), to_ms(last))
    window: dict[str, Any] = {
        "name": args.name,
        "about": args.about,
        "recorded_at": iso(recorded_at),
        "account": f"{PLACEHOLDER_ACCOUNT} stands in for the account ID wherever AWS wrote it.",
        "replays": replays,
        "logs": {
            "group": LOG_GROUP,
            "service": SERVICE,
            "from": iso(logs_from),
            "to": iso(last),
            "events": len(events),
            "passes": passes,
            "note": "Lines without their EMF _aws block, as the log tool shows them.",
            "file": "logs.jsonl.gz",
        },
        "metrics": record_metrics(session, floor(logs_from), ceil(last)),
        "deploys": {
            "table": HISTORY_TABLE,
            "service": SERVICE,
            "to": iso_ms(last),
            "items": record_deploys(session.client("dynamodb", config=config), iso_ms(last)),
        },
        "alarms": record_alarms(cloudwatch),
        "alarm_history": {
            "from": iso(logs_from),
            "to": iso(history_to),
            "items": record_history(cloudwatch, logs_from, history_to),
        },
        "checks": [],
    }
    print(
        f"logs: {len(events)} lines in {passes} passes; metrics: "
        f"{sum(len(s['points']) for s in window['metrics']['series'])} points; deploys: "
        f"{len(window['deploys']['items'])}; alarm changes: {len(window['alarm_history']['items'])}"
    )

    found = scrub(window, account) + scrub(events, account)
    for where, count in sorted(found.items()):
        print(f"account ID replaced: {count} in {where}")
    window_text, logs_bytes = serialize(window), compress(events)
    if account in window_text or account.encode() in gzip.decompress(logs_bytes):
        raise SystemExit("The account ID is still in the recording; nothing written.")

    folder = WINDOWS / args.name
    staging = folder.with_name(folder.name + ".new")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    (staging / "window.json").write_text(window_text, encoding="utf-8", newline="\n")
    (staging / "logs.jsonl.gz").write_bytes(logs_bytes)

    window["checks"], mismatched = check(session, Window(staging.name, root=staging.parent))
    window_text = serialize(window)
    if account in window_text:
        raise SystemExit("The account ID is in a check; nothing written.")
    (staging / "window.json").write_text(window_text, encoding="utf-8", newline="\n")
    if mismatched:
        print(f"{mismatched} checks never matched the live stack; left in {staging.name}, not saved.")
        return 1
    shutil.rmtree(folder, ignore_errors=True)
    staging.rename(folder)
    print(f"saved: window.json {len(window_text.encode()):,} bytes, logs.jsonl.gz {len(logs_bytes):,} bytes")
    return 0


def find_replays(cloudwatch: Any, start: datetime, end: datetime) -> list[dict[str, str]]:
    replays = []
    for item in record_history(cloudwatch, start, end):
        if json.loads(item["HistoryData"])["newState"]["stateValue"] == "ALARM":
            replays.append({"alarm": item["AlarmName"], "fired_at": item["Timestamp"]})
    return replays


def record_history(cloudwatch: Any, start: datetime, end: datetime) -> list[dict[str, Any]]:
    items, request = [], {
        "HistoryItemType": "StateUpdate",
        "StartDate": start,
        "EndDate": end,
        "ScanBy": "TimestampAscending",
        "MaxRecords": 100,
    }
    while True:
        response = cloudwatch.describe_alarm_history(**request)
        for item in response["AlarmHistoryItems"]:
            if item["AlarmName"].startswith(ALARM_PREFIX):
                kept = {k: item[k] for k in HISTORY_FIELDS if k in item}
                kept["Timestamp"] = iso(item["Timestamp"])
                items.append(kept)
        if not response.get("NextToken"):
            return sorted(items, key=lambda i: i["Timestamp"])
        request["NextToken"] = response["NextToken"]


def record_alarms(cloudwatch: Any) -> list[dict[str, Any]]:
    alarms = cloudwatch.describe_alarms(AlarmNamePrefix=ALARM_PREFIX, AlarmTypes=["MetricAlarm"])
    return [{k: a[k] for k in ALARM_FIELDS if k in a} for a in alarms["MetricAlarms"]]


def record_logs(client: Any, start_ms: int, end_ms: int) -> tuple[list[dict[str, Any]], list[int]]:
    """Every checkout-api line in [start_ms, end_ms], read in two passes or three if they disagree."""
    union: dict[str, dict[str, Any]] = {}
    passes: list[int] = []
    while len(passes) < 2 or (len(passes) == 2 and passes[0] != passes[1]):
        found: dict[str, dict[str, Any]] = {}
        for low in range(start_ms, end_ms + 1, CHUNK_MS):
            request = {
                "logGroupName": LOG_GROUP,
                "startTime": low,
                "endTime": min(low + CHUNK_MS - 1, end_ms),
                "filterPattern": logs.filter_pattern(SERVICE, errors_only=False, route=None),
            }
            while True:
                response = client.filter_log_events(**request)
                for event in response.get("events", []):
                    found[event["eventId"]] = event
                if not response.get("nextToken"):
                    break
                request["nextToken"] = response["nextToken"]
        passes.append(len(found))
        union.update(found)
    events = [
        {
            "timestamp": e["timestamp"],
            "ingestionTime": e["ingestionTime"],
            "eventId": e["eventId"],
            "logStreamName": e["logStreamName"],
            "message": without_emf(e["message"]),
        }
        for e in union.values()
    ]
    return sorted(events, key=lambda e: (e["timestamp"], e["eventId"])), passes


def without_emf(message: str) -> str:
    try:
        record = json.loads(message)
    except ValueError:
        return message
    if not isinstance(record, dict):
        return message
    record.pop("_aws", None)
    return json.dumps(record, separators=(",", ":"))


def record_metrics(session: Any, start: datetime, end: datetime) -> dict[str, Any]:
    """Every column the metrics tool shows, read with the tool's own query."""
    env = ToolEnv.from_session(session)
    series = metrics._query(env, start, end, PERIOD)
    return {
        "namespace": NAMESPACE,
        "period": PERIOD,
        "from": iso(start),
        "to": iso(end),
        "note": "CloudWatch keeps 10-second data for 3 hours, so only 60-second periods remain.",
        "series": [
            {
                "metric": c.metric,
                "stat": c.stat,
                "dimensions": {"dependency": c.dependency} if c.dependency else {},
                "points": [[iso(t), v] for t, v in sorted(series[c.id].items())],
            }
            for c in metrics.COLUMNS
        ],
    }


def record_deploys(client: Any, to: str) -> list[dict[str, Any]]:
    items, request = [], {
        "TableName": HISTORY_TABLE,
        "KeyConditionExpression": "#service = :service AND #ts <= :to",
        "ExpressionAttributeNames": {"#service": "service", "#ts": "ts"},
        "ExpressionAttributeValues": {":service": {"S": SERVICE}, ":to": {"S": to}},
        "ScanIndexForward": False,
    }
    while True:
        response = client.query(**request)
        items += response["Items"]
        if "LastEvaluatedKey" not in response:
            return items
        request["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def check(session: Any, window: Window) -> tuple[list[dict[str, Any]], int]:
    checks, mismatched = [], 0
    for replay in window.replays:
        now = replay.fired_at
        live_env = ToolEnv.from_session(session, now=lambda now=now: now)
        fake_env = window.env(now)
        matched, retried = 0, 0
        for tool, args in grid(window, now):
            fake = TOOLS[tool](fake_env, **args)
            for tries in range(1, LIVE_TRIES + 1):
                live = TOOLS[tool](live_env, **args)
                if live == fake:
                    break
                time.sleep(2)
            same = live == fake
            matched += same
            retried += same and tries > 1
            if not same:
                mismatched += 1
                print(f"  differs: {tool} {args}\n    live: {first_line(live)}\n    recorded: {first_line(fake)}")
            checks.append(
                {
                    "alarm": replay.alarm,
                    "now": iso(now),
                    "tool": tool,
                    "args": args,
                    "live_tries": tries,
                    "chars": len(fake),
                    "first_line": first_line(fake),
                    "sha256": hashlib.sha256(fake.encode("utf-8")).hexdigest(),
                }
            )
            time.sleep(0.2)
        total = sum(1 for c in checks if c["now"] == iso(now))
        print(f"{replay.alarm} at {iso(now)}: {matched} of {total} calls match live ({retried} on a retry)")
    return checks, mismatched


def grid(window: Window, now: datetime) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = [
        ("get_metrics", {"minutes": minutes, "period_seconds": period})
        for minutes, period in ((10, 10), (30, 60), (90, 60), (180, 60))
    ]
    for errors_only in (True, False):
        for route in (None, *logs.ROUTES):
            for minutes, limit in ((10, 20), (180, 200)):
                args = {"minutes": minutes, "errors_only": errors_only, "route": route, "limit": limit}
                calls.append(("get_recent_logs", args))
    visible = sum(1 for i in window.meta["deploys"]["items"] if i["ts"]["S"] <= iso_ms(now))
    for limit in sorted({1, 4, 10, deploys.MAX_RECORDS} | ({visible} if visible <= deploys.MAX_RECORDS else set())):
        calls.append(("get_deploy_history", {"limit": limit}))
    return calls


def scrub(value: Any, account: str, where: str = "", found: Counter | None = None) -> Counter:
    """Replace the account ID in every string, in place, counting where it was."""
    found = Counter() if found is None else found
    items = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
    for key, item in items:
        path = f"{where}.{key}" if isinstance(key, str) else where
        if isinstance(item, str):
            if account in item:
                found[path.lstrip(".")] += item.count(account)
                value[key] = item.replace(account, PLACEHOLDER_ACCOUNT)
        elif isinstance(item, dict | list):
            scrub(item, account, path, found)
    return found


def serialize(window: dict[str, Any]) -> str:
    return json.dumps(window, indent=1) + "\n"


def compress(events: list[dict[str, Any]]) -> bytes:
    text = "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in events)
    buffer = io.BytesIO()
    # mtime 0: the same lines always make the same bytes.
    with gzip.GzipFile(fileobj=buffer, mode="wb", compresslevel=9, mtime=0) as f:
        f.write(text.encode("utf-8"))
    return buffer.getvalue()


def first_line(text: str) -> str:
    return text.split("\n", 1)[0]


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def floor(moment: datetime) -> datetime:
    return datetime.fromtimestamp(int(moment.timestamp()) // PERIOD * PERIOD, UTC)


def ceil(moment: datetime) -> datetime:
    down = floor(moment)
    return down if down == moment else down + timedelta(seconds=PERIOD)


if __name__ == "__main__":
    sys.exit(main())
