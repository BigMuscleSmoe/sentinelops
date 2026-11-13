"""Check each claim against what the run read: supported, contradicted, unsupported, or loose.

Seven checks, from the Phase 3 design: literal, value, range, coverage,
truncation, absence, and arithmetic. Each looks only at the run's own tool
output (`view.RunView`), never at what was true in the world.
"""

import json
import math
import re
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from grader.claims import (
    AbsenceClaim,
    Claim,
    FirstClaim,
    GapClaim,
    LiteralClaim,
    Scope,
    ValueClaim,
    Window,
    normalize,
)
from grader.view import ERR, Line, Page, Row, RunView


class Check(StrEnum):
    LITERAL = "literal"
    VALUE = "value"
    RANGE = "range"
    COVERAGE = "coverage"
    TRUNCATION = "truncation"
    ABSENCE = "absence"
    ARITHMETIC = "arithmetic"


class Verdict(StrEnum):
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"  # the run's own output says otherwise
    UNSUPPORTED = "unsupported"  # past what the run read, or not in it at all
    LOOSE = "loose"  # hedged, and right for most of what it covers but not all


@dataclass(frozen=True)
class Finding:
    where: str
    text: str
    check: Check
    verdict: Verdict
    detail: str

    @property
    def flagged(self) -> bool:
        return self.verdict in (Verdict.CONTRADICTED, Verdict.UNSUPPORTED)


def check(claim: Claim, run: RunView) -> Finding | None:
    match claim:
        case LiteralClaim():
            return _literal(claim, run)
        case ValueClaim():
            return _value(claim, run)
        case FirstClaim():
            return _first(claim, run)
        case AbsenceClaim():
            return _absence(claim, run)
        case GapClaim():
            return _gap(claim, run)
    return None


# --- tolerance ------------------------------------------------------------------

def precision(text: str) -> float:
    """The step a stated number was rounded to: 0.1 for "8.5", 1 for "61", 100 for "1,500"."""
    text = text.replace(",", "")
    if "." in text:
        return 10.0 ** -len(text.split(".")[1])
    if float(text) == 0:
        return 1.0
    zeros = len(text) - len(text.rstrip("0"))
    return 10.0**zeros


def close(stated: float, stated_text: str, actual: float, hedged: bool) -> bool:
    """Whether a stated number fits an actual one, rounded or cut to the step it was written at.

    A round number's step is capped at a tenth of it, so "30" can stand for
    28.7 and "1,500" for 1,498, but "20" can't stand for 16.
    """
    step = precision(stated_text) if stated_text else 1.0
    if stated == 0:
        return abs(actual) <= step / 2  # "0 ms" can be 0.5 ms (rounded half down), not 0.6
    tolerance = max(min(step, 0.1 * abs(stated)) if step > 1 else step, 0.01 * abs(actual))
    if hedged:
        tolerance = max(tolerance, 0.1 * abs(actual), step)
    return abs(stated - actual) <= tolerance + 1e-9


# --- literal ---------------------------------------------------------------------

def _corpus(run: RunView) -> str:
    return "\n".join(run.texts)


def _squash(text: str) -> str:
    text = normalize(text).lower()
    text = re.sub(r"[\"'`]", "", text)
    return re.sub(r"\s+", " ", text)


def _times_seen(run: RunView) -> set[str]:
    seen: set[str] = set()
    stamps = [line.at for line in run.lines] + [_iso(r["ts"]) for r in run.deploys if "ts" in r]
    for moment in stamps:
        seen.add(moment.strftime("%H:%M:%S"))
        seen.add(moment.strftime("%H:%M:%S.") + f"{moment.microsecond // 1000:03d}")
    for row in run.rows:
        seen.add(row.start.strftime("%H:%M:%S"))
    seen.add(run.alarm_at.strftime("%H:%M:%S"))
    seen.add(run.alarm_at.strftime("%H:%M:%S.") + f"{run.alarm_at.microsecond // 1000:03d}")
    for text in run.texts:
        seen.update(re.findall(r"\d\d:\d\d:\d\d", text))
    return seen


