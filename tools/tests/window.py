"""Recorded windows of the live stack, served without AWS.

record_window.py records a window before CloudWatch drops it: checkout-api's
request log lines, its 60-second metrics, its deploy history, and its alarms
with their state changes. A Window answers the calls the tools and the agent's
alarm make the way AWS answered them, so any alarm that fired in the window can
be replayed offline, long after the logs' 7-day retention.

Each window also holds checks: tool calls made against the live stack and
against the recording when it was made, with what they returned. test_windows.py
replays them, so these fakes can't drift from what AWS did.
"""

import gzip
import json
import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sentinelops_tools.env import ToolEnv

WINDOWS = Path(__file__).parent / "fixtures" / "windows"
# Stands in for the account ID wherever AWS wrote it.
PLACEHOLDER_ACCOUNT = "123456789012"
# A FilterLogEvents page holds at most 10,000 events or 1 MB.
PAGE_EVENTS = 10_000
PAGE_BYTES = 1_048_576
_TERM = re.compile(r'\$\.(\w+) = (?:"([^"]*)"|(-?\d+))')


class OutsideWindow(Exception):
    """A call asked for something the recording doesn't hold."""


@dataclass(frozen=True)
class Replay:
    alarm: str
    fired_at: datetime


class Window:
    """One recorded window, from fixtures/windows/<name>/."""

    def __init__(self, name: str, root: Path = WINDOWS) -> None:
        folder = root / name
        self.name = name
        self.meta = json.loads((folder / "window.json").read_text(encoding="utf-8"))
        self.recorded_at = parse_time(self.meta["recorded_at"])
        self.replays = [Replay(r["alarm"], parse_time(r["fired_at"])) for r in self.meta["replays"]]
        with gzip.open(folder / self.meta["logs"]["file"], "rt", encoding="utf-8") as f:
            self.events = [json.loads(line) for line in f]
        self.stamps = [e["timestamp"] for e in self.events]
        self.records = [_record(e["message"]) for e in self.events]

    def env(self, now: datetime) -> ToolEnv:
        """The tools' view at `now`, with the clock CloudWatch's retention was judged by when recorded."""
        return ToolEnv(
            logs=FakeLogs(self),
            cloudwatch=FakeCloudWatch(self),
            dynamodb=FakeDynamoDB(self),
            now=lambda: now,
            wall_clock=lambda: self.recorded_at,
        )

    def client(self, service: str, **_: Any) -> Any:
        """Stands in for boto3.Session.client, for a whole run's clients."""
        fakes = {"logs": FakeLogs, "cloudwatch": FakeCloudWatch, "dynamodb": FakeDynamoDB}
        return fakes[service](self)


class FakeLogs:
    def __init__(self, window: Window) -> None:
        self.window = window
        self.meta = window.meta["logs"]

    def filter_log_events(
        self,
        *,
        logGroupName: str,
        startTime: int,
        endTime: int,
        filterPattern: str,
        nextToken: str | None = None,
    ) -> dict[str, Any]:
        if logGroupName != self.meta["group"]:
            raise OutsideWindow(f"only {self.meta['group']} is recorded")
        if startTime < to_ms(parse_time(self.meta["from"])) or endTime > to_ms(parse_time(self.meta["to"])):
            raise OutsideWindow(f"logs are recorded from {self.meta['from']} to {self.meta['to']}")
        matches = _pattern(filterPattern, self.meta["service"])
        w = self.window
        span = range(bisect_left(w.stamps, startTime), bisect_right(w.stamps, endTime))
        hits = [w.events[i] for i in span if matches(w.records[i])]
        start = int(nextToken or 0)
        page, size = [], 0
        for event in hits[start:]:
            size += len(event["message"].encode("utf-8"))
            if page and (len(page) == PAGE_EVENTS or size > PAGE_BYTES):
                break
            page.append(event)
        response: dict[str, Any] = {"events": page}
        if start + len(page) < len(hits):
            response["nextToken"] = str(start + len(page))
        return response


def _pattern(pattern: str, service: str) -> Any:
    """A matcher for the JSON patterns the log tool builds: `{ $.a = "x" && $.b = 1 }`."""
    body = pattern.strip()
    if not (body.startswith("{") and body.endswith("}")):
        raise OutsideWindow(f"not a JSON filter pattern: {pattern}")
    terms = []
    for term in body[1:-1].split("&&"):
        found = _TERM.fullmatch(term.strip())
        if found is None:
            raise OutsideWindow(f"unsupported term {term.strip()!r}")
        field, text, number = found.groups()
        terms.append((field, text if text is not None else int(number)))
    if ("service", service) not in terms:
        raise OutsideWindow(f"only {service}'s lines are recorded")

    def matches(record: dict[str, Any] | None) -> bool:
        if record is None:
            return False
        for field, want in terms:
            value = record.get(field)
            if isinstance(want, int):
                if isinstance(value, bool) or not isinstance(value, int | float) or value != want:
                    return False
            elif value != want:
                return False
        return True

    return matches


