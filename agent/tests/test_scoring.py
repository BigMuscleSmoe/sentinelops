"""The redesign's step 5: labels by ID, the scorer, the pass bar, the labeling packet, the comparison, and the arms.

No model and no AWS. The sweep scored here is trial 3 (20261114T190302Z) from
the checker's fixture, written out as a sweep folder, with hand labels made up
for each test: what's measured is the arithmetic and the refusals, not the runs.
"""

import asyncio
import copy
import dataclasses
import json
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from checked_runs import RUNS
from checker.__main__ import check_run
from evals import audit, packet, repo, score, sweep
from evals import __main__ as evals_cli
from evals.__main__ import main as evals_main
from evals.arms import ARMS, Arm
from evals.scenarios import BY_ID, WINDOW_NOTES, Split
from fake_anthropic import FEDERATION_IDS, FakeAnthropic, Reply, answer, tool_use
from guards import Limits
from pricing import price_for
from providers import Settings
from stubs import FakeSession

TRIAL = "20261114T190302Z"
TRIAL_RUNS = {key.split("/", 1)[1]: run for key, run in RUNS.items() if key.startswith(TRIAL + "/")}
CHECKS = {name: check_run(name, run) for name, run in TRIAL_RUNS.items()}
FLAGS = {name: {c.id: c.verdict.value for c in check.flagged} for name, check in CHECKS.items()}
HALTED = {
    "root_cause": "The run stopped at its cost limit before reaching a diagnosis.",
    "confidence": 0.0,
    "evidence": [],
    "affected_component": "unknown",
    "fault_location": "undetermined",
    "halted_by": "cost_limit",
}


def _sweep_dir(
    tmp_path: Path, *, halted: bool = False, error: bool = False, meta: dict[str, Any] | None = None, after_ask: dict[str, Any] | None = None
) -> Path:
    """Trial 3's runs as a sweep folder, optionally with a halted run and an errored one beside them.

    Trial 3's transcripts are from before tool_calls_after_ask was recorded: after_ask gives some runs one."""
    folder = tmp_path / TRIAL
    folder.mkdir(parents=True)
    records = []
    for name, run in TRIAL_RUNS.items():
        if after_ask and name in after_ask:
            run = {**run, "tool_calls_after_ask": after_ask[name]}
        (folder / f"{name}.json").write_text(json.dumps(run), encoding="utf-8")
        records.append({"run": name, "scenario": run["eval"]["scenario"], "attempt": 1, "status": "answered", "cost_usd": 0.006, "transcript": f"{name}.json"})
    if halted:
        (folder / "pool-1027-1856-r2.json").write_text(json.dumps({"hypothesis": HALTED, "messages": []}), encoding="utf-8")
        records.append({"run": "pool-1027-1856-r2", "scenario": "pool-1027-1856", "attempt": 1, "status": "halted", "cost_usd": 0.1, "transcript": "pool-1027-1856-r2.json"})
    if error:
        records.append({"run": "pool-1027-1900-r2", "scenario": "pool-1027-1900", "attempt": 1, "status": "error", "cost_usd": 0.0, "transcript": None})
    (folder / "sweep.json").write_text(json.dumps({"sweep": TRIAL, "arm": "haiku", "model": "claude-haiku-5-5", "records": records, **(meta or {})}), encoding="utf-8")
    return folder


def _clean_audit(runs: list[str] | None = None) -> dict[str, Any]:
    names = runs if runs is not None else list(TRIAL_RUNS)
    return {"sweep": TRIAL, "runs": {name: {"diagnosis": "right", "note": "", "slips": []} for name in names}}


def _slip(target: str, complaint: str, kind: str = "strict", note: str = "a note") -> dict[str, str]:
    return {"target": target, "kind": kind, "complaint": complaint, "note": note}


def _write(tmp_path: Path, name: str, data: dict[str, Any]) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _bar(tmp_path: Path, **changes: Any) -> Path:
    bar = {**json.loads(score.PASS_BAR.read_text(encoding="utf-8")), **changes}
    return _write(tmp_path, "bar.json", bar)


def _committed(_: Path) -> str:
    return repo.head() or "0" * 40


def _calls(call: str = "right") -> dict[str, Any]:
    return {"sweep": TRIAL, "flags": {run: {i: {"verdict": v, "call": call, "note": ""} for i, v in f.items()} for run, f in FLAGS.items() if f}}


# --- targets and the audit tool ---------------------------------------------------


def test_root_cause_sentences_are_numbered_where_one_ends_and_not_inside_times_or_versions() -> None:
    text = "Deploy 1.43.0 at 02:57:16.990Z raised TypeError. With e.g. no promo, apply_promo fails. 17% of requests failed."
    assert audit.sentences(text) == [
        "Deploy 1.43.0 at 02:57:16.990Z raised TypeError.",
        "With e.g. no promo, apply_promo fails.",
        "17% of requests failed.",
    ]