def _literal(claim: LiteralClaim, run: RunView) -> Finding:
    corpus = _corpus(run)
    if claim.kind == "identifier":
        ok = claim.text in corpus
        detail = "in the tool output" if ok else "in no tool output"
        return Finding(claim.where, claim.text, Check.LITERAL, Verdict.SUPPORTED if ok else Verdict.UNSUPPORTED, detail)
    if claim.kind == "quote":
        ok = _squash(claim.text) in _squash(corpus)
        detail = "quoted from the tool output" if ok else "quoted from nothing the run read"
        return Finding(claim.where, claim.text, Check.LITERAL, Verdict.SUPPORTED if ok else Verdict.UNSUPPORTED, detail)
    clock = claim.text.split("T")[-1]
    seen = _times_seen(run)
    ok = clock in seen or any(s.startswith(clock) for s in seen if "." in s and "." in clock)
    if not ok and "." not in clock:
        ok = clock in {s.split(".")[0] for s in seen}
    detail = "a time in the tool output" if ok else "no line, record, or row at that time"
    return Finding(claim.where, claim.text, Check.LITERAL, Verdict.SUPPORTED if ok else Verdict.UNSUPPORTED, detail)


# --- windows ----------------------------------------------------------------------

def _moment(run: RunView, text: str) -> datetime:
    if "T" in text:
        return _iso(text)
    return run.at(text)


def _step(text: str) -> timedelta:
    """How much a stated time covers: a minute for "19:05", a second for "19:05:33"."""
    clock = text.split("T")[-1]
    if clock.count(":") == 1:
        return timedelta(minutes=1)
    if "." in clock:
        return timedelta(milliseconds=10 ** (3 - len(clock.split(".")[1])))
    return timedelta(seconds=1)


def _metric_span(run: RunView) -> tuple[datetime, datetime] | None:
    if not run.metrics_spans:
        return None
    return min(s for s, _ in run.metrics_spans), max(e for _, e in run.metrics_spans)


def _bounds(window: Window, run: RunView, start: datetime, end: datetime) -> tuple[datetime, datetime] | None:
    """A window as [from, to), given the start and end of what was read."""
    times = window.times
    match window.scope:
        case Scope.SPAN:
            lo = start if times[0] == "start" else _moment(run, times[0])
            hi = _moment(run, times[1]) + _step(times[1])
            return lo, hi
        case Scope.BEFORE:
            hi = _moment(run, times[0])
            lo = hi - timedelta(hours=window.hours_before) if window.hours_before else start
            return lo, hi
        case Scope.SINCE:
            return _moment(run, times[0]), end
        case Scope.WHOLE:
            return start, end
    return None


def _incident_rows(rows: list[Row]) -> list[Row]:
    """The last stretch of rows with errors in a table."""
    with_errors = [i for i, r in enumerate(rows) if (r.cells.get(ERR) or 0) > 0]
    if not with_errors:
        return []
    last = with_errors[-1]
    first = last
    while first > 0 and (rows[first - 1].cells.get(ERR) or 0) > 0:
        first -= 1
    return rows[first : last + 1]


def first_error_row(run: RunView) -> datetime | None:
    starts = [_incident_rows(t)[0].start for t in run.tables if _incident_rows(t)]
    return max(starts) if starts else None


def _covered(run: RunView, lo: datetime, hi: datetime, spans: list[tuple[datetime, datetime]]) -> float:
    """The share of [lo, hi) inside the union of spans."""
    if hi <= lo:
        return 1.0
    total = timedelta(0)
    merged: list[list[datetime]] = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    for s, e in merged:
        overlap = min(e, hi) - max(s, lo)
        if overlap > timedelta(0):
            total += overlap
    return total / (hi - lo)


# --- values and ranges ----------------------------------------------------------------

def _value(claim: ValueClaim, run: RunView) -> Finding | None:
    if claim.column.source == "log":
        return _log_value(claim, run)
    candidates = _row_sets(claim, run)
    if candidates is None:
        return None
    kind = Check.RANGE if claim.mode in ("range", "max", "at_most") or claim.window.scope != Scope.ROWS else Check.VALUE
    if isinstance(candidates, Finding):
        return candidates
    if not candidates:
        return Finding(claim.where, claim.text, kind, Verdict.UNSUPPORTED, "no row the run read is in that window")
    verdicts = [_judge(claim, values) for values in candidates]
    best = min(verdicts, key=lambda v: _RANK[v[0]])
    return Finding(claim.where, claim.text, kind, best[0], best[1])


_RANK = {Verdict.SUPPORTED: 0, Verdict.LOOSE: 1, Verdict.UNSUPPORTED: 2, Verdict.CONTRADICTED: 3}


