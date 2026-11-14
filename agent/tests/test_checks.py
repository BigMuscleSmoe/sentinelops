"""The checks per kind, the planted errors, and the uncited-figure count (BUILD_PLAN.md, "The grader redesign", step 4).

Two sets of runs. The replay (the agent and its real tools on a recorded
window, with a fake model) gives known data for a check per kind. The fixture
(the three trials' 26 answered runs, `checked_runs`) gives the items Haiku 5.5
actually wrote: every one's verdict is pinned, and every one that holds is
corrupted in code, each mutant to be flagged. No model, no AWS.
"""

import json
from typing import Any

import pytest

from answers import item
from checked_runs import PINNED, RUNS
from checker.__main__ import main as checker_main
from checker.checks import Verdict, check_all, check_item, number, same
from checker.figures import figures, uncited
from checker.plant import plant_run, recall
from checker.resolve import resolve
from checker.sources import RunSources, read_sources
from test_main import REPLAY, investigating, replay_session, run


@pytest.fixture(scope="module")
def sources(tmp_path_factory: pytest.TempPathFactory) -> RunSources:
    path = tmp_path_factory.mktemp("replay") / "run.json"
    code, _, _ = run(REPLAY, replay_session(), investigating(), path)
    assert code == 0
    return read_sources(json.loads(path.read_text(encoding="utf-8")))