def test_a_run_has_a_target_per_item_sentence_and_inference_and_a_halted_run_none() -> None:
    name = "pool-1027-1856-r1"
    hypothesis = TRIAL_RUNS[name]["hypothesis"]
    names = audit.targets(hypothesis)
    assert {k for k in names if k[0] == "e"} == {f"e{n}" for n in range(1, len(hypothesis["evidence"]) + 1)}
    assert {k for k in names if k[0] == "i"} == {f"i{n}" for n in range(1, len(hypothesis["inferences"]) + 1)}
    assert names["s1"].startswith("A config change at 18:56:06") and "s4" in names and "s5" not in names
    assert audit.targets(HALTED) == {} and audit.targets(None) == {}


@pytest.mark.parametrize(
    ("slip", "problem"),
    [
        (_slip("e99", "contradicted"), "e99 points at nothing in this answer (e1-e"),
        (_slip("s9", "unsupported"), "s9 points at nothing"),
        (_slip("evidence[2]", "contradicted"), "isn't e<n>, s<n>, or i<n>"),
        (_slip("e1", "wrong_value"), "complaint 'wrong_value' isn't one of"),
        (_slip("e1", "contradicted", kind="bad"), "kind 'bad' isn't strict or loose"),
        (_slip("e1", "contradicted", note=" "), "no note"),
    ],
)
def test_the_audit_tool_refuses_a_label_that_points_at_nothing_or_says_nothing_it_may(slip: dict[str, str], problem: str) -> None:
    labels = _clean_audit()
    labels["runs"]["pool-1027-1856-r1"]["slips"] = [slip]
    found = audit.problems(labels, TRIAL, {n: r["hypothesis"] for n, r in TRIAL_RUNS.items()})
    assert len(found) == 1 and found[0].startswith("pool-1027-1856-r1: slip 1: ") and problem in found[0]


def test_one_label_per_target_and_complaint_so_one_flag_cant_count_twice() -> None:
    labels = _clean_audit()
    labels["runs"]["pool-1027-1856-r1"]["slips"] = [_slip("e2", "contradicted"), _slip("e2", "contradicted"), _slip("e2", "outside_read")]
    found = audit.problems(labels, TRIAL, {n: r["hypothesis"] for n, r in TRIAL_RUNS.items()})
    assert found == ["pool-1027-1856-r1: slip 2: a second contradicted label on e2: one label per target and complaint"]


def test_a_blind_audit_covers_every_scored_run_and_only_those_and_a_run_without_an_answer_is_none() -> None:
    answers: dict[str, Any] = {**{n: r["hypothesis"] for n, r in TRIAL_RUNS.items()}, "pool-1027-1856-r2": HALTED}
    labels = _clean_audit()
    labels["runs"]["pool-1027-1856-r2"] = {"diagnosis": "right", "slips": [_slip("e1", "contradicted")]}
    labels["runs"]["deploy-9999-r1"] = {"diagnosis": "right", "slips": []}
    del labels["runs"]["deploy-1027-1905-r1"]
    found = audit.problems({**labels, "sweep": "other"}, TRIAL, answers)
    assert found == [
        f"it audits sweep 'other', not {TRIAL}",
        "deploy-1027-1905-r1: not audited",
        "pool-1027-1856-r2: no answer, so its diagnosis is none",
        "pool-1027-1856-r2: slip 1: e1 points at nothing in this answer (it has none)",
        f"deploy-9999-r1: not a scored run of {TRIAL}",
    ]
    # Calibration labels cover the runs they were given, not every run.
    assert audit.problems(_clean_audit(["pool-1027-1856-r1"]), TRIAL, answers, every_run=False) == []


# --- the scorer -------------------------------------------------------------------


def test_the_sweep_loads_its_scored_runs_and_leaves_errors_out(tmp_path: Path) -> None:
    loaded = score.load_sweep(_sweep_dir(tmp_path, halted=True, error=True))
    assert len(loaded.records) == 10 and "pool-1027-1900-r2" not in loaded.answers
    assert loaded.checks["pool-1027-1856-r2"] is None and loaded.answers["pool-1027-1856-r2"]["halted_by"] == "cost_limit"
    assert loaded.flags() == FLAGS
    assert sum(len(f) for f in FLAGS.values()) == 6  # trial 3's flags, as verdicts.json pins them


