"""The trial's five marks, read from a sweep folder built from a real replay's transcript."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from checker.__main__ import main as checker_main
from checker.trial import BASELINE_MEDIAN_USD, rejected_answers, render, trial
from test_main import REPLAY, investigating, replay_session, run

SCENARIO = "deploy-1027-1905"  # the replay's alarm


@pytest.fixture(scope="module")
def transcript(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    path = tmp_path_factory.mktemp("replay") / "run.json"
    assert run(REPLAY, replay_session(), investigating(), path)[0] == 0
    return json.loads(path.read_text(encoding="utf-8"))


def sweep(folder: Path, runs: list[tuple[str, float, dict[str, Any]]]) -> Path:
    """A sweep folder: one record and transcript per (status, cost, transcript)."""
    records = []
    for i, (status, cost, body) in enumerate(runs, 1):
        name = f"{SCENARIO}-r{i}"
        (folder / f"{name}.json").write_text(json.dumps(body), encoding="utf-8")
        records.append({"run": name, "scenario": SCENARIO, "attempt": 1, "status": status, "cost_usd": cost, "transcript": f"{name}.json"})
    (folder / "sweep.json").write_text(json.dumps({"records": records}), encoding="utf-8")
    return folder


def with_rejections(body: dict[str, Any], n: int) -> dict[str, Any]:
    out = copy.deepcopy(body)
    for i in range(n):
        out["messages"] += [
            {"role": "assistant", "content": [{"toolUse": {"toolUseId": f"h{i}", "name": "Hypothesis", "input": {}}}]},
            {"role": "user", "content": [{"toolResult": {"toolUseId": f"h{i}", "status": "error", "content": [{"text": "Validation failed"}]}}]},
        ]
    return out


def halted(body: dict[str, Any]) -> dict[str, Any]:
    return {**body, "hypothesis": {**body["hypothesis"], "evidence": [], "halted_by": "resend_limit"}}


def marks(result) -> dict[str, bool]:
    return {mark.name: mark.passed for mark in result.marks}


def test_nine_good_runs_pass_every_mark(transcript: dict[str, Any], tmp_path: Path) -> None:
    result = trial(sweep(tmp_path, [("answered", 0.0060, transcript)] * 9))
    assert result.passed, [(m.name, m.value) for m in result.marks]
    assert [m.value for m in result.marks] == ["0 of 9", "18 of 18 (100%)", "18 of 18 (100%)", "9 of 9", "$0.0060 (1.10x)"]


def test_one_run_without_an_answer_passes_and_two_fail(transcript: dict[str, Any], tmp_path: Path) -> None:
    one = [("halted", 0.0100, with_rejections(halted(transcript), 3))] + [("answered", 0.0060, transcript)] * 8
    result = trial(sweep(tmp_path, one))
    assert result.passed and result.runs[0].resends == 3 and result.runs[0].check is None
    two = [("halted", 0.0100, halted(transcript)), ("error", 0.0, {})] + [("answered", 0.0060, transcript)] * 7
    result = trial(sweep(tmp_path, two))
    assert marks(result)["runs without a valid answer"] is False
    assert marks(result)["diagnoses right, by the rubric"] is False  # 7 of 9


def test_citations_that_cant_be_followed_and_inferences_fail_their_marks(transcript: dict[str, Any], tmp_path: Path) -> None:
    unfollowed = copy.deepcopy(transcript)
    for item in unfollowed["hypothesis"]["evidence"]:
        item["source"] = "m7"
    result = trial(sweep(tmp_path, [("answered", 0.0060, unfollowed)] * 2 + [("answered", 0.0060, transcript)] * 7))
    assert marks(result)["citations followed"] is False  # 14 of 18
    # Claims left as inferences: 2 typed items beside 2 inferences is half, under two-thirds.
    inferences = [{"claim": f"inferred {i}", "rests_on": ["e1"]} for i in range(2)]
    untyped = {**transcript, "hypothesis": {**transcript["hypothesis"], "inferences": inferences}}
    result = trial(sweep(tmp_path, [("answered", 0.0060, untyped)] * 9))
    assert marks(result)["claims typed, not inferences"] is False
    assert [m.value for m in result.marks][2] == "18 of 36 (50%)"


def test_a_run_that_costs_past_one_and_a_half_times_the_baseline_fails(transcript: dict[str, Any], tmp_path: Path) -> None:
    over = round(BASELINE_MEDIAN_USD * 1.5 + 0.0001, 4)
    result = trial(sweep(tmp_path, [("answered", over, transcript)] * 9))
    assert marks(result)["median cost of an answered run"] is False


def test_resends_are_counted_from_the_answers_sent_back(transcript: dict[str, Any]) -> None:
    assert rejected_answers(transcript) == 0
    assert rejected_answers(with_rejections(transcript, 2)) == 2


def test_answers_asked_for_again_as_a_tool_call_are_resends_too(transcript: dict[str, Any]) -> None:
    assert transcript["answers_in_text"] == 0  # every run records its own count
    assert rejected_answers({**transcript, "answers_in_text": 2}) == 2
    assert rejected_answers({**with_rejections(transcript, 1), "answers_in_text": 2}) == 3
    before = {k: v for k, v in transcript.items() if k != "answers_in_text"}  # a transcript from before the count
    assert rejected_answers(with_rejections(before, 1)) == 1


def test_the_cli_prints_the_marks_and_exits_on_the_verdict(transcript: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "good").mkdir()
    (tmp_path / "bad").mkdir()
    good = sweep(tmp_path / "good", [("answered", 0.0060, transcript)] * 9)
    assert checker_main(["--trial", str(good)]) == 0
    printed = capsys.readouterr().out
    assert "PASS  citations followed: 18 of 18 (100%) (bar: at least 90%)" in printed
    assert printed.rstrip().endswith("trial passed")
    bad = sweep(tmp_path / "bad", [("error", 0.0, {})] * 9)
    assert checker_main(["--trial", str(bad)]) == 1
    assert capsys.readouterr().out.rstrip().endswith("trial failed")


def test_trims_are_counted_next_to_resends(transcript: dict[str, Any], tmp_path: Path) -> None:
    trimmed = {**with_rejections(transcript, 1), "trims": ['</root_cause>\n<parameter name="confidence">0.9']}
    before_trims = {k: v for k, v in transcript.items() if k != "trims"}  # a transcript from before the trim
    result = trial(sweep(tmp_path, [("answered", 0.0060, trimmed), ("answered", 0.0060, before_trims)] + [("answered", 0.0060, transcript)] * 7))

    assert [(r.resends, r.trims) for r in result.runs[:3]] == [(1, 1), (0, 0), (0, 0)]
    printed = render(result)
    assert f"{SCENARIO}-r1: answered, 1 resends, 1 trims, $0.0060" in printed
    assert "resends: 1, in 1 runs; trims: 1, in 1 runs" in printed