# The replay's data, as the tests below read it (deploy-1027-1905's window):
#   m1  60 s rows 18:55 to 19:05; err% 0 to 18:58, then 0.5, 8.1, 19.5, 21.1, 0, 0, 10.1;
#       p99_ms 642, 860, 757, 1469, 1648, 1938, ...
#   l1  20 of 25 lines found, 19:05:33.125 to 19:05:55.310: every one a 500, TypeError,
#       has_promo false, latency_ms 0.2 but for 6.8 (l1[14]) and 5.0 (l1[16])
#   d1  10 records to 19:05:57.026, older ones exist: the 1.43.0 deploy at 19:05:22.912,
#       POOL_MAX_SIZE 2->20 at 19:03:31.782 and 20->2 at 18:56:06.039, ...
#   the alarm's datapoints 0.0851 at 19:05:20 and 0.0597 at 19:05:30
@pytest.mark.parametrize(
    "evidence, verdict, why",
    [
        # value: one row, line, record, or datapoint; a number within half a step of its last digit.
        (item("value", source="m1", column="err%", at="19:01", value="19.5"), Verdict.HOLDS, "is 19.5"),
        (item("value", source="m1", column="err%", at="19:01", value="19.5%"), Verdict.HOLDS, "is 19.5"),
        (item("value", source="m1", column="err%", at="19:01", value="19.4"), Verdict.CONTRADICTED, "is 19.5"),
        (item("value", source="m1", column="err%", at="19:01", value="20"), Verdict.HOLDS, ""),
        (item("value", source="l1", column="latency_ms", lines=[14], value="7"), Verdict.HOLDS, "is 6.8"),
        (item("value", source="l1", column="latency_ms", lines=[14], value="6.5"), Verdict.CONTRADICTED, "is 6.8"),
        (item("value", source="l1", column="has_promo", lines=[1], value="false"), Verdict.HOLDS, ""),
        (item("value", source="l1", column="has_promo", lines=[1], value="true"), Verdict.CONTRADICTED, "is false"),
        (item("value", source="l1", column="error_type", lines=[1], at="19:05:38.539", value="TypeError"), Verdict.CONTRADICTED, "is at 19:05:33.125"),
        (item("value", source="l1", column="cpu", lines=[1], value="1"), Verdict.UNRESOLVED, "no column"),
        # A changed key as old->new, or its new value; a deploy's version likewise.
        (item("value", source="d1", column="POOL_MAX_SIZE", at="18:56:06.039", value="20->2"), Verdict.HOLDS, ""),
        (item("value", source="d1", column="POOL_MAX_SIZE", at="18:56:06.039", value="20 -> 2"), Verdict.HOLDS, ""),
        (item("value", source="d1", column="POOL_MAX_SIZE", at="18:56:06.039", value="2"), Verdict.HOLDS, ""),
        (item("value", source="d1", column="POOL_MAX_SIZE", at="18:56:06.039", value="2->20"), Verdict.CONTRADICTED, "is 20->2"),
        (item("value", source="d1", column="version", at="19:05:22.912", value="1.43.0"), Verdict.HOLDS, ""),
        (item("value", source="d1", column="version", at="19:05:22.912", value="1.42.0"), Verdict.CONTRADICTED, ""),
        (item("value", source="d1", column="changes", at="18:56:06.039", value="POOL_MAX_SIZE 20->2"), Verdict.HOLDS, ""),
        (item("value", source="d1", column="kind", at="19:05:22.912", value="deploy 1.43.0"), Verdict.LOOSE, "is deploy"),
        # The alarm's fractions, as written or as a percentage.
        (item("value", source="alarm", column="error rate", at="19:05:30", value="0.0597"), Verdict.HOLDS, ""),
        (item("value", source="alarm", column="error rate", at="19:05:30", value="6.0%"), Verdict.HOLDS, ""),
        (item("value", source="alarm", column="error rate", at="19:05:30", value="8.5%"), Verdict.CONTRADICTED, ""),
        # range: the smallest and largest in the window, which includes its end.
        (item("range", source="m1", column="p99_ms", start="18:56", end="19:00", low=757, high=1938), Verdict.HOLDS, "757 to 1938 over 5 rows"),
        (item("range", source="m1", column="p99_ms", start="18:56", end="19:00", low=860, high=1938), Verdict.CONTRADICTED, "757 to 1938"),
        (item("range", source="l1", column="latency_ms", lines=list(range(1, 21)), low=0.2, high=6.8), Verdict.HOLDS, "over 20 lines"),
        # all: every line of the result with neither lines nor a window.
        (item("all", source="l1", column="status", value="500"), Verdict.HOLDS, "all 20 lines"),
        (item("all", source="l1", column="latency_ms", value="0.2"), Verdict.CONTRADICTED, "2 of 20 lines have another latency_ms: l1[14] has 6.8"),
        (item("all", source="l1", column="latency_ms", value="0.1-7"), Verdict.LOOSE, ""),
        # count: shown, or found for a whole log read; a column's matches.
        (item("count", source="l1", count=20), Verdict.HOLDS, "20 lines; it found 25"),
        (item("count", source="l1", count=25), Verdict.HOLDS, "found 25 and shows 20"),
        (item("count", source="l1", count=21), Verdict.CONTRADICTED, ""),
        (item("count", source="m1", column="err%", value="0.0", start="18:55", end="19:05", count=6), Verdict.HOLDS, "6 of 11 rows"),
        (item("count", source="m1", column="err%", value="0.0", start="18:55", end="19:05", count=5), Verdict.CONTRADICTED, "6 of 11 rows"),
        # Stated to the unit, "0" holds for 18:59's 0.5 too: half a step, the edge included.
        (item("count", source="m1", column="err%", value="0", start="18:55", end="19:05", count=7), Verdict.HOLDS, "7 of 11 rows"),
        (item("count", source="d1", column="kind", value="deploy", count=5), Verdict.HOLDS, "5 of 10 records"),
        # A coarse end read both ways: through 19:05:38 (2 lines) or to its instant (1).
        (item("count", source="l1", start="19:05:33", end="19:05:38", count=2), Verdict.HOLDS, ""),
        (item("count", source="l1", start="19:05:33", end="19:05:38", count=1), Verdict.HOLDS, "the instant of its end"),
        (item("count", source="l1", start="19:05:33", end="19:05:38", count=3), Verdict.CONTRADICTED, ""),
        # first and last: among the matches when a column is given; a column alone means set.
        (item("first", source="m1", column="err%", at="18:59"), Verdict.HOLDS, "18:59:00"),
        (item("first", source="m1", column="err%", at="19:00"), Verdict.CONTRADICTED, "18:59:00"),
        (item("first", source="l1", at="19:05:33.125"), Verdict.HOLDS, ""),
        (item("last", source="l1", at="19:05:55.310"), Verdict.HOLDS, ""),
        (item("last", source="l1", at="19:05:54.851"), Verdict.CONTRADICTED, "19:05:55.310"),
        (item("first", source="d1", column="kind", value="deploy", start="2026-10-27T00:00:00Z", end="19:05:57", at="03:01:46.357"), Verdict.HOLDS, ""),
        # none: nothing in the window; exact bounds that name records hold neither.
        (item("none", source="l1", start="19:05:34", end="19:05:37"), Verdict.HOLDS, "0 lines in it"),
        (item("none", source="l1", start="19:05:38", end="19:05:39"), Verdict.CONTRADICTED, "the first l1[2]"),
        (item("none", source="d1", start="2026-10-27T14:19:02.761Z", end="2026-10-27T18:56:06.039Z"), Verdict.HOLDS, ""),
        (item("none", source="d1", column="POOL_MAX_SIZE", start="14:20", end="18:55"), Verdict.HOLDS, ""),
        (item("none", source="d1", column="POOL_MAX_SIZE", start="14:20", end="19:00"), Verdict.CONTRADICTED, "with POOL_MAX_SIZE 20->2"),
        (item("none", source="d1", column="POOL_MAX_SIZE", start="14:20", end="18:56"), Verdict.HOLDS, "the instant of its end"),
        (item("none", source="l1", column="has_promo", value="true", start="19:05:34", end="19:05:57"), Verdict.HOLDS, ""),
        # l1 shows 20 of the 25 it found, so its read starts at the oldest line shown.
        (item("none", source="l1", column="has_promo", value="true", start="19:05:30", end="19:05:57"), Verdict.OUTSIDE_READ, "19:05:33.125 to"),
        # gap: arithmetic, within the step of seconds plus what the times leave open.
        (item("gap", start="18:56:06.039", end="19:05:22.912", seconds=557), Verdict.HOLDS, "556.873 s"),
        (item("gap", start="18:56:06.039", end="19:05:22.912", seconds=556), Verdict.CONTRADICTED, ""),
        (item("gap", start="18:56", end="19:05", seconds=600), Verdict.HOLDS, ""),
        (item("gap", start="18:56", end="19:05", seconds=700), Verdict.CONTRADICTED, ""),
        # The resolver's verdicts pass through.
        (item("value", source="m1", column="err%", at="18:40:00", value="0.0"), Verdict.OUTSIDE_READ, "outside m1's read"),
        ("errors rose after the deploy", Verdict.UNTYPED, ""),
    ],
)
def test_each_kind_is_checked_on_its_own_fields(sources: RunSources, evidence: Any, verdict: Verdict, why: str) -> None:
    check = check_item("e1", evidence, sources)
    assert check.verdict is verdict, check.reason
    assert why in check.reason


