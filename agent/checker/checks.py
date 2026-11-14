"""Whether each typed evidence item holds in the data it cites: one check per kind.

An item is resolved first (`resolve`), which finds the entries it's about.
Each kind's check then recomputes what the item states from those entries,
with no guess about what it refers to (BUILD_PLAN.md, "The grader redesign"):

  value       column at the one row, line, record, or datapoint is value
  range       across the scope, column's smallest is low and its largest high
  all         every readable entry in the scope has column equal to value
  count       count entries in the scope have column equal to value; with no
              column, the scope holds count entries (a log read's whole result
              may also be counted by what it found, which its first line states)
  first, last the oldest or newest entry in the scope, among those with column
              equal to value when it gives one, is at at
  none        no entry in the window has column equal to value; with a column
              and no value, none has it set (non-null, non-zero, non-empty);
              with neither, the window holds no entry at all. An end stated to
              the millisecond that names an entry bounds the window: "none from
              the 14:19:02.761 change to the 18:56:06.039 one" holds neither
  gap         seconds pass from start to end

The scope is the cited lines, else the rows or lines from start to end, else
the whole result. The window includes its end, as the schema says; an end
stated coarser than the entries it bounds is read two ways, through its whole
second and up to its instant, and the item holds if it holds either way: "none
to 19:05:23" isn't contradicted by a line at 19:05:23.956.

A stated number holds within half a step of its last digit: 19.5 holds for
19.48 and 19.4 doesn't; 66 holds for 65.8 and not for 66.7. Percent signs and
ms or s units are dropped, and the alarm's fractions read a stated 8.5% as
0.085. A changed key holds as old->new or as its new value; a stack as one of
its frames, or several in order ("a -> b"). Fields an item states beyond what
its kind uses are listed as unchecked, never guessed at.

Verdicts: holds; loose, right as far as it goes with the rest unchecked (a
text field's value and then more words, "deploy 1.43.0" for kind deploy, or a
number stated as a range, "0.1-4.2"); contradicted, the cited data says
otherwise; outside_read and unresolved, from the resolver; untyped, a
sentence. Contradicted, outside_read, and unresolved are the flags.
"""

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from checker.resolve import Entry, Resolution, Status, resolve
from checker.sources import RunSources
from sources import ALARM


class Verdict(StrEnum):
    HOLDS = "holds"
    LOOSE = "loose"  # right as far as it goes, the rest unchecked: "deploy 1.43.0" for kind deploy, "0.1-4.2"
    CONTRADICTED = "contradicted"  # the cited data says otherwise
    OUTSIDE_READ = "outside_read"  # it names a time its result never covered
    UNRESOLVED = "unresolved"  # its citation can't be followed
    UNTYPED = "untyped"  # a sentence, from before typed evidence


FLAGS = frozenset({Verdict.CONTRADICTED, Verdict.OUTSIDE_READ, Verdict.UNRESOLVED})
_FROM_STATUS = {
    Status.OUTSIDE_READ: Verdict.OUTSIDE_READ,
    Status.UNRESOLVED: Verdict.UNRESOLVED,
    Status.UNTYPED: Verdict.UNTYPED,
}
STATED = ("column", "at", "start", "end", "lines", "value", "low", "high", "count", "seconds")
# What each kind's check reads. Anything else an item states is reported unchecked.
USES = {
    "value": {"column", "value", "at", "lines"},
    "range": {"column", "low", "high", "start", "end", "lines"},
    "all": {"column", "value", "start", "end", "lines"},
    "count": {"column", "value", "count", "start", "end", "lines"},
    "first": {"column", "value", "at", "start", "end", "lines"},
    "last": {"column", "value", "at", "start", "end", "lines"},
    "none": {"column", "value", "start", "end", "lines"},
    "gap": {"start", "end", "seconds"},
}


@dataclass
class ItemCheck:
    id: str
    kind: str
    source: str | None
    verdict: Verdict
    reason: str = ""
    column: str | None = None
    points_at: list[str] = field(default_factory=list)
    actual: dict[str, Any] = field(default_factory=dict)  # what the cited data shows, beside what the item states
    unchecked: list[str] = field(default_factory=list)  # fields stated beyond the kind's
    # The entries the item shows: a value's target, a first or last's entry, a
    # count's matches, the scope otherwise. The rubric reads its citations here.
    entries: list[Entry] = field(default_factory=list, repr=False)

    @property
    def flagged(self) -> bool:
        return self.verdict in FLAGS

    def row(self) -> dict[str, Any]:
        """As data, without the entries."""
        return {
            "id": self.id, "kind": self.kind, "source": self.source, "column": self.column, "verdict": self.verdict.value,
            "reason": self.reason, "points_at": self.points_at, "actual": self.actual, "unchecked": self.unchecked,
        }