def test_a_flag_catches_a_strict_label_only_on_its_own_target_saying_its_own_complaint(tmp_path: Path) -> None:
    labels = _clean_audit()
    flagged = [(run, i, audit.FLAG_COMPLAINT[next(c.verdict for c in CHECKS[run].flagged if c.id == i)]) for run, f in FLAGS.items() for i in f]
    (r1, i1, c1), (r2, i2, c2), (r3, i3, _) = flagged[:3]
    other = next(c for c in ("contradicted", "outside_read", "miscited") if c != c2)
    holds = next(c.id for c in CHECKS["pool-1027-1856-r1"].checks if not c.flagged)
    labels["runs"][r1]["slips"].append(_slip(i1, c1))  # caught
    labels["runs"][r2]["slips"].append(_slip(i2, other))  # flagged, but for something else: not caught
    labels["runs"][r3]["slips"].append(_slip(i3, "contradicted", kind="loose"))  # loose: not in recall
    labels["runs"]["pool-1027-1856-r1"]["slips"] += [_slip(holds, "contradicted"), _slip("s2", "unsupported"), _slip("i1", "unsupported")]
    report = score.score_sweep(_sweep_dir(tmp_path), _write(tmp_path, "audit.json", labels), bar_path=_bar(tmp_path, frozen=False), committed=_committed)
    whole = report.summary["all"]
    assert (whole["strict_labels"], whole["caught"], whole["caught_other_complaint"]) == (5, 1, 1)
    assert whole["prose_only"] == 2 and whole["prose_share"] == pytest.approx(2 / 5)
    assert whole["strict_recall"] == pytest.approx(1 / 5)  # prose-only labels count, as the bar says
    assert whole["checkable_recall"] == pytest.approx(1 / 3)
    assert (whole["loose_labels"], whole["loose_flagged"], whole["flags"]) == (1, 1, 6)
    assert "precision" not in whole and report.verdict.startswith("TUNING")
    text = score.render(report)
    assert "strict recall 1 of 5 (20%; bar 70%); on checkable labels 33%; 1 more flagged with another complaint" in text
    assert "prose-only 2 of 5 strict labels (40%; a judge if 20% or more)" in text
    assert f"pool-1027-1856-r1 s2 unsupported (prose-only): a note" in text

    without_prose = score.score_sweep(_sweep_dir(tmp_path / "b"), _write(tmp_path, "audit.json", labels), bar_path=_bar(tmp_path, frozen=False, recall_includes_prose_only=False), committed=_committed)
    assert without_prose.summary["all"]["strict_recall"] == pytest.approx(1 / 3)


def test_precision_is_every_flag_hand_checked_and_a_right_flag_no_label_names_is_an_audit_miss(tmp_path: Path) -> None:
    calls = _calls()
    run, item_id = next((r, i) for r, f in FLAGS.items() for i in f)
    calls["flags"][run][item_id]["call"] = "false_flag"
    labels = _clean_audit()
    labeled = next((r, i) for r, f in FLAGS.items() for i in f if (r, i) != (run, item_id))
    labels["runs"][labeled[0]]["slips"].append(_slip(labeled[1], "contradicted"))
    report = score.score_sweep(
        _sweep_dir(tmp_path), _write(tmp_path, "audit.json", labels), _write(tmp_path, "flags.json", calls), bar_path=_bar(tmp_path, frozen=False), committed=_committed
    )
    whole = report.summary["all"]
    assert (whole["flags"], whole["flags_right"], whole["precision"]) == (6, 5, pytest.approx(5 / 6))
    assert whole["audit_misses"] == 4  # right, and no label on them
    assert "flags called right that no label names (audit misses):" in score.render(report)


def test_a_flag_check_that_misses_a_flag_or_was_made_on_other_verdicts_is_refused(tmp_path: Path) -> None:
    calls = _calls()
    run, item_id = next((r, i) for r, f in FLAGS.items() for i in f)
    del calls["flags"][run][item_id]
    other_run, other_id = [(r, i) for r, f in FLAGS.items() for i in f][-1]
    calls["flags"][other_run][other_id]["verdict"] = "holds"
    calls["flags"]["deploy-1027-1905-r1"] = {"e1": {"verdict": "contradicted", "call": "right"}}
    with pytest.raises(repo.Refusal) as refused:
        score.score_sweep(
            _sweep_dir(tmp_path), _write(tmp_path, "audit.json", _clean_audit()), _write(tmp_path, "flags.json", calls), bar_path=_bar(tmp_path, frozen=False), committed=_committed
        )
    message = str(refused.value)
    assert f"{run} {item_id}: flagged" in message and "not checked" in message
    assert f"{other_run} {other_id}: checked as holds, but the checker now says" in message
    assert "deploy-1027-1905-r1 e1: checked, but not a flag" in message


