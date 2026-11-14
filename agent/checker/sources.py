"""A run's tool results as the sources its evidence cites, by label, read back into data.

Each result carries the label the agent's `sources` module gave it (m1, l2,
d1). Its marks come off with `sources.unmark`, and the tools' own parsers read
the text that's left. The alarm message is the source `alarm`: the datapoints
in CloudWatch's reason.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sentinelops_tools.deploys import DeployPage, parse_history
from sentinelops_tools.logs import LogsPage, parse_lines
from sentinelops_tools.metrics import MetricsPage, parse_table
from sources import ALARM, unmark

BUDGET = re.compile(r"\[budget: .*\]$")
DATAPOINT = re.compile(r"([\d.]+) \((\d\d)/(\d\d)/(\d\d) (\d\d:\d\d:\d\d)\)")
_CLOCK = re.compile(r"(\d\d):(\d\d)(?::(\d\d)(?:\.(\d{1,6}))?)?Z?")


@dataclass
class Source:
    label: str
    tool: str
    text: str  # as the tool wrote it, marks off
    metrics: MetricsPage | None = None
    logs: LogsPage | None = None
    deploys: DeployPage | None = None
    # A log line's number, as the result gave it, to its index in logs.lines.
    line_index: dict[int, int] = field(default_factory=dict)
    # The alarm's datapoints: their times and values.
    points: list[tuple[datetime, float]] = field(default_factory=list)


@dataclass
class RunSources:
    fired_at: datetime
    by_label: dict[str, Source] = field(default_factory=dict)
    unlabeled: list[str] = field(default_factory=list)  # results with no label: a tool's error, or an old run

    def moment(self, text: str) -> tuple[datetime, timedelta]:
        """A cited time as UTC, with how much its text leaves open (a minute for 18:59, a second for 18:59:00).

        A time of day falls on the alarm's day, or the day before when that would
        put it more than an hour after the alarm. Raises ValueError for anything else.
        """
        if "T" in text:
            day, clock_text = text.split("T", 1)
            base = datetime.fromisoformat(day).replace(tzinfo=UTC)
        else:
            clock_text = text
            base = self.fired_at.replace(hour=0, minute=0, second=0, microsecond=0)
        found = _CLOCK.fullmatch(clock_text)
        if not found:
            raise ValueError(f"not a time: {text!r}")
        h, m, s, frac = found.groups()
        if int(h) > 23 or int(m) > 59 or int(s or 0) > 59:
            raise ValueError(f"not a time: {text!r}")
        moment = base + timedelta(hours=int(h), minutes=int(m), seconds=int(s or 0))
        if frac:
            moment += timedelta(microseconds=int(frac.ljust(6, "0")))
        width = (
            timedelta(minutes=1) if s is None
            else timedelta(seconds=1) if not frac
            else timedelta(microseconds=10 ** (6 - len(frac)))
        )
        if "T" not in text and moment > self.fired_at + timedelta(hours=1):
            moment -= timedelta(days=1)
        return moment, width


def read_sources(transcript: dict[str, Any]) -> RunSources:
    alarm = transcript["alarm"]
    run = RunSources(_iso(alarm["fired_at"]))
    points = [
        (datetime.fromisoformat(f"20{year}-{month}-{day}T{clock_text}+00:00"), float(value))
        for value, day, month, year, clock_text in DATAPOINT.findall(alarm.get("reason", ""))
    ]
    run.by_label[ALARM] = Source(ALARM, "alarm", alarm.get("reason", ""), points=sorted(points))
    for tool, text in tool_results(transcript):
        label, plain, numbers = unmark(text)
        if label is None:
            run.unlabeled.append(text[:200])
            continue
        source = Source(label, tool, plain, line_index=numbers)
        if tool == "get_metrics":
            source.metrics = parse_table(plain)
        elif tool == "get_recent_logs":
            source.logs = parse_lines(plain)
        elif tool == "get_deploy_history":
            source.deploys = parse_history(plain)
        run.by_label[label] = source
    return run


def tool_results(transcript: dict[str, Any]) -> list[tuple[str, str]]:
    """Each tool call's name and its result's text, in order, without the guard's budget line."""
    names: dict[str, str] = {}
    results = []
    for message in transcript.get("messages", []):
        for block in message.get("content", []):
            if "toolUse" in block:
                names[block["toolUse"]["toolUseId"]] = block["toolUse"]["name"]
            elif "toolResult" in block:
                result = block["toolResult"]
                name = names.get(result["toolUseId"], "")
                if name == "Hypothesis":
                    continue
                lines = "".join(c.get("text", "") for c in result.get("content", [])).split("\n")
                while lines and (not lines[-1].strip() or BUDGET.match(lines[-1].strip())):
                    lines.pop()
                results.append((name, "\n".join(lines)))
    return results


def line_time(record: dict[str, Any] | str) -> datetime | None:
    if isinstance(record, dict) and isinstance(record.get("timestamp"), str):
        return _iso(record["timestamp"])
    return None


def _iso(text: str) -> datetime:
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def as_json(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
