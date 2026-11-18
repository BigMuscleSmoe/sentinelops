"""The CI gate (Phase 3's step 2, closed as scoped): the committed baseline in evals/gate/, held in CI with no model.

What the model is sent must be as the baseline's sweep was sent; every
committed answer must score on the rubric as pinned, every diagnosis right; and
the checker's verdicts on those answers are pinned, the flags a floor under the
typed slips (evals/baseline.py says why a floor, not a count).

When the first test fails on purpose (a prompt, schema, tool, or model change):
run a sweep on the user's go, grade it, and rebuild the baseline from it
(`uv run python -m evals.baseline build ...`) in the same commit as the change.
"""

import json
from typing import Any

import pytest

import runner
from checker.__main__ import check_run
from evals import audit as audits
from evals import baseline as gate
from evals.arms import ARMS
from evals.repo import REPO

BASELINE, REQUEST, RUNS = gate.load()
ARM = ARMS[BASELINE["arm"]]


@pytest.fixture(scope="module")
def sent_now() -> dict[str, Any]:
    return gate.capture(ARM)


@pytest.fixture(scope="module")
def checked() -> dict[str, Any]:
    return {name: check_run(name, run) for name, run in RUNS.items()}


def test_what_the_model_is_sent_is_as_the_baseline_was_sent(sent_now: dict[str, Any]) -> None:
    changed = gate.changed_parts(sent_now, REQUEST)
    assert not changed, (
        f"changed since sweep {BASELINE['sweep']}'s baseline: {', '.join(changed)}. "
        "A change to what the model is sent needs a new sweep (on the user's go) and its baseline "
        "(python -m evals.baseline build) committed with it."
    )


def test_the_baseline_holds_the_sweeps_answered_runs_and_nothing_else() -> None:
    assert set(RUNS) == set(BASELINE["runs"])
    assert all(RUNS[name]["eval"]["run"] == name for name in RUNS)
    floor = BASELINE["floor"]
    assert floor["runs"] == len(RUNS) and floor["items"] == sum(len(p["verdicts"]) for p in BASELINE["runs"].values())


def test_every_committed_answer_scores_on_the_rubric_as_pinned(checked: dict[str, Any]) -> None:
    got = {name: gate.rubric_items(run, checked[name]) for name, run in RUNS.items()}
    assert got == {name: pin["rubric"] for name, pin in BASELINE["runs"].items()}


def test_every_committed_diagnosis_is_right() -> None:
    wrong = [name for name, pin in BASELINE["runs"].items() if not gate.diagnosis(pin["rubric"])]
    assert wrong == []


def test_the_rubric_agrees_with_the_committed_hand_audit() -> None:
    path = REPO / "agent" / "evals" / "audits" / f"{BASELINE['sweep']}.json"
    if not path.exists():
        pytest.skip("this baseline's sweep has no committed audit")
    audited = audits.read(path)["runs"]
    assert {name: ("right" if gate.diagnosis(pin["rubric"]) else "wrong") for name, pin in BASELINE["runs"].items()} == {
        name: audited[name]["diagnosis"] for name in BASELINE["runs"]
    }


def test_the_checkers_verdicts_on_the_committed_answers_are_pinned(checked: dict[str, Any]) -> None:
    got = {name: gate.verdicts(check) for name, check in checked.items()}
    assert got == {name: pin["verdicts"] for name, pin in BASELINE["runs"].items()}


def test_every_flag_in_the_floor_carries_its_hand_call() -> None:
    if BASELINE["flags_checked"] is None:
        pytest.skip("this baseline's flags weren't hand-checked")
    calls = audits.read(REPO / BASELINE["flags_checked"])
    flagged = {
        name: {i: v for i, v in pin["verdicts"].items() if v in gate.FLAGGED}
        for name, pin in BASELINE["runs"].items()
    }
    flagged = {name: by_id for name, by_id in flagged.items() if by_id}
    assert audits.flag_problems(calls, BASELINE["sweep"], flagged) == []
    for name, by_id in flagged.items():
        pinned = BASELINE["runs"][name]["calls"]
        assert set(pinned) == set(by_id)
        for item_id, call in pinned.items():
            entry = calls["flags"][name][item_id]
            assert call == {"call": entry["call"], "form_only": entry["note"].startswith("form:")}


def test_the_floor_counts_as_the_report_gives_them() -> None:
    # agent/evals/reports/2026-11-18-test-sweeps.md: 37 flags on 455 items, 35 right (95%),
    # 29 with the 6 form-only flags counted false (78%).
    calls = [c for pin in BASELINE["runs"].values() for c in (pin.get("calls") or {}).values()]
    right = sum(c["call"] == "right" for c in calls)
    form = sum(c["call"] == "right" and c["form_only"] for c in calls)
    floor = BASELINE["floor"]
    assert (floor["flags"], floor["right"], floor["form_only_right"], floor["right_with_form_only_false"]) == (
        len(calls),
        right,
        form,
        right - form,
    )
    if BASELINE["sweep"] == "20261117T192758Z":
        assert (floor["runs"], floor["items"], floor["flags"], floor["right"], floor["form_only_right"]) == (33, 455, 37, 35, 6)