def _row_sets(claim: ValueClaim, run: RunView) -> list[list[float]] | Finding | None:
    """The sets of values a claim could be about: one per table, or each stretch for an unscoped claim."""
    name = claim.column.name
    tables = [[r for r in t if r.cells.get(name) is not None] for t in run.tables]
    tables = [t for t in tables if t]
    alarm = [r for r in run.rows if r.source == "alarm"]
    scale = _err_scale(claim) if name == ERR else [1.0]
    window = claim.window
    sets: list[list[float]] = []
    if window.scope == Scope.ROWS:
        for text in window.times:
            moment = _moment(run, text)
            rows = [r for r in run.rows if r.start == moment and r.cells.get(name) is not None]
            if not rows and _step(text) == timedelta(minutes=1):
                rows = [r for t in tables for r in t if r.period_s == 60 and r.start == moment]
            if not rows:
                return Finding(claim.where, claim.text, Check.VALUE, Verdict.UNSUPPORTED,
                               f"no {name} row starting at {text} in what the run read")
            sets.append([r.cells[name] for r in rows])
        if claim.mode in ("value", "each") and len(window.times) >= 1:
            # every named row must hold the value (any table's copy of the row will do)
            results = []
            for values in sets:
                results.append([[v * s for v in values] for s in scale])
            flat = []
            for options in results:
                ok = any(any(close(claim.lo, claim.lo_text, v, claim.hedged) for v in vals) for vals in options)
                flat.append(ok)
            if all(flat):
                return [[claim.lo]]
            seen = ", ".join(f"{t}: {'/'.join(_fmt(v) for v in vals)}" for t, vals in zip(window.times, sets))
            return Finding(claim.where, claim.text, Check.VALUE, Verdict.CONTRADICTED, f"the rows show {seen}")
        return [[v * s for vals in sets for v in vals] for s in scale]
    span = _metric_span(run)
    if span is None:
        return Finding(claim.where, claim.text, Check.COVERAGE, Verdict.UNSUPPORTED, "the run read no metrics")
    if window.scope == Scope.ANY:
        alarm_table = [r for r in alarm if r.cells.get(name) is not None]
        for t in tables + ([alarm_table] if alarm_table else []):
            for i in range(len(t)):
                for j in range(i, len(t)):
                    sets.extend([[r.cells[name] * s for r in t[i : j + 1]] for s in scale])
        return sets
    if window.scope == Scope.INCIDENT:
        for t in tables:
            rows = _incident_rows([r for r in t])
            if rows:
                sets.extend([[r.cells[name] * s for r in rows if r.cells.get(name) is not None] for s in scale])
        return sets
    bounds = _bounds(window, run, *span)
    if bounds is None:
        return None
    lo, hi = bounds
    share = _covered(run, lo, hi, run.metrics_spans)
    if share < 0.9:
        return Finding(claim.where, claim.text, Check.COVERAGE, Verdict.UNSUPPORTED,
                       f"{_clock(lo)}-{_clock(hi)} is {share:.0%} inside the metrics the run read "
                       f"({', '.join(f'{_clock(s)}-{_clock(e)}' for s, e in run.metrics_spans)})")
    excluded = set()
    for text in window.except_times:
        a, _, b = text.partition("-")
        excluded.add((_moment(run, a), _moment(run, b or a) + _step(b or a) - _moment(run, a)))
    # a table that read only part of the window can't speak for all of it
    whole_tables = [t for t, s in zip(run.tables, run.metrics_spans) if _covered(run, lo, hi, [s]) >= 0.9]
    for t in whole_tables or run.tables:
        rows = [r for r in t if lo <= r.start < hi and r.cells.get(name) is not None
                and not any(m <= r.start < m + st for m, st in excluded)]
        if rows:
            sets.extend([[r.cells[name] * s for r in rows] for s in scale])
    if window.scope == Scope.WHOLE and len(sets) > 1:
        # "throughout": every row the run read, across its tables
        union = {}
        for t in tables:
            for r in t:
                union[r.start] = r.cells[name]
        sets = [[v * s for v in union.values()] for s in scale]
    return sets


def _err_scale(claim: ValueClaim) -> list[float]:
    # "error rate 0.0597" is CloudWatch's fraction; the table shows percent
    if claim.unit != "%" and claim.hi <= 1:
        return [1.0, 0.01]
    return [1.0]