class FakeCloudWatch:
    def __init__(self, window: Window) -> None:
        self.window = window
        metrics = window.meta["metrics"]
        self.metrics = metrics
        self.series = {
            (s["metric"], tuple(sorted(s["dimensions"].items())), s["stat"]): [
                (parse_time(t), v) for t, v in s["points"]
            ]
            for s in metrics["series"]
        }

    def get_metric_data(
        self,
        *,
        MetricDataQueries: list[dict[str, Any]],
        StartTime: datetime,
        EndTime: datetime,
        ScanBy: str = "TimestampDescending",
        NextToken: str | None = None,
    ) -> dict[str, Any]:
        if StartTime < parse_time(self.metrics["from"]) or EndTime > parse_time(self.metrics["to"]):
            raise OutsideWindow(f"metrics are recorded from {self.metrics['from']} to {self.metrics['to']}")
        results = []
        for query in MetricDataQueries:
            stat = query["MetricStat"]
            metric = stat["Metric"]
            if metric["Namespace"] != self.metrics["namespace"]:
                raise OutsideWindow(f"only the {self.metrics['namespace']} namespace is recorded")
            if stat["Period"] != self.metrics["period"]:
                raise OutsideWindow(f"only {self.metrics['period']} s periods are recorded")
            dimensions = tuple(sorted((d["Name"], d["Value"]) for d in metric.get("Dimensions", [])))
            key = (metric["MetricName"], dimensions, stat["Stat"])
            if key not in self.series:
                raise OutsideWindow(f"{key} isn't recorded")
            points = [(t, v) for t, v in self.series[key] if StartTime <= t < EndTime]
            if ScanBy == "TimestampDescending":
                points.reverse()
            results.append(
                {
                    "Id": query["Id"],
                    "Label": metric["MetricName"],
                    "Timestamps": [t for t, _ in points],
                    "Values": [v for _, v in points],
                    "StatusCode": "Complete",
                }
            )
        return {"MetricDataResults": results, "Messages": []}

    def describe_alarms(self, *, AlarmNames: list[str], AlarmTypes: list[str]) -> dict[str, Any]:
        alarms = [dict(a) for a in self.window.meta["alarms"] if a["AlarmName"] in AlarmNames]
        return {"MetricAlarms": alarms if "MetricAlarm" in AlarmTypes else []}

    def describe_alarm_history(
        self,
        *,
        AlarmName: str,
        HistoryItemType: str,
        StartDate: datetime,
        EndDate: datetime,
        ScanBy: str = "TimestampDescending",
        MaxRecords: int = 100,
    ) -> dict[str, Any]:
        history = self.window.meta["alarm_history"]
        if EndDate > parse_time(history["to"]):
            raise OutsideWindow(f"alarm history is recorded up to {history['to']}")
        # Earlier changes aren't recorded, so a search that finds none before the
        # window's start can't say there were none.
        items = [
            {**item, "Timestamp": parse_time(item["Timestamp"])}
            for item in history["items"]
            if item["AlarmName"] == AlarmName and item["HistoryItemType"] == HistoryItemType
        ]
        items = [i for i in items if StartDate <= i["Timestamp"] <= EndDate]
        items.sort(key=lambda i: i["Timestamp"], reverse=ScanBy == "TimestampDescending")
        if not items and StartDate < parse_time(history["from"]):
            raise OutsideWindow(f"alarm history is recorded from {history['from']}")
        return {"AlarmHistoryItems": items[:MaxRecords]}


class FakeDynamoDB:
    """The deploy-history query the tool makes, as DynamoDB answers it."""

    QUERY = "#service = :service AND #ts <= :now"

    def __init__(self, window: Window) -> None:
        self.deploys = window.meta["deploys"]

    def query(
        self,
        *,
        TableName: str,
        KeyConditionExpression: str,
        ProjectionExpression: str,
        ExpressionAttributeNames: dict[str, str],
        ExpressionAttributeValues: dict[str, Any],
        ScanIndexForward: bool,
        Limit: int,
    ) -> dict[str, Any]:
        if TableName != self.deploys["table"] or KeyConditionExpression != self.QUERY:
            raise OutsideWindow("only the deploy tool's query is recorded")
        service = ExpressionAttributeValues[":service"]["S"]
        now = ExpressionAttributeValues[":now"]["S"]
        if service != self.deploys["service"] or now > self.deploys["to"]:
            raise OutsideWindow(f"deploys are recorded for {self.deploys['service']} up to {self.deploys['to']}")
        fields = [ExpressionAttributeNames.get(p.strip(), p.strip()) for p in ProjectionExpression.split(",")]
        items = sorted(
            (i for i in self.deploys["items"] if i["ts"]["S"] <= now),
            key=lambda i: i["ts"]["S"],
            reverse=not ScanIndexForward,
        )
        page = [{f: item[f] for f in fields if f in item} for item in items[:Limit]]
        response: dict[str, Any] = {"Items": page, "Count": len(page), "ScannedCount": len(page)}
        # DynamoDB stops at Limit without looking further, so a full page always
        # says there may be more.
        if len(page) == Limit:
            last = items[Limit - 1]
            response["LastEvaluatedKey"] = {"service": last["service"], "ts": last["ts"]}
        return response


def _record(message: str) -> dict[str, Any] | None:
    try:
        record = json.loads(message)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def to_ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)