def test_an_audit_that_doesnt_fit_the_sweep_is_refused_before_scoring(tmp_path: Path) -> None:
    labels = _clean_audit()
    labels["runs"]["pool-1027-1856-r1"]["slips"] = [_slip("e40", "contradicted")]
    with pytest.raises(repo.Refusal, match="the audit doesn't fit the sweep:\n  pool-1027-1856-r1: slip 1: e40 points at nothing"):
        score.score_sweep(_sweep_dir(tmp_path), _write(tmp_path, "audit.json", labels), bar_path=_bar(tmp_path, frozen=False), committed=_committed)


# --- the pass bar and the freeze ----------------------------------------------------


def test_the_committed_pass_bar_is_r7s_and_frozen() -> None:
    bar = json.loads(score.PASS_BAR.read_text(encoding="utf-8"))
    assert (bar["precision"], bar["strict_recall"], bar["judge_if_prose_share_at_least"]) == (0.85, 0.70, 0.20)
    assert bar["recall_includes_prose_only"] is True and bar["frozen"] is True
    # The user's calls (2026-11-15): the 8 paths of 2026-11-13 widened to 26, and the scenarios and windows held to each sweep's commit.
    assert len(bar["frozen_paths"]) == 26 and bar["frozen_paths"] == sorted(set(bar["frozen_paths"]))
    assert bar["fixed_from_sweep"] == ["agent/evals/scenarios.py", "tools/tests/fixtures/windows"]
    assert not {p for p in bar["frozen_paths"] for f in bar["fixed_from_sweep"] if f == p or f.startswith(p + "/") or p.startswith(f + "/")}
    tracked = set(repo.git("ls-files", "--", *bar["frozen_paths"], *bar["fixed_from_sweep"]).stdout.splitlines())
    for path in bar["frozen_paths"] + bar["fixed_from_sweep"]:
        assert any(t == path or t.startswith(path + "/") for t in tracked), f"{path} isn't tracked"


FIXED = ("agent/prompts",)


def _frozen_bar(tmp_path: Path, fixed: tuple[str, ...] | None = FIXED) -> Path:
    """The committed bar, frozen over one small path; fixed None leaves fixed_from_sweep out."""
    bar = {**json.loads(score.PASS_BAR.read_text(encoding="utf-8")), "frozen": True, "frozen_paths": ["agent/prompts"]}
    bar.pop("fixed_from_sweep", None)
    if fixed is not None:
        bar["fixed_from_sweep"] = list(fixed)
    return _write(tmp_path, "bar.json", bar)


def _frozen(
    tmp_path: Path, labels: dict[str, Any], calls: dict[str, Any], fixed: tuple[str, ...] | None = FIXED, committed: Any = _committed, **meta: Any
) -> score.Report:
    folder = _sweep_dir(tmp_path, meta={"commit": repo.head(), "dirty": [], **meta})
    return score.score_sweep(
        folder, _write(tmp_path, "audit.json", labels), _write(tmp_path, "flags.json", calls), bar_path=_frozen_bar(tmp_path, fixed), committed=committed
    )


@pytest.fixture
def held_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trial 3's alarms as test-split ones: under a frozen bar only a test-split sweep is graded."""
    monkeypatch.setattr(score, "BY_ID", {k: dataclasses.replace(s, split=Split.TEST) for k, s in BY_ID.items()})


def _all_caught() -> dict[str, Any]:
    labels = _clean_audit()
    for run, check in CHECKS.items():
        labels["runs"][run]["slips"] = [_slip(c.id, audit.FLAG_COMPLAINT[c.verdict]) for c in check.flagged]
    return labels


def test_a_frozen_bar_gives_pass_or_fail_on_the_hand_checked_flags(tmp_path: Path, held_out: None) -> None:
    passed = _frozen(tmp_path / "a", _all_caught(), _calls())
    assert passed.verdict == "PASS" and passed.summary["all"]["precision"] == 1.0 and passed.summary["all"]["strict_recall"] == 1.0

    missed = _all_caught()
    for n in range(1, 4):  # 3 strict slips no flag can catch: recall 6 of 9, under 70%
        missed["runs"]["deploy-1027-1905-r1"]["slips"].append(_slip(f"s{n}", "unsupported"))
    assert _frozen(tmp_path / "b", missed, _calls()).verdict == "FAIL"

    wrong_flags = _calls()
    for run in list(wrong_flags["flags"])[:1]:
        for entry in wrong_flags["flags"][run].values():
            entry["call"] = "false_flag"
    report = _frozen(tmp_path / "c", _all_caught(), wrong_flags)
    assert report.summary["all"]["precision"] < 0.85 and report.verdict == "FAIL"


@pytest.mark.parametrize(
    ("meta", "refusal"),
    [
        ({"commit": None}, "doesn't record the commit it ran on"),
        ({"dirty": ["agent/prompts/system.md"]}, "ran with uncommitted changes: agent/prompts/system.md"),
    ],
)
def test_a_frozen_bar_refuses_a_sweep_it_cant_hold_to_the_freeze(tmp_path: Path, held_out: None, meta: dict[str, Any], refusal: str) -> None:
    with pytest.raises(repo.Refusal, match=refusal):
        _frozen(tmp_path, _all_caught(), _calls(), **meta)


