"""What each typed evidence item points at in its cited result, or why it points at nothing.

Three outcomes per item:
- resolved: the source exists, the kind fits it, and every row, line, record,
  or window it names is there.
- outside_read: it names a time the cited result never covered: a claim about
  a period the agent never read, the strict slip (BUILD_PLAN.md, Phase 3,
  step 2). The citation is followed exactly, so it resolves as a format; it's
  counted apart because it's also a slip caught.
- unresolved: it can't be followed (an unknown source or column, a kind that
  doesn't fit the source, a row, line, or record that isn't there, a time that
  doesn't parse). These are what the trial counts as the format failing.

Whether the stated values hold is step 4's; this only finds what they're about.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from checker.sources import RunSources, Source, line_time
from sentinelops_tools.metrics import HEADERS
from sources import ALARM


class Status(StrEnum):
    RESOLVED = "resolved"
    OUTSIDE_READ = "outside_read"
    UNRESOLVED = "unresolved"
    UNTYPED = "untyped"  # a sentence, from before typed evidence


@dataclass
class Resolution:
    id: str  # e1 is the first evidence item
    kind: str
    source: str | None
    status: Status
    reason: str = ""
    points_at: list[str] = field(default_factory=list)  # the rows, lines, records, or datapoints it names


@dataclass
class _Span:
    start: datetime | None  # None: open, as a deploy read with no older records
    end: datetime

    def holds(self, moment: datetime, width: timedelta = timedelta(0)) -> bool:
        return (self.start is None or moment + width > self.start) and moment <= self.end


# The kinds each source can state. Deploy records hold no numbers, so no range.
_FITS = {
    "m": {"value", "range", "all", "count", "first", "last", "none", "gap"},
    "l": {"value", "range", "all", "count", "first", "last", "none", "gap"},
    "d": {"value", "all", "count", "first", "last", "none", "gap"},
    ALARM: {"value", "range", "all", "count", "first", "last", "none", "gap"},
}


def resolve_all(hypothesis: dict[str, Any] | None, run: RunSources) -> list[Resolution]:
    if not hypothesis:
        return []
    return [resolve(f"e{i}", item, run) for i, item in enumerate(hypothesis.get("evidence") or [], 1)]


def resolve(item_id: str, item: dict[str, Any] | str, run: RunSources) -> Resolution:
    if isinstance(item, str):
        return Resolution(item_id, "", None, Status.UNTYPED, "a sentence, not a typed item")
    kind = str(item.get("kind"))
    label = item.get("source")
    out = Resolution(item_id, kind, label, Status.RESOLVED)

    def fail(reason: str, status: Status = Status.UNRESOLVED) -> Resolution:
        out.status, out.reason = status, reason
        return out

    times: dict[str, tuple[datetime, timedelta]] = {}
    for key in ("at", "start", "end"):
        if item.get(key):
            try:
                times[key] = run.moment(str(item[key]))
            except ValueError:
                return fail(f"{key} {item[key]!r} isn't a time")
    if "start" in times and "end" in times and times["start"][0] > times["end"][0]:
        return fail("start is after end")

    if kind == "gap" and not label:
        return out
    if not label:
        return fail("no source")
    source = run.by_label.get(label)
    if source is None:
        if run.unlabeled and len(run.by_label) == 1:
            return fail("the run's results carry no labels")
        return fail(f"no result labeled {label}")
    if label != ALARM and source.metrics is None and source.logs is None and source.deploys is None:
        return fail(f"{label} is a tool's error or a result the checker can't read")
    prefix = ALARM if label == ALARM else label[0]
    if kind not in _FITS.get(prefix, set()):
        return fail(f"a {kind} item doesn't fit a {source.tool} result")
    if kind == "gap":
        return out
    empty = _empty(source)
    if empty and kind != "none":
        return fail(f"{label} holds no rows, lines, or records")

    column = item.get("column")
    if column and not empty and not _has_column(source, column):
        return fail(f"{label} has no column or field {column!r}")

    lines = item.get("lines")
    if lines is not None:
        if source.logs is None:
            return fail("only log results number their lines")
        missing = [n for n in lines if n not in source.line_index]
        if missing:
            return fail(f"{label} has no line {', '.join(map(str, missing))} (it shows {len(source.line_index)})")
        out.points_at += [f"{label}[{n}]" for n in lines]

    span = _span(source, run)
    if "at" in times:
        moment, width = times["at"]
        if not span.holds(moment, width):
            return fail(f"{item['at']} is outside {label}'s read ({_show(span)})", Status.OUTSIDE_READ)
        hits = _at(source, run, moment, width)
        if not hits:
            return fail(f"{label} has nothing at {item['at']}")
        if kind == "value" and len(hits) > 1:
            return fail(f"{len(hits)} entries at {item['at']} in {label}; cite one")
        out.points_at += hits
    if "start" in times or "end" in times:
        start = times.get("start", times.get("end"))
        end = times.get("end", start)
        assert start is not None and end is not None
        outside = [
            str(item[key]) for key, (moment, width) in (("start", start), ("end", end))
            if key in times and not span.holds(moment, width)
        ]
        if outside:
            return fail(f"{' and '.join(outside)} outside {label}'s read ({_show(span)})", Status.OUTSIDE_READ)
        out.points_at += _window(source, run, start[0], end[0] + end[1])
    return out


def _empty(source: Source) -> bool:
    if source.metrics is not None:
        return not source.metrics.rows
    if source.logs is not None:
        return not source.logs.lines
    if source.deploys is not None:
        return not source.deploys.records
    return not source.points


def _has_column(source: Source, column: str) -> bool:
    if source.metrics is not None:
        return column in HEADERS
    if source.logs is not None:
        return any(isinstance(line, dict) and column in line for line in source.logs.lines)
    if source.deploys is not None:
        return any(
            column in record or any(c.get("key") == column for c in record.get("changes") or [])
            for record in source.deploys.records
        )
    return True  # the alarm's one metric, however it's named


def _span(source: Source, run: RunSources) -> _Span:
    if source.metrics is not None:
        return _Span(run.moment(source.metrics.start)[0], run.moment(source.metrics.end)[0])
    if source.logs is not None:
        logs = source.logs
        start = run.moment(logs.searched_from)[0]
        if logs.cut:  # older matches were found but not shown: the read starts at the oldest shown
            shown = [t for t in map(line_time, logs.lines) if t]
            start = min(shown) if shown else start
        return _Span(start, run.moment(logs.searched_to)[0])
    if source.deploys is not None:
        deploys = source.deploys
        oldest = min(run.moment(r["ts"])[0] for r in deploys.records) if deploys.older_exist else None
        return _Span(oldest, run.moment(deploys.up_to)[0])
    if not source.points:
        return _Span(None, run.fired_at)
    return _Span(source.points[0][0], source.points[-1][0])


def _show(span: _Span) -> str:
    start = span.start.strftime("%H:%M:%S") if span.start else "the first record"
    return f"{start} to {span.end.strftime('%H:%M:%S')}"


def _entries(source: Source, run: RunSources) -> list[tuple[datetime, timedelta, str]]:
    """Each row, line, record, or datapoint: its time, how long it covers, and its name."""
    if source.metrics is not None:
        period = timedelta(seconds=source.metrics.period_s or 0)
        return [(run.moment(r.time)[0], period, f"{source.label} {r.time}") for r in source.metrics.rows]
    if source.logs is not None:
        by_index = {i: n for n, i in source.line_index.items()}
        return [
            (t, timedelta(0), f"{source.label}[{by_index.get(i, i + 1)}]")
            for i, line in enumerate(source.logs.lines)
            if (t := line_time(line))
        ]
    if source.deploys is not None:
        return [(run.moment(r["ts"])[0], timedelta(0), f"{source.label} {r['ts']}") for r in source.deploys.records]
    return [(t, timedelta(0), f"alarm {t:%H:%M:%S}") for t, _ in source.points]


def _at(source: Source, run: RunSources, moment: datetime, width: timedelta) -> list[str]:
    entries = _entries(source, run)
    if source.metrics is not None:
        # A row by its start, or the one row whose period holds the time.
        starts = [name for t, _, name in entries if t == moment]
        return starts or [name for t, period, name in entries if t <= moment < t + period]
    return [name for t, _, name in entries if moment <= t < moment + width]


def _window(source: Source, run: RunSources, start: datetime, end: datetime) -> list[str]:
    return [name for t, _, name in _entries(source, run) if start <= t < end]
