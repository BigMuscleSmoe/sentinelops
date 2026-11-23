"""Planted errors: items that hold, made wrong in code, each of which the checker must flag.

BUILD_PLAN.md, "The grader redesign", step 4. No model: every claim the agent
made that holds gets one mutant per way it can be made wrong, and each mutant
must still validate as Evidence, so it's a claim the model could have sent:

  number   the value, or a range's low or high, moved past every value its
           entries show: by a quarter, and at least 1
  text     a text value swapped for another one the result shows in that
           column, or failing that one the run's other results show; with
           none, a change reversed (2->20 for 20->2), a boolean flipped, a
           version's minor number raised, or a stack frame's line moved down
  window   at, or a window's end, moved 10 minutes past the end of the read
  count    count off by one, each way, never onto what the result shows or found
  moved    a first or last item's at moved to another entry that matches
  shifted  a none item's window moved onto an entry that matches
  seconds  a gap's seconds moved past its tolerance

The recall it reports is by kind and by mutation (`recall`); CI requires every
mutant flagged (tests/test_checker_fixture.py).
"""

import copy
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from checker.checks import FLAGS, MISSING, ItemCheck, Verdict, check_item, field_of, is_number, matches, number, same
from checker.resolve import Entry, entries, resolve, span_of
from checker.sources import RunSources
from models import Evidence

PAST = timedelta(minutes=10)
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_FRAME = re.compile(r"(.+):(\d+)( in .+)")
_HALF_SECOND = timedelta(milliseconds=500)


@dataclass(frozen=True)
class Mutant:
    item_id: str
    kind: str
    mutation: str
    item: dict[str, Any]


@dataclass(frozen=True)
class Planted:
    run: str
    item_id: str
    kind: str
    mutation: str
    verdict: Verdict
    reason: str

    @property
    def caught(self) -> bool:
        return self.verdict in FLAGS


def plant_run(name: str, hypothesis: dict[str, Any], run: RunSources) -> tuple[list[Planted], list[str]]:
    """Every mutant of every item that holds in one run with the checker's verdict on each, and the items with none."""
    out, unplanted = [], []
    for i, item in enumerate(hypothesis.get("evidence") or [], 1):
        item_id = f"e{i}"
        if not isinstance(item, dict) or check_item(item_id, item, run).verdict is not Verdict.HOLDS:
            continue
        made = list(mutants(item_id, item, run))
        if not made:
            unplanted.append(f"{name} {item_id} {item.get('kind')}")
        for mutant in made:
            check: ItemCheck = check_item(item_id, mutant.item, run)
            out.append(Planted(name, item_id, mutant.kind, mutant.mutation, check.verdict, check.reason))
    return out, unplanted


def mutants(item_id: str, item: dict[str, Any], run: RunSources) -> Iterator[Mutant]:
    resolution = resolve(item_id, item, run)
    kind = resolution.kind

    def made(mutation: str, **changes: Any) -> Mutant | None:
        changed = {**copy.deepcopy(item), **changes}
        try:
            Evidence.model_validate(changed)
        except ValueError:
            return None
        return Mutant(item_id, kind, mutation, changed)

    found: list[Mutant | None] = []
    source = resolution.result
    every = entries(source, run) if source is not None else []
    column = item.get("column")
    # The checker reads an end stated coarser than its entries both ways, so a
    # mutant must be wrong under both.
    scopes = [resolution.scope] + ([resolution.short_scope] if resolution.short_scope is not None else [])

    if kind in ("value", "all"):
        shown = [field_of(e, column) for e in (resolution.scope if kind == "all" else [resolution.target])
                 if e is not None and e.fields is not None]
        actual = shown[0] if shown else MISSING
        numbers = [v for v in shown if is_number(v)]
        if is_number(actual) and number(item.get("value")) is not None:
            found.append(made("number", value=_written(max(numbers) + _step(max(numbers)), item.get("value"))))
        elif actual is not MISSING and actual is not None:
            rest = [e for s in run.by_label.values() if s is not source and _readable(s) for e in entries(s, run)]
            other = _other_text(actual, shown, every, column) or _other_text(actual, shown, rest, column, made_up=True)
            if other is not None:
                found.append(made("text", value=other))
    if kind == "range":
        values = [field_of(e, column) for e in resolution.scope if is_number(field_of(e, column))]
        found.append(made("number", high=float(max(values) + _step(max(values)))))
        found.append(made("number", low=float(min(values) - _step(min(values)))))
    if kind == "count":
        supported = {len([e for e in scope if matches(item, e)]) for scope in scopes}
        if source is not None and source.logs is not None and not column:
            supported.add(source.logs.found)
        stated = int(item["count"])
        for wrong in (stated + 1, stated - 1):
            if wrong >= 0 and wrong not in supported:
                found.append(made("count", count=wrong))
    if kind in ("first", "last") and source is not None:
        def ordered(scope: list[Entry]) -> list[Entry]:
            found_ = sorted((e for e in scope if e.time is not None and matches(item, e)), key=lambda e: e.time)
            return found_[::-1] if kind == "last" else found_

        truths = {ordered(scope)[0].time for scope in scopes if ordered(scope)}
        other = [e for e in ordered(resolution.scope) if e.time not in truths]
        if other:
            assert other[0].time is not None
            found.append(made("moved", at=_iso(other[0].time)))
    if kind == "none" and source is not None:
        span = span_of(source, run)
        for e in every:
            if e.time is None or e.fields is None or not matches(item, e) or e in resolution.scope:
                continue
            start, end = e.time - _HALF_SECOND, e.time + _HALF_SECOND
            if span.holds(start) and span.holds(end):
                found.append(made("shifted", start=_iso(start), end=_iso(end)))
                break
    if kind == "gap":
        stated = number(item.get("seconds"))
        assert stated is not None
        widths = [run.moment(str(item[k]))[1].total_seconds() for k in ("start", "end")]
        found.append(made("seconds", seconds=float(stated[0] + stated[1] + max(widths) + _step(stated[0]))))

    if source is not None and kind != "gap":
        past = _iso(span_of(source, run).end + PAST)
        if item.get("at") and kind in ("value", "first", "last"):
            found.append(made("window", at=past))
        elif item.get("end") and kind in ("range", "all", "count", "none"):
            found.append(made("window", end=past))
    yield from (m for m in found if m is not None)


