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

A resolved item carries the entries it's about (its scope: the cited lines, the
rows or lines in its window, or the whole result; a value item's one target),
so the checks per kind (`checks`) read the cited data, never the item's words.
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


@dataclass(frozen=True)
class Entry:
    """One row, line, record, or datapoint of a result, and what it shows."""

    name: str  # m1 19:01:00, l1[3], d1 2026-10-27T18:56:06.039Z, alarm 19:05:30
    time: datetime | None  # None: a log line cut short or not JSON
    period: timedelta  # a metric row's period; zero for a point in time
    fields: dict[str, Any] | None  # by column or field; None where the checker can't read the line


@dataclass
class Resolution:
    id: str  # e1 is the first evidence item
    kind: str
    source: str | None
    status: Status
    reason: str = ""
    points_at: list[str] = field(default_factory=list)  # the rows, lines, records, or datapoints it names
    scope: list[Entry] = field(default_factory=list)  # what the item states something about
    # The window up to its end's instant, where the end is stated coarser than the
    # entries it bounds ("to 19:05:23" beside a line at 19:05:23.956): None when the same.
    short_scope: list[Entry] | None = None
    target: Entry | None = None  # a value item's one row, line, record, or datapoint
    result: Source | None = None  # the cited result


@dataclass(frozen=True)
class Span:
    """What a result covered: from start, up to but not including end."""

    start: datetime | None  # None: open, as a deploy read with no older records
    end: datetime
    text: str  # as the result states it

    def holds(self, moment: datetime, width: timedelta = timedelta(0)) -> bool:
        return (self.start is None or moment + width > self.start) and moment < self.end


# The kinds each source can state. Deploy records hold no numbers, so no range.
_FITS = {
    "m": {"value", "range", "all", "count", "first", "last", "none", "gap"},
    "l": {"value", "range", "all", "count", "first", "last", "none", "gap"},
    "d": {"value", "all", "count", "first", "last", "none", "gap"},
    ALARM: {"value", "range", "all", "count", "first", "last", "none", "gap"},
}
# A read of nothing can still be counted (0) or said to hold nothing.
_EMPTY_OK = {"count", "none"}


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
    out.result = source
    prefix = ALARM if label == ALARM else label[0]
    if kind not in _FITS.get(prefix, set()):
        return fail(f"a {kind} item doesn't fit a {source.tool} result")
    if kind == "gap":
        return out
    empty = _empty(source)
    if empty and kind not in _EMPTY_OK:
        return fail(f"{label} holds no rows, lines, or records")

    column = item.get("column")
    if column and not empty and not _has_column(source, column):
        return fail(f"{label} has no column or field {column!r}")

    every = entries(source, run)
    span = span_of(source, run)
    lines = item.get("lines") or None
    cited: list[Entry] = []
    if lines is not None:
        if source.logs is None:
            return fail("only log results number their lines")
        missing = [n for n in lines if n not in source.line_index]
        if missing:
            return fail(f"{label} has no line {', '.join(map(str, missing))} (it shows {len(source.line_index)})")
        cited = [every[source.line_index[n]] for n in lines]
        unreadable = [e.name for e in cited if e.fields is None]
        if unreadable:
            return fail(f"{', '.join(unreadable)} is cut short or isn't JSON")
        out.points_at += [e.name for e in cited]
        out.scope = cited

    if "at" in times:
        moment, width = times["at"]
        if not span.holds(moment, width):
            return fail(f"{item['at']} is outside {label}'s read ({span.text})", Status.OUTSIDE_READ)
        hits = _at(every, source, moment, width)
        if kind == "value" and len(cited) != 1:
            if not hits:
                return fail(f"{label} has nothing at {item['at']}")
            if len(hits) > 1:
                return fail(f"{len(hits)} entries at {item['at']} in {label}; cite one")
        out.points_at += [e.name for e in hits if e.name not in out.points_at]
        if kind == "value":
            out.target = cited[0] if len(cited) == 1 else hits[0]

    if "start" in times or "end" in times:
        start = times.get("start", times.get("end"))
        end = times.get("end", start)
        assert start is not None and end is not None
        outside = [
            str(item[key]) for key, (moment, width) in (("start", start), ("end", end))
            if key in times and not span.holds(moment, width)
        ]
        if outside:
            return fail(f"{' and '.join(outside)} outside {label}'s read ({span.text})", Status.OUTSIDE_READ)
        if lines is None:
            out.scope = [e for e in every if e.time is not None and start[0] <= e.time < end[0] + end[1]]
            out.points_at += [e.name for e in out.scope]
            short = [e for e in out.scope if e.time is not None and e.time <= end[0]]
            if len(short) != len(out.scope):
                out.short_scope = short
    elif lines is None:
        out.scope = every  # the whole result
    if kind == "value" and out.target is None and len(cited) == 1:
        out.target = cited[0]
    if out.target is not None:
        out.scope, out.short_scope = [out.target], None
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