def test_fields_beyond_the_kind_are_listed_unchecked(sources: RunSources) -> None:
    check = check_item("e1", item("value", source="m1", column="err%", at="19:01", value="19.5", low=1, high=2, count=3), sources)
    assert check.verdict is Verdict.HOLDS and check.unchecked == ["low", "high", "count"]


def test_stated_numbers_carry_their_own_tolerance() -> None:
    assert number("19.5") == (19.5, 0.05) and number("1,500 ms") == (1500.0, 0.5) and number(66.0) == (66.0, 0.5)
    assert number("12%") == (12.0, 0.5) and number("abc") is None and number(True) is None
    assert same("checkout/pricing.py:11 in price_cart -> checkout/pricing.py:15 in apply_promo", ["a", "checkout/pricing.py:11 in price_cart", "checkout/pricing.py:15 in apply_promo"])
    assert not same("checkout/pricing.py:15 in apply_promo -> checkout/pricing.py:11 in price_cart", ["checkout/pricing.py:11 in price_cart", "checkout/pricing.py:15 in apply_promo"])
    assert same("null", None) and not same("0", None)


# --- the fixture: what the model actually wrote --------------------------------


def test_every_fixture_verdict_is_pinned() -> None:
    got = {name: {c.id: c.verdict.value for c in check_all(t["hypothesis"], read_sources(t))} for name, t in sorted(RUNS.items())}
    assert got == PINNED["verdicts"]


def test_every_flagged_original_was_read_by_hand() -> None:
    flagged = {
        f"{name} {item_id}"
        for name, verdicts in PINNED["verdicts"].items()
        for item_id, verdict in verdicts.items()
        if verdict in ("contradicted", "outside_read", "unresolved")
    }
    assert flagged == set(PINNED["flagged"]) and all(PINNED["flagged"].values())
    assert len(flagged) == 31 and sum(len(v) for v in PINNED["verdicts"].values()) == 366