def _judge(claim: ValueClaim, values: list[float]) -> tuple[Verdict, str]:
    if not values:
        return Verdict.UNSUPPORTED, "no values"
    lo, hi = min(values), max(values)
    seen = _fmt(lo) if lo == hi else f"{_fmt(lo)}-{_fmt(hi)}"
    hedged = claim.hedged
    if claim.mode == "at_most":
        ok = hi <= claim.hi or close(claim.hi, claim.hi_text or claim.lo_text, hi, hedged)
        return (Verdict.SUPPORTED, f"at most {_fmt(hi)}") if ok else (Verdict.CONTRADICTED, f"up to {_fmt(hi)}")
    if claim.mode == "max":
        ok = close(claim.hi, claim.hi_text or claim.lo_text, hi, hedged)
        return (Verdict.SUPPORTED, f"max {_fmt(hi)}") if ok else (Verdict.CONTRADICTED, f"max {_fmt(hi)} over {seen}")
    if claim.mode == "each":
        ok = any(close(claim.lo, claim.lo_text, v, hedged) for v in values)
        return (Verdict.SUPPORTED, f"among {seen}") if ok else (Verdict.UNSUPPORTED, f"not among {seen}")
    if claim.mode == "range":
        lo_ok = close(claim.lo, claim.lo_text, lo, hedged)
        hi_ok = close(claim.hi, claim.hi_text, hi, hedged)
        if lo_ok and hi_ok:
            return Verdict.SUPPORTED, f"the rows run {seen}"
        inside = all(claim.lo - _tol(claim.lo, claim.lo_text) <= v <= claim.hi + _tol(claim.hi, claim.hi_text) for v in values)
        return (Verdict.UNSUPPORTED if inside else Verdict.CONTRADICTED), f"the rows run {seen}"
    # one value over a stretch: every value should be it
    if all(close(claim.lo, claim.lo_text, v, hedged) for v in values):
        return Verdict.SUPPORTED, f"the rows show {seen}"
    if hedged and close(claim.lo, claim.lo_text, statistics.median(values), True):
        return Verdict.LOOSE, f"the rows run {seen}"
    return Verdict.CONTRADICTED, f"the rows run {seen}"


def _tol(value: float, text: str) -> float:
    return max(precision(text) / 2 if text else 0.5, 0.01 * abs(value))


def _log_value(claim: ValueClaim, run: RunView) -> Finding | None:
    field = claim.column.name
    flt = claim.log_filter
    values = _field(_filtered(run, flt), field)
    if not values and flt.get("failed_from") == "item":
        # the item named failures, but this field is only on other lines: "pool_wait_time_ms 0.0-0.5 on the 19:05 requests"
        flt = {k: v for k, v in flt.items() if k != "failed"}
        values = _field(_filtered(run, flt), field)
    kind = Check.RANGE if claim.mode in ("range", "max", "at_most") or len(values) > 1 else Check.VALUE
    if not values:
        return Finding(claim.where, claim.text, kind, Verdict.UNSUPPORTED,
                       f"no line the run read{_describe(claim.log_filter)} has {field}")
    verdict, detail = _judge(claim, values)
    return Finding(claim.where, claim.text, kind, verdict, f"{len(values)} lines{_describe(flt)}: {detail}")


def _field(lines: list[Line], name: str) -> list[float]:
    return [float(v) for line in lines if isinstance(v := line.record.get(name), (int, float)) and not isinstance(v, bool)]


def _filtered(run: RunView, flt: dict) -> list[Line]:
    lines = run.lines
    if "failed" in flt:
        lines = [line for line in lines if line.failed == flt["failed"]]
    if "version" in flt:
        lines = [line for line in lines if line.record.get("version") == flt["version"]]
    if "route" in flt:
        lines = [line for line in lines if line.record.get("route") == flt["route"]]
    window = flt.get("window")
    if isinstance(window, Window):
        first = lines[0].at if lines else run.alarm_at
        bounds = _bounds(window, run, min([first, run.alarm_at]), run.alarm_at + timedelta(hours=1))
        if bounds is None and window.scope == Scope.ROWS:
            moments = [(_moment(run, t), _step(t)) for t in window.times]
            lines = [line for line in lines if any(m <= line.at < m + s for m, s in moments)]
        elif bounds:
            lines = [line for line in lines if bounds[0] <= line.at < bounds[1]]
    return lines


