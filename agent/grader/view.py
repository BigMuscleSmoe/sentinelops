"""What a run saw: its alarm and every tool result, parsed back into data.

The grader checks a Hypothesis against this view and nothing else. It reads the
text the model read, through the parsers beside each renderer, so it grades
saved transcripts and runs through Gateway alike.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sentinelops_tools.deploys import DeployPage, parse_history
from sentinelops_tools.logs import LogsPage, parse_lines
from sentinelops_tools.metrics import MetricsPage, parse_table

ERR = "err%"
BUDGET = re.compile(r"^\[budget: .*\]$")
# CloudWatch's alarm reason: [0.17 (12/11/26 20:00:20), ...], day/month/year.
DATAPOINT = re.compile(r"(\d+(?:\.\d+)?(?:E-?\d+)?) \((\d\d)/(\d\d)/(\d\d) (\d\d:\d\d:\d\d)\)")


@dataclass(frozen=True)
class Row:
    """One metrics row, or one of the alarm's datapoints (err% only)."""

    start: datetime
    period_s: int
    cells: dict[str, float | None]
    unsettled: bool
    source: str  # "metrics" or "alarm"

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.period_s)


@dataclass(frozen=True)
class Line:
    at: datetime
    record: dict[str, Any]

    @property
    def failed(self) -> bool:
        return _num(self.record.get("status")) >= 500 or self.record.get("error") == 1


@dataclass(frozen=True)
class Page:
    """A log page: the lines it showed and the span it can speak for."""

    page: LogsPage
    lines: list[Line]
    searched_from: datetime
    searched_to: datetime

    @property
    def speaks_from(self) -> datetime:
        """Where the page's evidence starts: its oldest shown line if it was cut, else its search's start."""
        if self.page.cut and self.lines:
            return self.lines[0].at
        return self.searched_from


@dataclass
class RunView:
    alarm_at: datetime
    alarm_text: str
    texts: list[str] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)
    tables: list[list[Row]] = field(default_factory=list)
    metrics_spans: list[tuple[datetime, datetime]] = field(default_factory=list)
    pages: list[Page] = field(default_factory=list)
    deploy_pages: list[DeployPage] = field(default_factory=list)
    deploys: list[dict[str, Any]] = field(default_factory=list)
    unread: list[str] = field(default_factory=list)  # tool results no parser took

    def at(self, clock_text: str) -> datetime:
        """HH:MM[:SS[.fff]] as a UTC datetime on the alarm's day, or the day before if it would be later."""
        parts = clock_text.split(":")
        h, m = int(parts[0]), int(parts[1])
        s = float(parts[2]) if len(parts) > 2 else 0.0
        base = self.alarm_at.replace(hour=0, minute=0, second=0, microsecond=0)
        moment = base + timedelta(hours=h, minutes=m, seconds=s)
        return moment - timedelta(days=1) if moment > self.alarm_at + timedelta(hours=1) else moment

    @property
    def lines(self) -> list[Line]:
        """Every log line the run saw, once each, oldest first."""
        seen: dict[str, Line] = {}
        for page in self.pages:
            for line in page.lines:
                seen.setdefault(line.record.get("request_id") or f"{line.at}{len(seen)}", line)
        return sorted(seen.values(), key=lambda line: line.at)

    @property
    def deploys_from(self) -> datetime | None:
        """The oldest time the deploy history speaks for: None if it was never read."""
        if not self.deploy_pages:
            return None
        if any(not p.older_exist for p in self.deploy_pages):
            return datetime.min.replace(tzinfo=UTC)
        times = [_iso(r["ts"]) for p in self.deploy_pages for r in p.records if "ts" in r]
        return min(times) if times else None


def view_of(transcript: dict[str, Any]) -> RunView:
    alarm = transcript["alarm"]
    run = RunView(_iso(alarm["fired_at"]), json.dumps(alarm))
    run.texts.append(run.alarm_text)
    for value, day, month, year, clock_text in DATAPOINT.findall(alarm.get("reason", "")):
        start = datetime.fromisoformat(f"20{year}-{month}-{day}T{clock_text}+00:00")
        run.rows.append(Row(start, 10, {ERR: float(value) * 100}, False, "alarm"))
    for name, text in tool_results(transcript):
        run.texts.append(text)
        if name == "get_metrics" and (table := parse_table(text)):
            _add_metrics(run, table)
        elif name == "get_recent_logs" and (logs := parse_lines(text)):
            _add_logs(run, logs)
        elif name == "get_deploy_history" and (history := parse_history(text)):
            run.deploy_pages.append(history)
        else:
            run.unread.append(text[:200])
    seen = set()
    for page in run.deploy_pages:
        for record in page.records:
            key = json.dumps(record, sort_keys=True)
            if key not in seen:
                seen.add(key)
                run.deploys.append(record)
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
                text = "".join(c.get("text", "") for c in result.get("content", []))
                lines = text.split("\n")
                while lines and (not lines[-1].strip() or BUDGET.match(lines[-1].strip())):
                    lines.pop()
                results.append((name, "\n".join(lines)))
    return results


def _add_metrics(run: RunView, table: MetricsPage) -> None:
    start, end = run.at(table.start), run.at(table.end)
    if end < start:
        start -= timedelta(days=1)
    run.metrics_spans.append((start, end))
    rows = []
    for row in table.rows:
        moment = run.at(row.time)
        if moment < start - timedelta(seconds=1):
            moment += timedelta(days=1)
        rows.append(Row(moment, table.period_s or 60, row.cells, row.unsettled, "metrics"))
    run.tables.append(rows)
    run.rows.extend(rows)


def _add_logs(run: RunView, logs: LogsPage) -> None:
    lines = [Line(_iso(r["timestamp"]), r) for r in logs.lines if isinstance(r, dict) and "timestamp" in r]
    searched_from, searched_to = run.at(logs.searched_from), run.at(logs.searched_to)
    if searched_from > searched_to:
        searched_from -= timedelta(days=1)
    run.pages.append(Page(logs, lines, searched_from, searched_to + timedelta(seconds=1)))


def _iso(text: str) -> datetime:
    """An ISO time as UTC; one without an offset is UTC too, never this machine's local time."""
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
