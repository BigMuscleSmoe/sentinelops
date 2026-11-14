"""The checker's resolver and run report: a run's labeled results read back, each typed item resolved against them.

The run is a real replay (the agent, its real tools on recorded AWS calls, a
fake model), so the labels and line numbers are the ones a live run gets.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from answers import DEPLOY_EVIDENCE, item
from checker.__main__ import check_run, load as load_transcript, main as checker_main, render
from checker.checks import Verdict
from checker.resolve import Status, resolve
from checker.sources import RunSources, read_sources
from test_main import REPLAY, investigating, replay_session, run


@pytest.fixture(scope="module")
def transcript(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    path = tmp_path_factory.mktemp("replay") / "run.json"
    code, _, _ = run(REPLAY, replay_session(), investigating(), path)
    assert code == 0
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def sources(transcript: dict[str, Any]) -> RunSources:
    return read_sources(transcript)


def test_the_replays_results_read_back_by_label(sources: RunSources) -> None:
    assert set(sources.by_label) == {"alarm", "m1", "l1", "d1"} and sources.unlabeled == []
    m1, l1, d1 = sources.by_label["m1"], sources.by_label["l1"], sources.by_label["d1"]
    assert m1.metrics is not None and m1.metrics.period_s == 60 and len(m1.metrics.rows) == 11
    assert l1.logs is not None and len(l1.logs.lines) == 20 and l1.logs.found == 25
    assert d1.deploys is not None and len(d1.deploys.records) == 10 and d1.deploys.older_exist
    assert [value for _, value in sources.by_label["alarm"].points] == [0.0851063829787234, 0.05970149253731343]


def test_the_answer_the_fake_model_gave_resolves_whole(transcript: dict[str, Any]) -> None:
    check = check_run("run", transcript)
    assert [(i.id, i.status) for i in check.items] == [("e1", Status.RESOLVED), ("e2", Status.RESOLVED)]
    assert len(check.items) == len(DEPLOY_EVIDENCE)
    assert check.typed_share == 1.0 and check.resolved_share == 1.0


@pytest.mark.parametrize(
    "evidence, status, detail",
    [
        # Metric rows: by their start, a minute, or a time inside one row's period.
        (item("value", source="m1", column="err%", at="19:01:00", value="19.5"), Status.RESOLVED, ["m1 19:01:00"]),
        (item("value", source="m1", column="err%", at="19:01", value="19.5"), Status.RESOLVED, ["m1 19:01:00"]),
        (item("value", source="m1", column="err%", at="19:01:30", value="19.5"), Status.RESOLVED, ["m1 19:01:00"]),
        (
            item("range", source="m1", column="db_query_p50_ms", start="18:55", end="18:58", low=60.2, high=66.7),
            Status.RESOLVED,
            ["m1 18:55:00", "m1 18:56:00", "m1 18:57:00", "m1 18:58:00"],
        ),
        (item("value", source="m1", column="err%", at="18:40:00", value="0.0"), Status.OUTSIDE_READ, "outside m1's read"),
        (item("none", source="m1", column="err%", start="18:30", end="18:50", value="5"), Status.OUTSIDE_READ, "18:30"),
        (item("value", source="m1", column="cpu%", at="19:01", value="12"), Status.UNRESOLVED, "no column or field 'cpu%'"),
        (item("value", source="m1", column="err%", lines=[1], value="0.0"), Status.UNRESOLVED, "only log results"),
        (item("value", source="m9", column="err%", at="19:01", value="1"), Status.UNRESOLVED, "no result labeled m9"),
        (item("value", source="m1", column="err%", at="25:00", value="1"), Status.UNRESOLVED, "isn't a time"),
        (item("range", source="m1", column="err%", start="19:02", end="19:00", low=0, high=1), Status.UNRESOLVED, "start is after end"),
        # Log lines: by number, or by a time that names one line.
        (item("value", source="l1", column="error_type", lines=[1], value="TypeError"), Status.RESOLVED, ["l1[1]"]),
        (item("value", source="l1", column="has_promo", at="19:05:33.125", value="false"), Status.RESOLVED, ["l1[1]"]),
        (item("value", source="l1", column="has_promo", at="19:05", value="false"), Status.UNRESOLVED, "20 entries"),
        (item("all", source="l1", column="status", lines=[1, 2, 20], value="500"), Status.RESOLVED, ["l1[1]", "l1[2]", "l1[20]"]),
        (item("count", source="l1", column="status", lines=[21], value="500", count=1), Status.UNRESOLVED, "no line 21 (it shows 20)"),
        (item("value", source="l1", column="cpu", lines=[1], value="1"), Status.UNRESOLVED, "no column or field 'cpu'"),
        # 25 found and 20 shown: the read runs from the oldest line shown, not from when the search began.
        (item("none", source="l1", start="19:05:27", end="19:05:30"), Status.OUTSIDE_READ, "19:05:27 and 19:05:30 outside"),
        (item("none", source="l1", start="19:05:34", end="19:05:37"), Status.RESOLVED, []),
        # Resolving finds what an item is about, not whether it holds: this none has a line in its window.
        (item("none", source="l1", start="19:05:38", end="19:05:39"), Status.RESOLVED, ["l1[2]"]),
        # Deploy records: by their timestamp, whole or from the hour; a changed key counts as a field.
        (item("value", source="d1", column="version", at="2026-10-27T19:05:22.912Z", value="1.43.0"), Status.RESOLVED, ["d1 2026-10-27T19:05:22.912Z"]),
        (item("value", source="d1", column="version", at="19:05:22.912", value="1.43.0"), Status.RESOLVED, ["d1 2026-10-27T19:05:22.912Z"]),
        (item("value", source="d1", column="POOL_MAX_SIZE", at="18:56:06.039", value="20->2"), Status.RESOLVED, ["d1 2026-10-27T18:56:06.039Z"]),
        (item("value", source="d1", column="version", at="19:04:00", value="1.43.0"), Status.UNRESOLVED, "nothing at 19:04:00"),
        (item("value", source="d1", column="version", at="2026-09-01T10:00:00Z", value="1.40.0"), Status.OUTSIDE_READ, "outside d1's read"),
        (item("range", source="d1", column="version", start="18:00", end="19:00", low=1, high=2), Status.UNRESOLVED, "doesn't fit"),
        # The alarm's datapoints, and a gap, which needs no source.
        (item("value", source="alarm", column="error", at="19:05:30", value="0.0597"), Status.RESOLVED, ["alarm 19:05:30"]),
        (item("gap", start="18:56:06.039", end="19:00:00", seconds=234), Status.RESOLVED, []),
        (item("count", source="l1", count=20), Status.RESOLVED, []),
    ],
)
def test_each_item_resolves_or_says_why_not(sources: RunSources, evidence: dict[str, Any], status: Status, detail: Any) -> None:
    out = resolve("e1", evidence, sources)
    assert out.status is status, out.reason
    if isinstance(detail, list):
        assert out.points_at == detail
    else:
        assert detail in out.reason


def test_a_tools_error_is_a_source_that_resolves_nothing(transcript: dict[str, Any]) -> None:
    broken = json.loads(json.dumps(transcript))
    for message in broken["messages"]:
        for block in message["content"]:
            if "toolResult" in block and block["toolResult"]["content"][0]["text"].startswith("[source m1]"):
                block["toolResult"]["content"][0]["text"] = "[source m1]\nThe metrics call failed: AccessDenied\n[budget: x remaining]"
    out = resolve("e1", item("value", source="m1", column="err%", at="19:01", value="1"), read_sources(broken))
    assert out.status is Status.UNRESOLVED and "can't read" in out.reason


def test_a_sentence_is_untyped_and_a_result_without_a_label_unlabeled(transcript: dict[str, Any]) -> None:
    """The shape of a run from before typed evidence: sentences, and results the agent never labeled."""
    old = json.loads(json.dumps(transcript))
    old["hypothesis"]["evidence"] = ["Errors started at 19:05, right after the 1.43.0 deploy."]
    for message in old["messages"]:
        for block in message["content"]:
            for part in block.get("toolResult", {}).get("content", []):
                part["text"] = re.sub(r"\A\[source \w+\]\n", "", part["text"])
    check = check_run("old", old)
    assert [i.status for i in check.items] == [Status.UNTYPED] and check.checks[0].verdict is Verdict.UNTYPED
    assert check.resolved_share is None and len(read_sources(old).unlabeled) == 3


def test_a_halted_run_has_nothing_to_check(transcript: dict[str, Any]) -> None:
    halted = {**transcript, "hypothesis": {**transcript["hypothesis"], "evidence": [], "halted_by": "step_limit"}}
    check = check_run("halted", halted)
    assert check.halted and check.items == [] and check.typed_share is None
    assert render([check]).startswith("halted: halted, nothing to check")


def test_the_cli_prints_each_run_and_a_total(transcript: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    wrong = {**transcript, "trims": ["</root_cause>"], "hypothesis": {**transcript["hypothesis"], "evidence": [
        *DEPLOY_EVIDENCE,
        item("none", source="l1", start="19:05:27", end="19:05:30", note="no errors before the deploy"),
        item("value", source="m2", column="err%", at="19:01", value="19.5"),
        item("value", source="m1", column="err%", at="19:01", value="19.4"),
    ], "inferences": [
        {"claim": "the deploy caused it", "rests_on": ["e1", "e2"]},
        {"claim": "it started with the deploy", "rests_on": ["e3", "e5", "e9"]},
    ]}}
    path = tmp_path / "wrong.json"
    path.write_text(json.dumps(wrong), encoding="utf-8")
    assert load_transcript(path) == wrong

    assert checker_main([str(path)]) == 0
    printed = capsys.readouterr().out
    assert (
        "wrong: 5 evidence, 2 inferences (typed 71%, citations followed 80%); 2 hold, 0 loose, 3 flagged "
        "(1 contradicted, 1 outside the read, 1 unresolved);"
    ) in printed
    assert printed.split("\n")[0].endswith("uncited figures in root_cause; 1 trims")
    assert "  e3 none l1: outside_read: 19:05:27 and 19:05:30 outside l1's read" in printed
    assert "  e4 value m2: unresolved: no result labeled m2" in printed
    assert "  e5 value m1: contradicted: err% at m1 19:01:00 is 19.5" in printed
    assert "  i2 rests on e3 (outside_read), e5 (contradicted), e9 (no such item)" in printed
    assert "4 of 5 typed items' citations followed (80%), 1 outside the read" in printed
    assert printed.rstrip().split("\n")[-1].startswith("checks: 2 hold, 0 loose, 3 flagged")

    assert checker_main([str(path), "--json"]) == 0
    (row,) = json.loads(capsys.readouterr().out)
    assert row["typed_share"] == 5 / 7 and row["trims"] == 1
    assert [i["verdict"] for i in row["items"]] == ["holds", "holds", "outside_read", "unresolved", "contradicted"]