def test_a_frozen_bar_grades_only_a_test_split_sweep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(repo.Refusal, match=r"only a test-split sweep is a result: this one holds tuning scenarios \(deploy-1027-1905, "):
        _frozen(tmp_path / "a", _all_caught(), _calls())
    # A mixed sweep is refused too, and told which of its scenarios aren't the test split's.
    mixed = {k: dataclasses.replace(s, split=Split.TEST if k.startswith("deploy") else Split.TUNING) for k, s in BY_ID.items()}
    monkeypatch.setattr(score, "BY_ID", mixed)
    with pytest.raises(repo.Refusal, match=r"holds tuning scenarios \(pool-1027-1856, pool-1027-1900, pool-1027-1901\)"):
        _frozen(tmp_path / "b", _all_caught(), _calls())
    # A scenario the sweep planned counts, scored or not.
    monkeypatch.setattr(score, "BY_ID", {k: dataclasses.replace(s, split=Split.TEST) for k, s in BY_ID.items() if k != "pool-1027-1856"})
    with pytest.raises(repo.Refusal, match="names scenarios that aren't in scenarios.py: pool-1027-1856"):
        _frozen(tmp_path / "c", _all_caught(), _calls())
    # Unfrozen, the same sweep is graded, as tuning, whatever its split.
    monkeypatch.setattr(score, "BY_ID", mixed)
    report = score.score_sweep(_sweep_dir(tmp_path / "d"), _write(tmp_path, "audit.json", _clean_audit()), bar_path=_bar(tmp_path, frozen=False), committed=_committed)
    assert report.verdict.startswith("TUNING") and report.split == "mixed"


def test_a_frozen_bar_holds_the_scenarios_and_windows_to_the_sweeps_own_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held_out: None
) -> None:
    with pytest.raises(repo.Refusal, match="the frozen bar names no fixed_from_sweep paths"):
        _frozen(tmp_path / "a", _all_caught(), _calls(), fixed=None)

    # The freeze is some earlier commit, and scenarios.py has changed since the sweep ran.
    freeze, fixed = "f" * 40, ("agent/evals/scenarios.py", "tools/tests/fixtures/windows")
    asked = []

    def unchanged(since: str, paths: list[str], until: str | None = None) -> bool:
        asked.append((since, tuple(paths), until))
        return tuple(paths) != fixed

    monkeypatch.setattr(score, "unchanged", unchanged)
    monkeypatch.setattr(score, "git", lambda *args: subprocess.CompletedProcess(args, 0, "", ""))
    with pytest.raises(repo.Refusal, match="agent/evals/scenarios.py, tools/tests/fixtures/windows changed since the sweep ran on"):
        _frozen(tmp_path / "b", _all_caught(), _calls(), fixed=fixed, committed=lambda _: freeze)
    # The frozen paths are held to the freeze; these, to the sweep's own commit and the files as they are now.
    assert asked == [(freeze, ("agent/prompts",), repo.head()), (freeze, ("agent/prompts",), None), (repo.head(), fixed, None)]

    # Unchanged since the sweep ran, it's graded: rows and windows added before it, after the freeze, are fine.
    monkeypatch.setattr(score, "unchanged", lambda since, paths, until=None: True)
    assert _frozen(tmp_path / "c", _all_caught(), _calls(), fixed=fixed, committed=lambda _: freeze).verdict == "PASS"


def test_a_test_sweep_can_start_only_where_it_could_be_graded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tmp_path.mkdir(exist_ok=True)
    with pytest.raises(repo.Refusal, match="the test split runs only after the freeze, and the pass bar isn't frozen"):
        score.check_before_test_sweep(_bar(tmp_path, frozen=False), committed=_committed)
    frozen = _frozen_bar(tmp_path)
    monkeypatch.setattr(score, "dirty", lambda: ["agent/runner.py"])
    with pytest.raises(repo.Refusal, match="uncommitted changes: agent/runner.py"):
        score.check_before_test_sweep(frozen, committed=_committed)
    monkeypatch.setattr(score, "dirty", lambda: [])
    score.check_before_test_sweep(frozen, committed=_committed)  # frozen, clean, on the freeze's own commit
    with pytest.raises(repo.Refusal, match="names no fixed_from_sweep paths"):
        score.check_before_test_sweep(_frozen_bar(tmp_path, fixed=None), committed=_committed)


# --- the report's groups, the count after the ask, and its notes ----------------------------