def check_all(hypothesis: dict[str, Any] | None, run: RunSources) -> list[ItemCheck]:
    if not hypothesis:
        return []
    return [check_item(f"e{i}", item, run) for i, item in enumerate(hypothesis.get("evidence") or [], 1)]


def check_item(item_id: str, item: dict[str, Any] | str, run: RunSources) -> ItemCheck:
    return check_resolved(item, resolve(item_id, item, run), run)


def check_resolved(item: dict[str, Any] | str, resolution: Resolution, run: RunSources) -> ItemCheck:
    column = item.get("column") if isinstance(item, dict) else None
    out = ItemCheck(
        resolution.id, resolution.kind, resolution.source, Verdict.HOLDS, column=column, points_at=resolution.points_at
    )
    if resolution.status is not Status.RESOLVED:
        out.verdict, out.reason = _FROM_STATUS[resolution.status], resolution.reason
        return out
    assert isinstance(item, dict)
    out.unchecked = [k for k in STATED if _stated(item, k) and k not in USES[resolution.kind]]
    check = _CHECKS[resolution.kind]
    holds, out.reason, out.actual, out.entries = check(item, resolution, run)
    if not holds and resolution.short_scope is not None:
        short = check(item, replace(resolution, scope=resolution.short_scope, short_scope=None), run)
        if short[0]:
            holds, out.reason, out.actual, out.entries = short
            out.reason += f" (the window to the instant of its end, {item['end']})"
    out.verdict = holds if isinstance(holds, Verdict) else Verdict.HOLDS if holds else Verdict.CONTRADICTED
    return out


def _stated(item: dict[str, Any], key: str) -> bool:
    return item.get(key) not in (None, [], "")


# --- reading values -------------------------------------------------------------

MISSING = object()  # an entry without the field
_UNIT = re.compile(r"\s*(?:%|ms|s)\s*\Z", re.I)
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")
# Arrows and dashes the model may write, as ASCII.
_ASCII = {0x2192: "->", 0x2013: "-", 0x2014: "-", 0x2212: "-"}


def field_of(entry: Entry, column: str | None) -> Any:
    if entry.fields is None:
        return MISSING
    if ALARM in entry.fields and len(entry.fields) == 1:
        return entry.fields[ALARM]  # the alarm's one metric, however the item names it
    if column is None:
        return MISSING
    return entry.fields.get(column, MISSING)


def stated_for(entry: Entry, stated: Any) -> Any:
    """A stated value as this entry's scale writes it: the alarm's datapoints are fractions, so 8.5% is 0.085."""
    if entry.fields is None or ALARM not in entry.fields or not str(stated).strip().endswith("%"):
        return stated
    found = number(stated)
    if found is None:
        return stated
    text = _UNIT.sub("", str(stated).strip())
    decimals = len(text.split(".")[1]) if "." in text else 0
    return f"{found[0] / 100:.{decimals + 2}f}"


def number(text: Any) -> tuple[float, float] | None:
    """A stated number and the tolerance it carries: half a step of its last digit. None if it isn't one."""
    if isinstance(text, bool):
        return None
    if isinstance(text, int):
        return float(text), 0.5
    if isinstance(text, float):
        if not math.isfinite(text):
            return None
        shown = repr(text)
        if "e" in shown or "." not in shown or text.is_integer():
            return text, 0.5
        return text, 0.5 * 10.0 ** -len(shown.split(".")[1])
    stripped = _UNIT.sub("", str(text).strip()).replace(",", "")
    if not _NUMBER.fullmatch(stripped):
        return None
    decimals = len(stripped.split(".")[1]) if "." in stripped else 0
    return float(stripped), 0.5 * 10.0**-decimals


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _norm(text: Any) -> str:
    return str(text).translate(_ASCII).strip().strip("'\"").casefold()


def same(stated: Any, actual: Any) -> bool:
    """Whether a stated value is what the data shows."""
    if actual is MISSING:
        return False
    if actual is None:
        return _norm(stated) in {"-", "null", "none", "n/a", ""}
    if isinstance(actual, bool):
        return _norm(stated) == ("true" if actual else "false")
    if is_number(actual):
        n = number(stated)
        return n is not None and abs(n[0] - actual) <= n[1] + 1e-9
    if isinstance(actual, list):
        if any(same(stated, a) for a in actual) or _norm(stated) == _norm(json.dumps(actual)):
            return True
        return _in_order(stated, actual)
    if isinstance(actual, dict):
        return _norm(stated) == _norm(json.dumps(actual, separators=(",", ":")))
    text = str(actual)
    if "->" in text:  # a change, KEY old->new or a changed key's own old->new: as written, or as its new value
        if _squeeze(stated) == _squeeze(text):
            return True
        return " " not in text and same(stated, text.split("->", 1)[1])
    stated_n, actual_n = number(stated), number(text)
    if stated_n is not None and actual_n is not None:
        return abs(stated_n[0] - actual_n[0]) <= stated_n[1] + 1e-9
    return _norm(stated) == _norm(text)


