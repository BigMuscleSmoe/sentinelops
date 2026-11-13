"""The grader, held to the hand-audited claims of 12 saved runs.

`fixtures/grader/runs.json.gz` holds each run's alarm, tool calls and results,
and Hypothesis (no reasoning, costs, or timings). `slips.json` labels every
claim past the data the audits named, and the ones the grader found that the
audits missed, each checked by hand. The rules were written against these same
runs, so a pass here is an in-sample upper bound, not a measured miss rate.
"""

import copy
import gzip
import json
from pathlib import Path

import pytest

from grader import grade
from grader.checks import Verdict, close, precision
from grader.claims import Kind, Scope, ValueClaim, extract, tokens

FIXTURES = Path(__file__).parent / "fixtures" / "grader"
RUNS = json.loads(gzip.decompress((FIXTURES / "runs.json.gz").read_bytes()).decode("utf-8"))
SLIPS = json.loads((FIXTURES / "slips.json").read_text(encoding="utf-8"))["slips"]
GRADES = {name: grade(run) for name, run in RUNS.items()}
FLAGGED = {Verdict.UNSUPPORTED, Verdict.CONTRADICTED}


def _findings(slip: dict) -> list:
    return [f for f in GRADES[slip["run"]].findings if f.where == slip["where"] and slip["match"] in f.text]


def _case(slip: dict):
    marks = []
    if slip["expect"] == "missed":
        marks = [pytest.mark.xfail(strict=True, reason=slip["note"])]
    return pytest.param(slip, id=f"{slip['run']}-{slip['where']}-{slip['match']}", marks=marks)


@pytest.mark.parametrize("slip", [_case(s) for s in SLIPS if s["expect"] in ("flagged", "missed")])
def test_slip_is_flagged(slip):
    verdicts = [f.verdict for f in _findings(slip)]
    assert any(v in FLAGGED for v in verdicts), f"{slip['quote']!r}: {verdicts}"


@pytest.mark.parametrize("slip", [_case(s) for s in SLIPS if s["expect"] == "loose"])
def test_slip_is_loose(slip):
    assert [f.verdict for f in _findings(slip)] == [Verdict.LOOSE], slip["quote"]


def test_nothing_else_is_flagged():
    """Every flagged or loose finding is a labeled slip: no false flags on these runs."""
    labeled = {(s["run"], s["where"], s["match"]) for s in SLIPS if s["expect"] != "missed"}
    stray = []
    for name, g in GRADES.items():
        for f in g.findings:
            if f.verdict == Verdict.SUPPORTED:
                continue
            if not any(r == name and w == f.where and m in f.text for r, w, m in labeled):
                stray.append((name, f.where, f.verdict.value, f.text, f.detail))
    assert not stray


def test_every_tool_result_is_read():
    assert all(not g.unread for g in GRADES.values())


def test_audit_catch_rate():
    """26 of the 29 strict slips the audits named; 2 of their 5 loose ones."""
    strict = [s for s in SLIPS if s["source"] == "audit" and s["check"] not in ("comparison",) and "loose" not in s["note"]]
    strict += [s for s in SLIPS if s["source"] == "audit" and s["check"] == "comparison"]
    caught = [s for s in strict if s["expect"] == "flagged"]
    loose = [s for s in SLIPS if s["source"] == "audit" and "audited loose" in s["note"]]
    assert (len(caught), len(strict)) == (26, 29)
    assert (sum(s["expect"] == "loose" for s in loose), len(loose)) == (2, 5)


def test_a_clean_hypothesis_gets_no_flags():
    """Replay 1 with its one slip and its loose figure corrected."""
    run = copy.deepcopy(RUNS["20261028T194139Z"])
    h = run["hypothesis"]
    h["root_cause"] = h["root_cause"].replace("in about 0.2 ms", "in under 7 ms")
    h["evidence"][6] = (
        "Database is not the cause: db_query_p50_ms ran 41.1-97.0 ms across the window, "
        "and the failing requests never reach the database."
    )
    g = grade(run)
    assert [f for f in g.findings if f.verdict != Verdict.SUPPORTED] == []
    assert len(g.findings) > 50
    assert not g.unparsed


# --- units ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stated, text, actual, ok",
    [
        (30, "30", 28.7, True),  # a round number stands for its tens
        (1500, "1,500", 1498, True),
        (20, "20", 16, False),  # but not past a tenth of itself
        (61, "61", 60.2, True),
        (8.5, "8.5", 8.51, True),
        (8.5, "8.5", 8.6, True),  # rounded or cut: 8.5 can be 8.59
        (8.5, "8.5", 8.7, False),
        (0, "0", 0.5, True),
        (0, "0", 0.6, False),
        (0, "0.0", 0.05, True),
        (0, "0.0", 0.5, False),
    ],
)
def test_close(stated, text, actual, ok):
    assert close(stated, text, actual, hedged=False) is ok


def test_precision():
    assert (precision("8.5"), precision("61"), precision("1,500"), precision("0")) == (0.1, 1.0, 100.0, 1.0)


def test_a_thousands_comma_keeps_its_unit():
    num = [t for t in tokens("pool wait rose to about 1,500 ms") if t.kind == Kind.NUM][0]
    assert (num.lo, num.unit) == (1500, "ms")


def _values(text: str) -> list[ValueClaim]:
    return [c for c in extract("e", text)[0] if isinstance(c, ValueClaim)]


def test_values_pair_with_times_respectively():
    claims = _values("Metrics: the filling 20:00:30 and 20:00:40 buckets had 26.1% and 15.2%.")
    assert [(c.lo, c.window.times) for c in claims] == [(26.1, ("20:00:30",)), (15.2, ("20:00:40",))]


def test_an_exception_is_left_out_of_the_window():
    (claim,) = _values("Metrics: 18:45-19:04 were at 0% errors except the 18:59-19:02 incident below.")
    assert claim.window.scope == Scope.SPAN
    assert claim.window.except_times == ("18:59-19:02",)


def test_an_opening_phrase_scopes_its_sentence():
    claims = _values("Since 19:03, pool wait is 0.3-0.5 ms and errors are 0%, apart from the new 19:05 errors.")
    assert [(c.window.scope, c.window.times, c.window.except_times) for c in claims] == [
        (Scope.SINCE, ("19:03",), ()),
        (Scope.SINCE, ("19:03",), ("19:05",)),
    ]


def test_a_parenthetical_takes_its_own_time():
    claims = _values("Alarm: error rate crossed 5% at 19:05:20 (8.5%) and 19:05:30 (6.0%).")
    assert [(c.lo, c.window.times) for c in claims] == [(8.5, ("19:05:20",)), (6.0, ("19:05:30",))]


def test_a_threshold_is_not_a_claim():
    assert _values("Errors pushed the 10-second error rate above the 5% threshold.") == []


def test_a_status_code_is_not_the_tail_of_a_number():
    (claim,) = _values("The change produced pool waits of about 1,500 ms from 18:57 to 19:02.")
    assert claim.column.source == "metric"
    assert claim.log_filter == {}