def _describe(flt: dict) -> str:
    parts = []
    if "failed" in flt:
        parts.append("failed" if flt["failed"] else "succeeded")
    for key in ("version", "route"):
        if key in flt:
            parts.append(str(flt[key]))
    return f" ({', '.join(parts)})" if parts else ""


# --- first --------------------------------------------------------------------------

def _first(claim: FirstClaim, run: RunView) -> Finding:
    times = sorted(claim.times, key=lambda t: _moment(run, t))
    text = times[0]
    moment, step = _moment(run, text), _step(text)
    for page in run.pages:
        for line in page.lines:
            if line.failed and moment <= line.at < moment + step and (not claim.version or line.record.get("version") == claim.version):
                return _first_line(claim, run, line, text)
    rows = [(t, i) for t in run.tables for i, r in enumerate(t) if r.start == moment]
    for table, i in rows:
        if (table[i].cells.get(ERR) or 0) > 0:
            if i == 0 or (table[i - 1].cells.get(ERR) or 0) == 0:
                return Finding(claim.where, claim.text, Check.TRUNCATION, Verdict.SUPPORTED,
                               f"the first row with errors starts at {text}")
            return Finding(claim.where, claim.text, Check.TRUNCATION, Verdict.CONTRADICTED,
                           f"the row before {text} already has errors")
    return Finding(claim.where, claim.text, Check.LITERAL, Verdict.UNSUPPORTED, f"no failure at {text} in what the run read")


def _same_kind(a: Line, b: Line) -> bool:
    keys = ("error_type", "version")
    return all(a.record.get(k) == b.record.get(k) for k in keys)


def _first_line(claim: FirstClaim, run: RunView, line: Line, text: str) -> Finding:
    earlier_same = [
        other for page in run.pages for other in page.lines
        if other.failed and other.at < line.at and _same_kind(other, line)
    ]
    if earlier_same:
        first = min(earlier_same, key=lambda other: other.at)
        return Finding(claim.where, claim.text, Check.TRUNCATION, Verdict.CONTRADICTED,
                       f"an earlier one, at {_clock(first.at, ms=True)}, is in the run's own output")
    holding = [p for p in run.pages if line in p.lines]
    establishing = [p for p in holding if _shows_before(p, line)]
    if establishing:
        return Finding(claim.where, claim.text, Check.TRUNCATION, Verdict.SUPPORTED,
                       "a page that reaches back past it shows nothing earlier")
    cut = [p for p in holding if p.page.cut]
    detail = (
        f"it's the oldest line shown on a cut page ({len(cut[0].page.lines)} of {cut[0].page.found} found)"
        if cut else "no page the run read reaches back past it"
    )
    return Finding(claim.where, claim.text, Check.TRUNCATION, Verdict.UNSUPPORTED, detail)


def _shows_before(page: Page, line: Line) -> bool:
    """Whether a page speaks for some time before the line: an earlier shown line, or an uncut search."""
    earlier = [other for other in page.lines if other.at < line.at]
    if earlier:
        return True
    return not page.page.cut and page.searched_from < line.at


# --- absence ----------------------------------------------------------------------

def _absence(claim: AbsenceClaim, run: RunView) -> Finding | None:
    if claim.what == "deploys":
        return _no_deploys(claim, run)
    spans: list[tuple[datetime, datetime]] = list(run.metrics_spans) if not claim.keyword else []
    spans += [(p.speaks_from, p.searched_to) for p in run.pages]
    if not spans:
        return Finding(claim.where, claim.text, Check.COVERAGE, Verdict.UNSUPPORTED, "the run read no metrics or logs")
    start, end = min(s for s, _ in spans), max(e for _, e in spans)
    bounds = _bounds(claim.window, run, start, end)
    if bounds is None:
        return None
    lo, hi = bounds
    hits: list[str] = []
    if claim.what in ("errors", "TypeError"):
        if not claim.keyword and claim.what == "errors":
            for row in run.rows:
                if row.source == "metrics" and lo <= row.start < hi and (row.cells.get(ERR) or 0) > 0:
                    hits.append(f"err% {_fmt(row.cells[ERR])} at {_clock(row.start)}")
        for line in run.lines:
            if line.failed and lo <= line.at < hi:
                blob = json.dumps(line.record).lower()
                if claim.keyword and claim.keyword not in blob:
                    continue
                if claim.what == "TypeError" and line.record.get("error_type") != "TypeError":
                    continue
                hits.append(f"a failed line at {_clock(line.at, ms=True)}")
    elif claim.what == "timeouts":
        for row in run.rows:
            if row.source == "metrics" and lo <= row.start < hi and (row.cells.get("payments_timeouts") or 0) > 0:
                hits.append(f"payments_timeouts {_fmt(row.cells['payments_timeouts'])} at {_clock(row.start)}")
        for line in run.lines:
            if lo <= line.at < hi and line.record.get("upstream_timeout") == 1:
                hits.append(f"a timed-out line at {_clock(line.at, ms=True)}")
    if hits:
        return Finding(claim.where, claim.text, Check.ABSENCE, Verdict.CONTRADICTED,
                       f"{len(hits)} in the run's own output, first {hits[0]}")
    share = _covered(run, lo, hi, spans)
    if share < 0.9:
        return Finding(claim.where, claim.text, Check.COVERAGE, Verdict.UNSUPPORTED,
                       f"{_clock(lo)}-{_clock(hi)} is {share:.0%} inside what the run read")
    return Finding(claim.where, claim.text, Check.ABSENCE, Verdict.SUPPORTED, "none in what the run read")