def test_the_report_groups_runs_by_fault_and_counts_the_tool_calls_after_the_ask(tmp_path: Path) -> None:
    after = {
        "pool-1027-1856-r1": [],  # asked, and answered
        "pool-1027-1900-r1": ["get_metrics", "get_recent_logs"],  # asked, and went on investigating
        "deploy-1113-0257-r1": None,  # answered unasked
        "deploy-1027-1905-r1": [],
    }
    report = score.score_sweep(
        _sweep_dir(tmp_path, after_ask=after), _write(tmp_path, "audit.json", _clean_audit()), bar_path=_bar(tmp_path, frozen=False), committed=_committed
    )
    summary = report.summary
    assert list(summary) == ["all", "unseen", "seen", "bad_deploy", "db_pool_exhaustion"]
    assert (summary["bad_deploy"]["runs"], summary["db_pool_exhaustion"]["runs"]) == (6, 3)
    assert summary["bad_deploy"]["flags"] + summary["db_pool_exhaustion"]["flags"] == summary["all"]["flags"] == 6
    whole, pool = summary["all"], summary["db_pool_exhaustion"]
    assert (whole["asked_recorded"], whole["asked"], whole["calls_after_ask"], whole["runs_with_calls_after_ask"]) == (4, 3, 2, 1)
    assert (pool["asked_recorded"], pool["asked"], pool["calls_after_ask"], pool["runs_with_calls_after_ask"]) == (2, 2, 2, 1)
    text = score.render(report)
    assert f"sweep {TRIAL}, arm haiku (claude-haiku-5-5), the tuning split; audit " in text
    assert "\nbad_deploy: 6 runs (6 answered)" in text and "\ndb_pool_exhaustion: 3 runs (3 answered)" in text
    assert "\nunseen: 6 runs" in text and "\nseen: 3 runs" in text
    assert "  asked for the answer in 3 of 4 runs (5 not recorded); tool calls after the ask: 2, in 1 run(s)" in text
    assert "  asked for the answer in 2 of 2 runs (1 not recorded); tool calls after the ask: 2, in 1 run(s)" in text  # the pool's
    assert "notes:" not in text

    # Transcripts from before the count don't read as "never asked".
    old = score.score_sweep(_sweep_dir(tmp_path / "b"), _write(tmp_path, "audit.json", _clean_audit()), bar_path=_bar(tmp_path, frozen=False), committed=_committed)
    assert old.summary["all"]["asked_recorded"] == 0
    assert "  asked for the answer: not recorded (transcripts from before the count)" in score.render(old)


def test_the_report_says_where_the_rubric_wasnt_blind_to_a_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    window = "2026-11-14-2357"  # downstream_timeout's held-out window
    assert (repo.REPO / "tools" / "tests" / "fixtures" / "windows" / window).is_dir()
    note = WINDOW_NOTES[window]
    assert "04d0479" in note and "22:28 bad_deploy break" in note and note.isascii()
    moved = {k: dataclasses.replace(s, seen=False, window=window if k == "deploy-1113-0257" else s.window) for k, s in BY_ID.items()}
    monkeypatch.setattr(score, "BY_ID", moved)
    report = score.score_sweep(_sweep_dir(tmp_path), _write(tmp_path, "audit.json", _clean_audit()), bar_path=_bar(tmp_path, frozen=False), committed=_committed)
    text = score.render(report)
    assert f"\n\nnotes:\n  window {window}: {note}" in text and text.count("04d0479") == 1
    # With every run unseen, that group would only repeat the whole: it's left out.
    assert "\nunseen: " not in text and "\nseen: " not in text and text.count("\nall: 9 runs") == 1


def _has_commit(commit: str) -> bool:
    return repo.git("cat-file", "-e", f"{commit}^{{commit}}").returncode == 0


@pytest.mark.skipif(not _has_commit("57455ab"), reason="a shallow clone lacks the older commit")
def test_a_frozen_bar_refuses_a_sweep_that_ran_before_the_freeze(tmp_path: Path, held_out: None) -> None:
    with pytest.raises(repo.Refusal, match="before the freeze"):
        _frozen(tmp_path, _all_caught(), _calls(), commit=subprocess.run(["git", "rev-parse", "57455ab"], cwd=repo.REPO, capture_output=True, text=True).stdout.strip())