def span_of(source: Source, run: RunSources) -> Span:
    """What the result covered. A time it states to the second covers that whole second.

    The log tool states its window to the second, so a line at 18:56:46.522 is
    inside a read "to 18:56:46" (trial 20261114T055051Z read it as outside).
    """

    def through(text: str) -> datetime:
        moment, width = run.moment(text)
        return moment + width

    if source.metrics is not None:
        metrics = source.metrics
        return Span(run.moment(metrics.start)[0], through(metrics.end), f"{metrics.start} to {metrics.end}")
    if source.logs is not None:
        logs = source.logs
        start = run.moment(logs.searched_from)[0]
        start_text = logs.searched_from
        if logs.cut:  # older matches were found but not shown: the read starts at the oldest shown
            shown = [t for t in map(line_time, logs.lines) if t]
            if shown:
                start = min(shown)
                start_text = f"{start:%H:%M:%S.%f}"[:-3]
        return Span(start, through(logs.searched_to), f"{start_text} to {logs.searched_to}")
    if source.deploys is not None:
        deploys = source.deploys
        oldest = min(run.moment(r["ts"])[0] for r in deploys.records) if deploys.older_exist else None
        start_text = f"{oldest:%Y-%m-%dT%H:%M:%S}" if oldest else "the first record"
        return Span(oldest, through(deploys.up_to), f"{start_text} to {deploys.up_to}")
    if not source.points:
        return Span(None, run.fired_at + timedelta(microseconds=1), f"the alarm at {run.fired_at:%H:%M:%S}")
    first, last = source.points[0][0], source.points[-1][0]
    return Span(first, last + timedelta(seconds=1), f"{first:%H:%M:%S} to {last:%H:%M:%S}")


def entries(source: Source, run: RunSources) -> list[Entry]:
    """Each row, line, record, or datapoint, in the result's order. A log line's index is its place in logs.lines."""
    label = source.label
    if source.metrics is not None:
        period = timedelta(seconds=source.metrics.period_s or 0)
        return [
            Entry(f"{label} {r.time}", run.moment(r.time)[0], period, dict(r.cells)) for r in source.metrics.rows
        ]
    if source.logs is not None:
        number = {i: n for n, i in source.line_index.items()}
        out = []
        for i, line in enumerate(source.logs.lines):
            readable = isinstance(line, dict)
            out.append(
                Entry(
                    f"{label}[{number.get(i, i + 1)}]",
                    line_time(line) if readable else None,
                    timedelta(0),
                    line if readable else None,
                )
            )
        return out
    if source.deploys is not None:
        out = []
        for record in source.deploys.records:
            changes = record.get("changes") or []
            # Each changed key is a field of its own, old->new; changes lists them as the result shows them.
            fields = {k: v for k, v in record.items() if k != "changes"}
            fields["changes"] = [f"{c['key']} {c['old']}->{c['new']}" for c in changes]
            for change in changes:
                fields[change["key"]] = f"{change['old']}->{change['new']}"
            out.append(Entry(f"{label} {record['ts']}", run.moment(record["ts"])[0], timedelta(0), fields))
        return out
    return [Entry(f"alarm {t:%H:%M:%S}", t, timedelta(0), {ALARM: value}) for t, value in source.points]


def _at(every: list[Entry], source: Source, moment: datetime, width: timedelta) -> list[Entry]:
    if source.metrics is not None:
        # A row by its start, or the one row whose period holds the time.
        starts = [e for e in every if e.time == moment]
        return starts or [e for e in every if e.time is not None and e.time <= moment < e.time + e.period]
    return [e for e in every if e.time is not None and moment <= e.time < moment + width]