def _no_deploys(claim: AbsenceClaim, run: RunView) -> Finding:
    oldest = run.deploys_from
    if oldest is None:
        return Finding(claim.where, claim.text, Check.COVERAGE, Verdict.UNSUPPORTED, "the run never read the deploy history")
    up_to = max(_iso(p.up_to) for p in run.deploy_pages)
    bounds = _bounds(claim.window, run, oldest, up_to + timedelta(milliseconds=1))
    if bounds is None:
        return Finding(claim.where, claim.text, Check.ABSENCE, Verdict.SUPPORTED, "unscoped")
    lo, hi = bounds
    mentioned = [(_moment(run, t), _step(t)) for t in claim.mentioned] if claim.other else []
    hits = []
    for record in run.deploys:
        at = _iso(record["ts"])
        if lo <= at < hi and not any(m <= at < m + s for m, s in mentioned):
            hits.append(f"{record.get('kind', 'record')} at {_clock(at, ms=True)}")
    if hits:
        return Finding(claim.where, claim.text, Check.ABSENCE, Verdict.CONTRADICTED, f"the history shows {', '.join(hits)}")
    if lo < oldest:
        return Finding(claim.where, claim.text, Check.COVERAGE, Verdict.UNSUPPORTED, "older than the history the run read")
    return Finding(claim.where, claim.text, Check.ABSENCE, Verdict.SUPPORTED, "none in the history the run read")


# --- arithmetic -----------------------------------------------------------------------

def _gap(claim: GapClaim, run: RunView) -> Finding | None:
    subject = _moment(run, claim.subject)
    if claim.target == ("first_error_row",):
        start = first_error_row(run)
        if start is None:
            return None
        targets = [start]
    else:
        targets = [_moment(run, t) for t in claim.target]
    gaps = [(t - subject).total_seconds() for t in targets]
    if claim.direction in ("after",):
        gaps = [-g for g in gaps]
    stated = (claim.lo, claim.hi)
    if len(gaps) == 1:
        ok = any(close(s, _text_of(s, claim.precision), gaps[0], claim.hedged) for s in stated)
    else:
        ok = close(stated[0], _text_of(stated[0], claim.precision), min(gaps), claim.hedged) and close(
            stated[1], _text_of(stated[1], claim.precision), max(gaps), claim.hedged
        )
    shown = ", ".join(f"{g:.2f} s" for g in gaps)
    detail = f"from {claim.subject} to {', '.join(_clock(t, ms=True) for t in targets)}: {shown}"
    if ok:
        return Finding(claim.where, claim.text, Check.ARITHMETIC, Verdict.SUPPORTED, detail)
    return Finding(claim.where, claim.text, Check.ARITHMETIC, Verdict.CONTRADICTED, detail)


def _text_of(value: float, step: float) -> str:
    decimals = max(0, -int(round(math.log10(step)))) if step < 1 else 0
    return f"{value:.{decimals}f}"


# --- helpers -------------------------------------------------------------------------

def _iso(text: str) -> datetime:
    from grader.view import _iso as parse

    return parse(text)


def _clock(moment: datetime, ms: bool = False) -> str:
    if ms:
        return moment.strftime("%H:%M:%S.") + f"{moment.microsecond // 1000:03d}"
    return moment.strftime("%H:%M:%S")


def _fmt(value: float) -> str:
    return f"{value:g}"