def _in_order(stated: Any, actual: list[Any]) -> bool:
    """Frames written as a path, "a -> b": each one an element of the list, in its order."""
    pieces = [p.strip() for p in str(stated).translate(_ASCII).split("->")]
    if len(pieces) < 2 or not all(pieces):
        return False
    at = 0
    for piece in pieces:
        while at < len(actual) and not same(piece, actual[at]):
            at += 1
        if at == len(actual):
            return False
        at += 1
    return True


_SPAN = re.compile(r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(?:-|to|\.\.)\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(?:%|ms|s)?\s*", re.I)


def loosely(stated: Any, actual: Any) -> bool:
    """Right as far as it goes: a text field's value and then more words, or a number stated as a range that holds it."""
    if is_number(actual) and isinstance(stated, str):
        found = _SPAN.fullmatch(stated.translate(_ASCII))
        if found:
            low, high = number(found[1]), number(found[2])
            assert low is not None and high is not None
            return low[0] - low[1] - 1e-9 <= actual <= high[0] + high[1] + 1e-9
        return False
    return starts_with(stated, actual)


def _squeeze(text: Any) -> str:
    return re.sub(r"[\s:]+", "", _norm(text))


def starts_with(stated: Any, actual: Any) -> bool:
    """A stated value that is a text field's value and then more words: "deploy 1.43.0" for kind deploy."""
    if not isinstance(actual, str) or not actual or not isinstance(stated, str):
        return False
    head = _norm(actual)
    return _norm(stated).startswith(head + " ") or _norm(stated).startswith(head + ":")


def is_set(value: Any) -> bool:
    """A field that holds something: not missing, null, zero, false, or empty."""
    if value is MISSING or value is None or value is False:
        return False
    if is_number(value):
        return value != 0
    return value not in ("", [], {})


def show(value: Any) -> str:
    if value is MISSING:
        return "missing"
    if is_number(value):
        return f"{value:g}" if isinstance(value, float) else str(value)
    return json.dumps(value) if not isinstance(value, str) else value


def matches(item: dict[str, Any], entry: Entry) -> bool:
    """An entry with column equal to value; with no value, column set; with neither, any entry."""
    column, value = item.get("column"), item.get("value")
    if not column:
        return True
    actual = field_of(entry, column)
    return same(stated_for(entry, value), actual) if value is not None else is_set(actual)


# --- the checks -----------------------------------------------------------------

Outcome = tuple[bool | Verdict, str, dict[str, Any], list[Entry]]
_EXACT = timedelta(milliseconds=1)


def _value(item: dict[str, Any], r: Resolution, run: RunSources) -> Outcome:
    entry = r.target
    assert entry is not None
    column = item.get("column")
    if item.get("at") and item.get("lines") and entry.time is not None:
        moment, width = run.moment(str(item["at"]))
        if not moment <= entry.time < moment + width:
            return False, f"{entry.name} is at {_clock(entry.time)}, not {item['at']}", {"at": _clock(entry.time)}, [entry]
    actual = field_of(entry, column)
    if actual is MISSING:
        return False, f"{entry.name} has no {column}", {"value": None}, [entry]
    stated = stated_for(entry, item.get("value"))
    holds: bool | Verdict = same(stated, actual) or (Verdict.LOOSE if loosely(stated, actual) else False)
    return holds, f"{column} at {entry.name} is {show(actual)}", {"value": show(actual)}, [entry]


def _range(item: dict[str, Any], r: Resolution, run: RunSources) -> Outcome:
    column = item.get("column")
    shown = [e for e in r.scope if is_number(field_of(e, column))]
    if not shown:
        return False, f"no {column} values in the {_noun(r)}", {}, []
    values = [field_of(e, column) for e in shown]
    lo, hi = min(values), max(values)
    low, high = number(item.get("low")), number(item.get("high"))
    assert low is not None and high is not None
    holds = abs(low[0] - lo) <= low[1] + 1e-9 and abs(high[0] - hi) <= high[1] + 1e-9
    skipped = len(r.scope) - len(shown)
    note = f" ({skipped} without it)" if skipped else ""
    reason = f"{column} runs {show(lo)} to {show(hi)} over {len(shown)} {_noun(r)}{note}"
    return holds, reason, {"low": lo, "high": hi, "n": len(shown)}, shown


def _all(item: dict[str, Any], r: Resolution, run: RunSources) -> Outcome:
    column, value = item.get("column"), item.get("value")
    readable = [e for e in r.scope if e.fields is not None]
    if not readable:
        return False, f"nothing in the {_noun(r)}", {}, []
    other = [e for e in readable if not same(stated_for(e, value), field_of(e, column))]
    if other and all(loosely(value, field_of(e, column)) for e in other):
        return Verdict.LOOSE, f"all {len(readable)} {_noun(r)} have {column} as far as {value!r} goes", {"n": len(readable)}, readable
    if other:
        first = other[0]
        reason = f"{len(other)} of {len(readable)} {_noun(r)} have another {column}: {first.name} has {show(field_of(first, column))}"
        return False, reason, {"other": len(other), "n": len(readable)}, readable
    return True, f"all {len(readable)} {_noun(r)} have {column} {value}", {"n": len(readable)}, readable


def _count(item: dict[str, Any], r: Resolution, run: RunSources) -> Outcome:
    stated = item.get("count")
    if item.get("column"):
        matching = [e for e in r.scope if matches(item, e)]
        n = len(matching)
        what = f"{item['column']} {item['value']}" if item.get("value") is not None else f"{item['column']} set"
        return stated == n, f"{n} of {len(r.scope)} {_noun(r)} have {what}", {"count": n}, matching
    n = len(r.scope)
    logs = r.result.logs if r.result is not None else None
    whole = not item.get("lines") and not item.get("start") and not item.get("end")
    if logs is not None and whole and stated == logs.found and n != logs.found:
        return True, f"the read found {logs.found} and shows {n}", {"count": n, "found": logs.found}, r.scope
    found = f"; it found {logs.found}" if logs is not None and whole and logs.found != n else ""
    return stated == n, f"{n} {_noun(r)}{found}", {"count": n}, r.scope


def _first_or_last(item: dict[str, Any], r: Resolution, run: RunSources) -> Outcome:
    candidates = sorted((e for e in r.scope if e.time is not None and matches(item, e)), key=lambda e: e.time)
    if not candidates:
        return False, f"nothing in the {_noun(r)} matches", {}, []
    entry = candidates[0] if r.kind == "first" else candidates[-1]
    assert entry.time is not None
    moment, width = run.moment(str(item["at"]))
    holds = moment <= entry.time < moment + width
    reason = f"the {r.kind} of {len(candidates)} is {entry.name}, at {_clock(entry.time)}"
    return holds, reason, {"at": _clock(entry.time)}, [entry]


def _none(item: dict[str, Any], r: Resolution, run: RunSources) -> Outcome:
    # A window from one record's exact time to another's can't mean those records
    # are absent: an end stated to the millisecond that names an entry bounds it.
    bounds = set()
    for key in ("start", "end"):
        if item.get(key):
            moment, width = run.moment(str(item[key]))
            if width <= _EXACT:
                bounds.add(moment)
    matching = [e for e in r.scope if matches(item, e) and e.time not in bounds]
    if matching:
        first = matching[0]
        detail = f" with {item['column']} {show(field_of(first, item['column']))}" if item.get("column") else ""
        return False, f"{len(matching)} in the window, the first {first.name}{detail}", {"n": len(matching)}, matching
    return True, f"nothing matches in the window ({len(r.scope)} {_noun(r)} in it)", {"n": 0}, []


def _gap(item: dict[str, Any], r: Resolution, run: RunSources) -> Outcome:
    start, start_width = run.moment(str(item["start"]))
    end, end_width = run.moment(str(item["end"]))
    actual = (end - start).total_seconds()
    stated = number(item.get("seconds"))
    assert stated is not None
    slack = stated[1] + max(start_width, end_width).total_seconds()
    holds = abs(stated[0] - actual) <= slack + 1e-9
    return holds, f"{actual:g} s from start to end", {"seconds": actual}, []


_CHECKS: dict[str, Callable[[dict[str, Any], Resolution, RunSources], Outcome]] = {
    "value": _value,
    "range": _range,
    "all": _all,
    "count": _count,
    "first": _first_or_last,
    "last": _first_or_last,
    "none": _none,
    "gap": _gap,
}


def _noun(r: Resolution) -> str:
    source = r.result
    if source is None:
        return "entries"
    if source.metrics is not None:
        return "rows"
    if source.logs is not None:
        return "lines"
    if source.deploys is not None:
        return "records"
    return "datapoints"


def _clock(moment: datetime) -> str:
    text = f"{moment:%H:%M:%S.%f}"[:-3]
    return text[:-4] if moment.microsecond == 0 else text