def test_grade_refuses_files_that_arent_committed(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, held_out: None
) -> None:
    # The committed bar is frozen: the sweep is a test-split one, and the freeze's own checks
    # (tested above) are passed over, so the refusal is the audit's.
    monkeypatch.setattr(score, "check_freeze", lambda *args: None)
    folder = _sweep_dir(tmp_path)
    audit = _write(tmp_path, "audit.json", _clean_audit())
    assert evals_main(["grade", str(folder), "--audit", str(audit)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Refused: ") and "audit.json isn't in the repo, so it isn't committed" in err


# --- the labeling packet and the comparison (R8) -------------------------------------


def test_the_packet_holds_what_the_run_saw_and_answered_and_nothing_else() -> None:
    name = "pool-1027-1856-r1"
    run = copy.deepcopy(TRIAL_RUNS[name])
    run["messages"][0]["content"][:0] = [
        {"reasoningContent": {"reasoningText": {"text": "SECRET-THINKING the pool looks small"}}},
        {"text": "MODEL-NARRATION let me read the metrics"},
    ]
    run["messages"].insert(0, {"role": "user", "content": [{"text": "An alarm fired on checkout-api. THE-PROMPT"}]})
    text = packet.render(TRIAL, {name: run}, audit.GUIDE)
    assert "SECRET-THINKING" not in text and "MODEL-NARRATION" not in text
    assert "````text\nAn alarm fired on checkout-api. THE-PROMPT\n````" in text
    calls = packet.tool_calls(run)
    assert len(calls) >= 3 and all(output in text for _, _, output in calls)
    for target, said in audit.targets(run["hypothesis"]).items():
        if target[0] == "e":
            assert f"[{target}] `{json.dumps(packet.shown(json.loads(said)), ensure_ascii=False)}`" in text
        else:
            assert f"[{target}] {said}" in text
    for check in CHECKS[name].checks:  # nothing the checker says
        assert check.reason not in text


def test_the_packet_command_writes_blank_labels_the_audit_tool_wont_take_until_filled(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    folder = _sweep_dir(tmp_path)
    out = tmp_path / "calibration.md"
    runs = ["pool-1027-1856-r1", "deploy-1113-0257-r1", "pool-1027-1901-r1"]
    assert evals_main(["packet", str(folder), "--out", str(out), *[a for r in runs for a in ("--run", r)]]) == 0
    blank = json.loads(out.with_suffix(".labels.json").read_text(encoding="utf-8"))
    assert list(blank["runs"]) == runs and blank["sweep"] == TRIAL and all(v["slips"] == [] for v in blank["runs"].values())
    assert out.read_text(encoding="utf-8").count("\n## ") == 3
    capsys.readouterr()
    assert evals_main(["audit", str(folder), "--labels", str(out.with_suffix(".labels.json")), "--some"]) == 1
    assert "3 problem(s)" in capsys.readouterr().out
    for entry in blank["runs"].values():
        entry["diagnosis"] = "right"
    filled = _write(tmp_path, "filled.json", blank)
    assert evals_main(["audit", str(folder), "--labels", str(filled), "--some"]) == 0
    assert evals_main(["audit", str(folder), "--labels", str(filled)]) == 1  # a blind audit covers every run


def test_two_labelers_are_compared_by_target_and_complaint() -> None:
    mine, theirs = _clean_audit(), _clean_audit(["pool-1027-1856-r1", "deploy-1113-0257-r1"])
    mine["runs"]["pool-1027-1856-r1"]["slips"] = [_slip("e2", "contradicted"), _slip("s2", "unsupported", kind="loose"), _slip("e5", "outside_read")]
    theirs["runs"]["pool-1027-1856-r1"]["slips"] = [_slip("e2", "contradicted"), _slip("s2", "unsupported"), _slip("e7", "miscited")]
    theirs["runs"]["deploy-1113-0257-r1"]["diagnosis"] = "partly"
    mine["runs"]["deploy-1027-1905-r1"]["slips"] = [_slip("e1", "contradicted")]  # a run they didn't label
    c = audit.compare(mine, theirs)
    assert c.runs == ["pool-1027-1856-r1", "deploy-1113-0257-r1"] and c.diagnoses_agree == 1
    assert [(a.target, b.target) for a, b in c.agree] == [("e2", "e2")]
    assert [(a.kind, b.kind) for a, b in c.other_kind] == [("loose", "strict")]
    assert [lb.target for lb in c.only_mine] == ["e5"] and [lb.target for lb in c.only_theirs] == ["e7"]
    text = audit.render_comparison(c)
    assert "deploy-1113-0257-r1: diagnosis mine right, theirs partly  <- differs" in text
    assert "slips: 1 of 4 agree on target, complaint, and kind; 1 differ only in kind" in text


def test_compare_refuses_label_files_that_arent_committed(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    folder = _sweep_dir(tmp_path)
    mine, theirs = _write(tmp_path, "mine.json", _clean_audit()), _write(tmp_path, "theirs.json", _clean_audit(["pool-1027-1856-r1"]))
    assert evals_main(["compare", str(folder), "--audit", str(mine), "--labels", str(theirs)]) == 1
    assert "isn't in the repo, so it isn't committed" in capsys.readouterr().err


# --- the arms (R10) -------------------------------------------------------------------


def test_each_arm_has_its_own_model_and_caps_and_haikus_are_productions() -> None:
    haiku, sonnet = ARMS["haiku"], ARMS["sonnet"]
    assert (haiku.model_id, haiku.per_run_usd, haiku.cap_usd, haiku.blocked) == ("claude-haiku-5-5", Limits().max_cost_usd, 1.00, None)
    assert (sonnet.model_id, sonnet.per_run_usd, sonnet.cap_usd, sonnet.blocked) == ("claude-sonnet-5-5", 0.50, 6.00, None)
    # The user's rule (2026-11-16): the check run projected the 33-run sweep at $7.74, past $4.50, so 2.
    assert (haiku.runs_per_scenario, sonnet.runs_per_scenario) == (3, 2)
    assert Limits().max_cost_usd == 0.10  # production's cap is untouched
    settings = sonnet.settings(Settings.from_env(FEDERATION_IDS))
    assert settings.model_id == "claude-sonnet-5-5" and settings.max_output_tokens == 16_000


def test_each_arm_plans_its_own_runs_per_alarm_unless_told(capsys: pytest.CaptureFixture[str]) -> None:
    # Never --go here: the sonnet arm is unblocked, so a --go would sign in and spend.
    assert price_for("anthropic", "claude-sonnet-5-5").standard.output == 10.00
    assert evals_main(["sweep", "--arm", "sonnet"]) == 2
    out = capsys.readouterr().out
    assert "40 runs (20 scenarios x 2) of the tuning split, one at a time, on the sonnet arm (claude-sonnet-5-5)" in out
    assert "Expected about $9.40; at most $6.00 ($0.50 a run, $6.00 a sweep)" in out
    assert "can't run yet" not in out and "Nothing ran" in out
    assert evals_main(["plan", "--arm", "sonnet", "--runs", "1"]) == 0
    assert "20 runs (20 scenarios x 1)" in capsys.readouterr().out
    assert evals_main(["plan"]) == 0
    assert "60 runs (20 scenarios x 3) of the tuning split, one at a time, on the haiku arm" in capsys.readouterr().out


def test_a_blocked_arm_still_refuses_to_spend(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    def started(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a sweep started")

    monkeypatch.setattr(evals_cli, "run_sweep", started)
    monkeypatch.setitem(ARMS, "sonnet", dataclasses.replace(ARMS["sonnet"], blocked="a test's reason"))
    assert evals_main(["sweep", "--go", "--arm", "sonnet", "--runs", "1"]) == 1
    captured = capsys.readouterr()
    assert "The sonnet arm can't run yet: a test's reason." in captured.out
    assert "Refused: the sonnet arm is blocked." in captured.err


def _scripted(n: int) -> FakeAnthropic:
    replies = []
    for _ in range(n):
        replies += [Reply([tool_use("get_recent_logs")]), answer(json.loads(json.dumps(TRIAL_RUNS["deploy-1113-0257-r1"]["hypothesis"])))]
    return FakeAnthropic(replies)


def _run(arm: Arm, out_dir: Path, fake: FakeAnthropic, **kwargs: Any) -> list[sweep.RunRecord]:
    session = FakeSession()
    session.expect_web_identity_token()
    return asyncio.run(
        sweep.run_sweep(
            [BY_ID["deploy-1113-0257"]], 1, settings=arm.settings(Settings.from_env(FEDERATION_IDS)), price=price_for("anthropic", arm.model_id),
            sign_in=session, out_dir=out_dir, api_transport=lambda: httpx.MockTransport(fake._handle),
            token_http_client=fake.token_client(), arm=arm, pause_s=0, say=lambda _: None, **kwargs,
        )
    )


def test_an_arms_cap_on_a_run_reaches_the_guard_and_the_sweep_records_the_arm(tmp_path: Path) -> None:
    tiny = Arm("tiny", "claude-haiku-5-5", per_run_usd=0.001, cap_usd=1.00, expected_run_usd=0.0, runs_per_scenario=1)
    records = _run(tiny, tmp_path, _scripted(1))
    assert [(r.status, r.detail) for r in records] == [("halted", "cost_limit")]
    saved = json.loads((tmp_path / "sweep.json").read_text(encoding="utf-8"))
    assert (saved["arm"], saved["model"], saved["per_run_usd"], saved["cap_usd"]) == ("tiny", "claude-haiku-5-5", 0.001, 1.00)
    assert saved["commit"] == repo.head() and isinstance(saved["dirty"], list)


def test_a_resumed_sweep_must_be_the_same_arms(tmp_path: Path) -> None:
    (tmp_path / "sweep.json").write_text(json.dumps({"arm": "sonnet", "records": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="is the sonnet arm's sweep, not haiku's"):
        _run(ARMS["haiku"], tmp_path, FakeAnthropic([]))
