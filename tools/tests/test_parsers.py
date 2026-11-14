"""The parsers read each tool's text back into the data it was rendered from.

Round trips on every recorded window, at every alarm in it: the source data
(CloudWatch's series, the log events, the deploy items) goes through the tool's
renderer and back through its parser, and must come out as it went in, to the
precision the text shows. The eval grader trusts these parsers, so a renderer
change that a parser misses fails here, not in a graded run.
"""

from datetime import UTC, datetime, timedelta

import pytest
from conftest import FIXTURES
from window import WINDOWS, Window

from sentinelops_tools.deploys import _record, history, parse_history
from sentinelops_tools.logs import parse_lines, recent_lines
from sentinelops_tools.metrics import COLUMNS, _ceil, _cell, _query, metrics_table, parse_table

NAMES = sorted(p.name for p in WINDOWS.iterdir() if p.is_dir())
METRIC_ARGS = [(10, 10), (20, 10), (30, 60), (5, 60), (200, 60)]
LOG_ARGS = [
    {"minutes": 10, "limit": 20},
    {"minutes": 15, "limit": 30},
    {"minutes": 3, "errors_only": False, "route": "POST /checkout", "limit": 40},
    {"minutes": 5, "errors_only": False, "limit": 200},
    {"minutes": 1, "errors_only": True, "route": "GET /products/{id}", "limit": 5},
]


@pytest.fixture(scope="module", params=NAMES)
def window(request) -> Window:
    return Window(request.param)


def _at(clock_text: str, now: datetime) -> datetime:
    """A table's HH:MM:SS as a datetime on `now`'s day, or the day before if that would be after now."""
    h, m, s = map(int, clock_text.split(":"))
    moment = now.replace(hour=h, minute=m, second=s, microsecond=0)
    return moment - timedelta(days=1) if moment > now + timedelta(minutes=5) else moment


def test_metrics_round_trip(window: Window) -> None:
    for replay in window.replays:
        env = window.env(replay.fired_at)
        for minutes, period in METRIC_ARGS:
            text = metrics_table(env, minutes, period)
            page = parse_table(text)
            assert page is not None, text[:200]
            if page.period_s is None:
                assert not page.rows
                continue
            start = _at(page.start, replay.fired_at)
            series = _query(env, start, _ceil(env.now(), page.period_s), page.period_s)
            times = sorted({t for values in series.values() for t in values})
            assert [_at(r.time, replay.fired_at) for r in page.rows] == times
            for row in page.rows:
                moment = _at(row.time, replay.fired_at)
                for column in COLUMNS:
                    shown = _cell(column, series[column.id].get(moment))
                    parsed = row.cells[column.header]
                    assert parsed == (None if shown == "-" else float(shown)), (row.time, column.header)
            assert sum(r.unsettled for r in page.rows) == text.count("*") - (1 if "* still" in text else 0)


def test_logs_round_trip(window: Window) -> None:
    by_id = {}
    for record in window.records:
        if record and "request_id" in record:
            record = {k: v for k, v in record.items() if k != "_aws"}
            by_id.setdefault(record["request_id"], record)
    for replay in window.replays:
        env = window.env(replay.fired_at)
        for kwargs in LOG_ARGS:
            text = recent_lines(env, **kwargs)
            page = parse_lines(text)
            assert page is not None, text[:200]
            assert page.errors_only == kwargs.get("errors_only", True)
            assert page.route == kwargs.get("route")
            assert page.cut == ("Only the newest" in text or "were left out" in text)
            stamps = []
            for line in page.lines:
                assert isinstance(line, dict), line[:100]
                assert line == by_id[line["request_id"]]
                if page.errors_only:
                    assert line["status"] >= 500
                if page.route:
                    assert line["route"] == page.route
                stamps.append(line["timestamp"])
            assert stamps == sorted(stamps)
            if page.lines:
                newest = datetime.fromisoformat(stamps[-1]).astimezone(UTC)
                assert _at(page.searched_from, replay.fired_at) <= newest <= _at(page.searched_to, replay.fired_at) + timedelta(seconds=1)


def test_deploys_round_trip(window: Window) -> None:
    items = window.meta["deploys"]["items"]
    for replay in window.replays:
        env = window.env(replay.fired_at)
        for limit in (1, 5, 10, 20):
            page = parse_history(history(env, limit))
            assert page is not None
            seen = sorted((i for i in items if i["ts"]["S"] <= page.up_to), key=lambda i: i["ts"]["S"], reverse=True)
            assert page.records == [_record(i) for i in seen[:limit]]
            assert page.older_exist == (len(seen) > limit)


def test_each_recorded_fixture_text_parses() -> None:
    import json

    parsers = {"metrics": parse_table, "logs": parse_lines, "history": parse_history}
    for path in FIXTURES.glob("*.json"):
        fixture = json.loads(path.read_text(encoding="utf-8"))
        if "text" in fixture:
            assert parsers[path.stem.split("_")[0]](fixture["text"]) is not None, path.name


def test_other_text_is_not_a_page() -> None:
    for parse in (parse_table, parse_lines, parse_history):
        assert parse("") is None
        assert parse("Couldn't read the metrics: AccessDeniedException.") is None