def test_every_planted_error_is_flagged() -> None:
    planted, unplanted = [], []
    for name, t in sorted(RUNS.items()):
        found, none = plant_run(name, t["hypothesis"], read_sources(t))
        planted += found
        unplanted += none
    result = recall(planted)
    assert result["missed"] == [] and unplanted == []
    assert result["all"]["planted"] == 701, "the mutations changed: check the recall by kind and mutation"
    assert set(result["by_kind"]) == {"value", "range", "all", "count", "first", "none"}
    assert set(result["by_mutation"]) == {"number", "text", "window", "count", "moved", "shifted"}


def test_planted_errors_cover_the_kinds_no_trial_item_used(sources: RunSources) -> None:
    """No trial item is a gap or a last, so the planter is tried on the replay's."""
    hypothesis = {"evidence": [
        item("gap", start="18:56:06.039", end="19:05:22.912", seconds=557),
        item("last", source="l1", at="19:05:55.310"),
        item("first", source="m1", column="err%", at="18:59"),
    ]}
    planted, unplanted = plant_run("replay", hypothesis, sources)
    assert unplanted == [] and all(p.caught for p in planted)
    assert {(p.kind, p.mutation) for p in planted} == {
        ("gap", "seconds"), ("last", "moved"), ("last", "window"), ("first", "moved"), ("first", "window"),
    }


def test_the_resolvers_two_misses_hold_now() -> None:
    """Trial 1's misses: lines in a log read's last second ("to 18:56:46" beside 18:56:46.522), and a count of 0 on an empty read."""
    t = RUNS["20261114T055051Z/pool-1027-1856-r1"]
    sources = read_sources(t)
    evidence = t["hypothesis"]["evidence"]
    for i in (7, 8):
        check = check_item(f"e{i}", evidence[i - 1], sources)
        assert check.verdict is Verdict.HOLDS and "over 20 lines" in check.reason
    empty = evidence[8]
    assert (empty["kind"], empty["count"], sources.by_label[empty["source"]].logs.lines) == ("count", 0, [])
    assert check_item("e9", empty, sources).verdict is Verdict.HOLDS
    assert resolve("e9", empty, sources).reason == ""


def test_the_cli_reports_the_planted_recall(capsys: pytest.CaptureFixture[str], tmp_path: Any) -> None:
    path = tmp_path / "run.json"
    path.write_text(json.dumps(RUNS["20261114T190302Z/deploy-1113-0257-r1"]), encoding="utf-8")
    assert checker_main([str(path), "--plant"]) == 0
    printed = capsys.readouterr().out
    assert printed.startswith("planted errors caught: ") and "(100%)" in printed and "by mutation:" in printed
    assert printed.rstrip().endswith("items that hold with no mutant: 0")


# --- uncited figures -----------------------------------------------------------


def test_figures_are_times_versions_and_numbers_not_identifiers() -> None:
    found = figures("At 18:56:06 POOL_MAX_SIZE went 20->2 (1.43.0 was rolled back at 03:06); p99 1,500 ms, 5xx, 12.5%, checkout/db.py:19")
    assert found.times == ("18:56:06", "03:06") and found.versions == ("1.43.0",)
    assert found.numbers == ("20", "2", "1500", "12.5")


def test_a_figure_no_item_states_is_uncited() -> None:
    hypothesis = {
        "root_cause": "At 18:56 POOL_MAX_SIZE went from 20 to 2, and p99 reached 1,938 ms by 19:00 and 2,049 ms by 19:02.",
        "evidence": [
            item("value", source="d1", column="POOL_MAX_SIZE", at="2026-10-27T18:56:06.039Z", value="20->2"),
            item("range", source="m1", column="p99_ms", start="18:56", end="19:00", low=757, high=1938),
        ],
    }
    assert uncited(hypothesis) == ["19:02", "2049"]


def test_the_fixture_runs_uncited_figures() -> None:
    counts = [len(uncited(t["hypothesis"])) for t in RUNS.values()]
    assert sum(counts) == 43 and len(counts) == 26