# --- the gate itself: it sees each part change, and keeps nothing it shouldn't -----


def test_a_prompt_change_moves_the_prompt_alone(monkeypatch: pytest.MonkeyPatch, sent_now: dict[str, Any]) -> None:
    monkeypatch.setattr(runner, "system_prompt", lambda: "A different prompt.")
    assert gate.changed_parts(gate.capture(ARM), sent_now) == ["prompt"]


def test_a_model_setting_change_moves_the_settings_alone(monkeypatch: pytest.MonkeyPatch, sent_now: dict[str, Any]) -> None:
    import providers

    monkeypatch.setattr(providers, "THINKING", {"type": "disabled"})
    assert gate.changed_parts(gate.capture(ARM), sent_now) == ["model_settings"]


def test_a_schema_change_moves_the_schema_alone(monkeypatch: pytest.MonkeyPatch, sent_now: dict[str, Any]) -> None:
    from pydantic import Field

    import models

    # Named as the answer tool is named, with one field's description changed.
    class Hypothesis(models.Hypothesis):
        affected_component: models.Text = Field(min_length=1, description="A different description.")

    monkeypatch.setattr(runner, "Hypothesis", Hypothesis)
    assert gate.changed_parts(gate.capture(ARM), sent_now) == ["schema"]


def test_a_tool_spec_change_moves_the_tool_specs_alone(monkeypatch: pytest.MonkeyPatch, sent_now: dict[str, Any]) -> None:
    import functools

    from sentinelops_tools import toolset

    real = toolset.make_tools

    def changed(env: Any) -> list[Any]:
        tools = real(env)
        first = tools[0]

        @functools.wraps(first)
        def reworded(*args: Any, **kwargs: Any) -> Any:
            return first(*args, **kwargs)

        reworded.__doc__ = "A different summary. " + (first.__doc__ or "")
        return [reworded, *tools[1:]]

    monkeypatch.setattr(toolset, "make_tools", changed)
    assert gate.changed_parts(gate.capture(ARM), sent_now) == ["tool_specs"]


def test_each_part_is_compared_on_its_own() -> None:
    for part in gate.PARTS:
        moved = json.loads(json.dumps(REQUEST))
        moved[part] = {"moved": True}
        assert gate.changed_parts(moved, REQUEST) == [part]


def test_a_kept_run_has_no_thinking_model_text_tool_inputs_or_costs() -> None:
    transcript = {
        "alarm": {"name": "a", "fired_at": "2026-11-16T19:17:46.805000+00:00", "reason": "r", "condition": "c", "replay": True},
        "eval": {"run": "x-r1", "scenario": "x", "window": "w", "fault": "bad_deploy", "split": "test"},
        "hypothesis": {"root_cause": "rc"},
        "trims": [],
        "cost_usd": 0.01,
        "messages": [
            {"role": "user", "content": [{"text": "An alarm fired."}]},
            {
                "role": "assistant",
                "content": [
                    {"reasoningContent": {"reasoningText": {"text": "thinking"}}},
                    {"text": "model text"},
                    {"toolUse": {"toolUseId": "t1", "name": "get_metrics", "input": {"minutes": 15}}},
                ],
            },
            {"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "status": "success", "content": [{"text": "[source m1]\nrows"}]}}]},
            {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t2", "name": "Hypothesis", "input": {"root_cause": "rc"}}}]},
            {"role": "user", "content": [{"toolResult": {"toolUseId": "t2", "status": "success", "content": [{"text": "ok"}]}}]},
        ],
    }
    kept = gate.strip(transcript)
    assert kept["messages"] == [
        {"role": "assistant", "content": [{"toolUse": {"name": "get_metrics", "toolUseId": "t1"}}]},
        {"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "content": [{"text": "[source m1]\nrows"}]}}]},
    ]
    assert set(kept) == {"alarm", "eval", "hypothesis", "trims", "messages"}
    assert "replay" not in kept["alarm"] and "split" not in kept["eval"]


def test_the_kept_runs_hold_no_thinking_or_model_text() -> None:
    for run in RUNS.values():
        for message in run["messages"]:
            for block in message["content"]:
                assert set(block) <= {"toolUse", "toolResult"}
                if "toolUse" in block:
                    assert set(block["toolUse"]) == {"name", "toolUseId"}