def recall(planted: list[Planted]) -> dict[str, Any]:
    """Caught over planted, overall, by kind, and by mutation, with every miss."""

    def share(rows: list[Planted]) -> dict[str, int]:
        return {"planted": len(rows), "caught": sum(p.caught for p in rows)}

    kinds = Counter(p.kind for p in planted)
    mutations = Counter(p.mutation for p in planted)
    return {
        "all": share(planted),
        "by_kind": {k: share([p for p in planted if p.kind == k]) for k in sorted(kinds)},
        "by_mutation": {m: share([p for p in planted if p.mutation == m]) for m in sorted(mutations)},
        "missed": [f"{p.run} {p.item_id} {p.kind} {p.mutation}: {p.verdict.value}, {p.reason}" for p in planted if not p.caught],
    }


def _step(value: float) -> float:
    return max(1.0, 0.25 * abs(value))


def _written(value: float, like: Any) -> str:
    """A number written to the same decimals as the value it replaces."""
    stated = str(like).strip().rstrip("%").rstrip("ms").strip()
    decimals = len(stated.split(".")[1]) if "." in stated else 0
    return f"{value:.{decimals}f}"


def _readable(source: Any) -> bool:
    return source.metrics is not None or source.logs is not None or source.deploys is not None


def _other_text(
    actual: Any, shown: list[Any], every: list[Entry], column: str | None, made_up: bool = False
) -> str | None:
    """Another value these entries show in this column; with made_up, failing that, one made from this one."""
    def flat(value: Any) -> list[Any]:
        return value if isinstance(value, list) else [value]

    here = {str(v) for s in shown for v in flat(s)}
    for e in every:
        for value in flat(field_of(e, column)):
            if value is MISSING or value is None or isinstance(value, (list, dict)) or is_number(value):
                continue
            if str(value) not in here and not any(same(value, s) for s in shown):
                return str(value)
    if not made_up:
        return None
    if isinstance(actual, bool):
        return "false" if actual else "true"
    if isinstance(actual, str) and "->" in actual and " " not in actual:
        old, new = actual.split("->", 1)
        if old != new:
            return f"{new}->{old}"
    version = _VERSION.fullmatch(str(actual))
    if version:
        return f"{version[1]}.{int(version[2]) + 1}.{version[3]}"
    frame = _FRAME.fullmatch(str(actual)) or (isinstance(actual, list) and _FRAME.fullmatch(str(actual[-1])))
    if frame:  # a stack: its innermost frame one line down
        return f"{frame[1]}:{int(frame[2]) + 1}{frame[3]}"
    return None


def _iso(moment: datetime) -> str:
    return f"{moment:%Y-%m-%dT%H:%M:%S.%f}"[:-3] + "Z"
