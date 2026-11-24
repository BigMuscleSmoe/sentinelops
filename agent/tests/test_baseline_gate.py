"""The CI gate (Phase 3's step 2, closed as scoped): the committed baseline in evals/gate/, held in CI with no model.

What the model is sent must be as the baseline's sweep was sent; every
committed answer must score on the rubric as pinned, every diagnosis right; and
the checker's verdicts on those answers are pinned, the flags a floor under the
typed slips (evals/baseline.py says why a floor, not a count).

When the first test fails on purpose (a prompt, schema, tool, or model change),
the baseline can't land in the same commit: `build` refuses a sweep run with
uncommitted changes. So: commit the change (that commit fails here alone); run
a sweep on that commit, on the user's go (a tuning sweep, so the test alarms
aren't re-run); build the baseline from it (`uv run python -m evals.baseline
build ...`) and commit that; push both commits together, since CI runs on the
pushed head only.
"""

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest

import runner
from checker.__main__ import check_run
from evals import audit as audits
from evals import baseline as gate
from evals.arms import ARMS
from evals.repo import REPO, Refusal
from evals.scenarios import BY_ID
from evals.score import PASS_BAR
from sentinelops_core.releases import RELEASES
from sources import PREFIXES

AGENT = REPO / "agent"
CORE = REPO / "core"  # the shared package, sentinelops_core
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
        "A change to what the model is sent needs a new sweep's baseline: commit the change, sweep on "
        "that commit (on the user's go), build and commit the baseline, push both (this file's docstring)."
    )


# What would point the model at an answer: this service's releases and their commits, deploy
# history's made-up commits, each fault's file, function, error, and config key, and the simulator.
HINTS = (
    *RELEASES,
    *(release.commit[:7] for release in RELEASES.values()),
    "3f8a2d1",
    "9c41e7b",
    "pricing",
    "promo",
    "subtotal",
    "TypeError",
    "NoneType",
    "UpstreamTimeout",
    "POOL_MAX_SIZE",
    "POOL_ACQUIRE_TIMEOUT",
    "PAYMENTS_TIMEOUT",
    "20->2",
    "simulat",
    "sim/",
)


def test_nothing_the_model_is_sent_names_a_release_or_the_fault(sent_now: dict[str, Any]) -> None:
    """The answer stays hidden from the agent: the prompt, the tool specs, the answer's schema, and
    the settings name none of it, so whatever a run finds, it found in what the tools showed."""
    text = json.dumps(sent_now).lower()
    assert [hint for hint in HINTS if hint.lower() in text] == []


# A result's label as the run gives it (sources.py): a tool's letter and a number, m1 or c2.
READ_LABEL = re.compile(r"(?<![A-Za-z0-9_])[" + "".join(sorted(set(PREFIXES.values()))) + r"][1-9][0-9]*(?![A-Za-z0-9_])")


def test_no_tool_spec_or_answer_field_gives_a_label_a_read_could_have(sent_now: dict[str, Any]) -> None:
    """An example label beside a field is one the model can copy into it, and every label is some
    run's real read: Remediation.source's (c2) was the replaced release's read in every bad_deploy run
    of sweep 20261124T072730Z, and 2 of 9 patches cited it. The prompt keeps its labels: it defines
    them and shows whole example items, apart from any field."""
    found = {spec["name"]: sorted(set(READ_LABEL.findall(json.dumps(spec)))) for spec in [*sent_now["tool_specs"], sent_now["schema"]]}
    assert {name: labels for name, labels in found.items() if labels} == {}


# checkout-api's real dependencies: payments-api (checkout/payments.py's DEPENDENCY) and its
# database (checkout/db.py).
DEPENDENCY = re.compile(r"payments|database|(?<![A-Za-z0-9_])db(?![A-Za-z0-9_])", re.IGNORECASE)
GET_METRICS_LINE = "- `get_metrics`: "


def test_no_example_names_a_dependency_the_service_really_calls(sent_now: dict[str, Any]) -> None:
    """A real dependency is named only where get_metrics' columns are described: its spec, and its
    line in the prompt's tool list. Anywhere else it's an example, and one naming payments-api (its
    latency ruled out, a TLS failure on its endpoint, an upstream "such as payments-api") points at
    downstream_timeout's culprit, the one scenario where it's the cause."""
    (system,) = sent_now["prompt"]["system"]
    found = [line[:80] for line in system["text"].splitlines() if DEPENDENCY.search(line) and not line.startswith(GET_METRICS_LINE)]
    for spec in [*sent_now["tool_specs"], sent_now["schema"]]:
        rest = {k: v for k, v in spec.items() if not (spec["name"] == "get_metrics" and k == "description")}
        found += [f"{spec['name']}: {m.group(0)}" for m in DEPENDENCY.finditer(json.dumps(rest))]
    found += [f"model_settings: {m.group(0)}" for m in DEPENDENCY.finditer(json.dumps(sent_now["model_settings"]))]
    assert found == []
    # Where they may be named, they are: the check would pass on a prompt that named nothing.
    assert any(line.startswith(GET_METRICS_LINE) and DEPENDENCY.search(line) for line in system["text"].splitlines())


def test_the_dependency_pattern_is_the_services_own() -> None:
    payments = (REPO / "services" / "checkout-api" / "checkout" / "payments.py").read_text(encoding="utf-8")
    assert 'DEPENDENCY = "payments-api"' in payments
    assert (REPO / "services" / "checkout-api" / "checkout" / "db.py").exists()
    assert [m.group(0) for m in DEPENDENCY.finditer("payments-api, payments_p99_ms, the database, a db, db.py")] == [
        "payments", "payments", "database", "db", "db"
    ]
    assert DEPENDENCY.findall("db_query_ms, dbx, fraud-check, loyalty-api, pool_wait_p99_ms") == []


def test_the_label_pattern_finds_labels_and_nothing_else() -> None:
    assert READ_LABEL.findall("by its label (c2), or m1, l12 and d3; r1.") == ["c2", "m1", "l12", "d3", "r1"]
    assert READ_LABEL.findall("e1 rests on it; lines [1, 30]; m0, c02, mc1, x1, 2.7.0, at 18:59:00") == []


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


def assert_floor_counts(baseline: dict[str, Any]) -> None:
    """The floor's counts from the pins; the hand calls' counts only on a baseline built with --flags."""
    pins = baseline["runs"].values()
    floor = baseline["floor"]
    flagged = sum(v in gate.FLAGGED for pin in pins for v in pin["verdicts"].values())
    assert (floor["runs"], floor["items"], floor["flags"]) == (
        len(baseline["runs"]),
        sum(len(pin["verdicts"]) for pin in pins),
        flagged,
    )
    if baseline["flags_checked"] is None:
        # A tuning sweep's flags may go unchecked: the verdict pins still catch any checker change.
        assert set(floor) == {"runs", "items", "flags"}
        assert all("calls" not in pin for pin in pins)
        return
    calls = [c for pin in pins for c in (pin.get("calls") or {}).values()]
    right = sum(c["call"] == "right" for c in calls)
    form = sum(c["call"] == "right" and c["form_only"] for c in calls)
    assert len(calls) == flagged
    assert (floor["right"], floor["form_only_right"], floor["right_with_form_only_false"]) == (right, form, right - form)


def test_the_floor_counts_as_the_report_gives_them() -> None:
    assert_floor_counts(BASELINE)
    if BASELINE["sweep"] == "20261117T192758Z":
        # agent/evals/reports/2026-11-18-test-sweeps.md: 37 flags on 455 items, 35 right (95%),
        # 29 with the 6 form-only flags counted false (78%).
        assert (BASELINE["floor"]["runs"], BASELINE["floor"]["items"]) == (33, 455)
        assert (BASELINE["floor"]["flags"], BASELINE["floor"]["right"], BASELINE["floor"]["form_only_right"]) == (37, 35, 6)


# --- build: it refuses a sweep that isn't whole (fake sweeps from the committed runs) ----


def answered_records() -> list[dict[str, Any]]:
    return [
        {"run": name, "scenario": run["eval"]["scenario"], "attempt": 1, "status": "answered", "transcript": f"{name}.json"}
        for name, run in sorted(RUNS.items())
    ]


def write_sweep(folder: Path, records: list[dict[str, Any]], *, skip_transcripts: tuple[str, ...] = ()) -> Path:
    """A sweep folder as evals sweep writes one, its transcripts the committed kept runs.

    Its split is the one the runs' scenarios are in now: the test sweep's alarms are tuning ones since 2026-11-23.
    """
    folder.mkdir(parents=True)
    for record in records:
        if record["transcript"] not in skip_transcripts:
            (folder / record["transcript"]).write_text(json.dumps(RUNS[record["run"]]), encoding="utf-8")
    meta = {
        "sweep": BASELINE["sweep"],
        "arm": BASELINE["arm"],
        "split": BY_ID[next(iter(RUNS.values()))["eval"]["scenario"]].split.value,
        "commit": BASELINE["commit"],
        "dirty": [],
        "records": records,
    }
    (folder / "sweep.json").write_text(json.dumps(meta), encoding="utf-8")
    return folder


@pytest.fixture
def fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    # Not what these tests are about, and CI's shallow clone lacks the sweep's commit.
    monkeypatch.setattr(gate, "unchanged", lambda commit, paths: True)


@pytest.mark.usefixtures("fresh")
def test_a_whole_sweep_builds_and_a_run_retried_to_an_answer_counts_by_its_last_attempt(tmp_path: Path) -> None:
    records = answered_records()
    retried = records[0]["run"]
    records.insert(0, {**records[0], "attempt": 1, "status": "error", "transcript": f"{retried}.json"})
    records[1] = {**records[1], "attempt": 2, "transcript": f"{retried}.attempt2.json"}
    built = gate.build(write_sweep(tmp_path / "sweep", records), None, tmp_path / "gate")
    assert {name: {k: v for k, v in pin.items() if k != "calls"} for name, pin in BASELINE["runs"].items()} == built["runs"]
    assert built["flags_checked"] is None
    assert_floor_counts(built)


@pytest.mark.usefixtures("fresh")
@pytest.mark.parametrize("status", ["halted", "error", "model_refused", "no_hypothesis"])
def test_a_sweep_with_a_run_that_didnt_answer_is_refused(tmp_path: Path, status: str) -> None:
    records = answered_records()
    records[3]["status"] = status
    with pytest.raises(Refusal, match=rf"not answered: {records[3]['run']} \({status}\)"):
        gate.build(write_sweep(tmp_path / "sweep", records), None, tmp_path / "gate")
    assert not (tmp_path / "gate").exists()


@pytest.mark.usefixtures("fresh")
def test_a_sweep_whose_retry_still_errored_is_refused(tmp_path: Path) -> None:
    records = answered_records()
    run = records[0]["run"]
    records.append({**records[0], "attempt": 2, "status": "error", "transcript": f"{run}.attempt2.json"})
    with pytest.raises(Refusal, match=rf"not answered: {run} \(error\)"):
        gate.build(write_sweep(tmp_path / "sweep", records), None, tmp_path / "gate")


@pytest.mark.usefixtures("fresh")
def test_an_answered_run_with_no_transcript_is_refused(tmp_path: Path) -> None:
    records = answered_records()
    lost = records[5]
    with pytest.raises(Refusal, match=rf"no transcript or no Hypothesis to check: {lost['run']}"):
        gate.build(write_sweep(tmp_path / "sweep", records, skip_transcripts=(lost["transcript"],)), None, tmp_path / "gate")


@pytest.mark.usefixtures("fresh")
def test_a_sweep_missing_one_of_its_splits_scenarios_is_refused(tmp_path: Path) -> None:
    records = answered_records()
    dropped = records[0]["scenario"]
    kept = [r for r in records if r["scenario"] != dropped]
    with pytest.raises(Refusal, match=rf"every {BY_ID[dropped].split.value} scenario; the sweep has no run of {dropped}$"):
        gate.build(write_sweep(tmp_path / "sweep", kept), None, tmp_path / "gate")


@pytest.mark.usefixtures("fresh")
def test_build_scores_with_the_current_rubric_and_refuses_a_wrong_diagnosis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    records = answered_records()
    wrong = records[2]["run"]
    cause = RUNS[wrong]["hypothesis"]["root_cause"]
    score = gate.rubric_score

    def stricter(scenario: Any, hypothesis: dict[str, Any], checks: Any) -> Any:
        result = score(scenario, hypothesis, checks)
        if hypothesis["root_cause"] == cause:
            result.items["blames_no_other"] = False
        return result

    monkeypatch.setattr(gate, "rubric_score", stricter)
    with pytest.raises(Refusal, match=rf"right diagnoses only; the rubric fails {wrong}$"):
        gate.build(write_sweep(tmp_path / "sweep", records), None, tmp_path / "gate")


# --- what shapes a run: a sweep is a baseline only while that is as it ran on ---


def changed_since_the_sweep(*files: str) -> Any:
    """git's answer, if these files alone changed since the sweep's commit: paths are unchanged unless one holds one."""

    def unchanged(commit: str, paths: list[str]) -> bool:
        assert commit == BASELINE["commit"]
        return not any(f == p or f.startswith(p + "/") for f in files for p in paths)

    return unchanged


@pytest.mark.parametrize(
    "changed",
    ["agent/evals/rubrics.py", "agent/checker/checks.py", "agent/checker/resolve.py", "agent/evals/score.py", "agent/evals/audit.py"],
)
def test_a_scoring_only_change_since_the_sweep_is_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str) -> None:
    monkeypatch.setattr(gate, "unchanged", changed_since_the_sweep(changed))
    built = gate.build(write_sweep(tmp_path / "sweep", answered_records()), None, tmp_path / "gate")
    assert built["sweep"] == BASELINE["sweep"] and len(built["runs"]) == len(RUNS)


@pytest.mark.parametrize(
    "changed",
    [
        "agent/prompts/system.md",
        "core/sentinelops_core/models.py",
        "core/sentinelops_core/patches.py",
        "agent/runner.py",
        "agent/guards.py",
        "agent/hooks.py",
        "agent/providers.py",
        "agent/evals/arms.py",
        "agent/knowledge_base.py",
        "agent/uv.lock",
        "tools/sentinelops_tools/toolset.py",
        "tools/sentinelops_tools/runbooks.py",
        "tools/tests/fixtures/windows/2026-11-17-0551.json",
        "runbooks/bad-deploy.md",
    ],
)
def test_a_run_shaping_change_since_the_sweep_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str) -> None:
    monkeypatch.setattr(gate, "unchanged", changed_since_the_sweep(changed))
    with pytest.raises(Refusal, match=rf"what shapes a run \(evals.baseline.RUN_SHAPING\) changed since the sweep ran on {BASELINE['commit'][:7]}"):
        gate.build(write_sweep(tmp_path / "sweep", answered_records()), None, tmp_path / "gate")
    assert not (tmp_path / "gate").exists()


def under(path: str, paths: tuple[str, ...]) -> bool:
    return any(path == p or path.startswith(p + "/") for p in paths)


def test_every_frozen_path_is_sorted_as_run_shaping_or_scoring_only() -> None:
    for frozen in audits.read(PASS_BAR)["frozen_paths"]:
        path = gate.MOVED.get(frozen, frozen)
        assert under(path, gate.RUN_SHAPING) != under(path, gate.SCORING_ONLY), frozen
    assert not set(gate.RUN_SHAPING) & set(gate.SCORING_ONLY)


def test_a_moved_frozen_path_is_gone_from_where_it_was_and_at_its_new_home() -> None:
    frozen = audits.read(PASS_BAR)["frozen_paths"]
    for old, new in gate.MOVED.items():
        assert old in frozen and not (REPO / old).exists() and (REPO / new).is_file(), old


def test_every_path_in_either_list_exists() -> None:
    # git diff passes a path that matches nothing, so a misspelled one would never be checked.
    missing = [p for p in (*gate.RUN_SHAPING, *gate.SCORING_ONLY) if not (REPO / p).exists()]
    assert not missing


def agent_files_imported_from(start: Path) -> set[str]:
    """Every agent or shared-package file start reaches by import, imports inside functions too, as paths from the repo's root."""

    def files(dotted: str) -> list[Path]:
        parts = dotted.split(".")
        found = []
        for root in (AGENT, CORE):
            for i in range(1, len(parts) + 1):
                base = root.joinpath(*parts[:i])
                found += [p for p in (base.with_suffix(".py"), base / "__init__.py") if p.is_file()]
        return found

    seen: set[Path] = set()
    todo = [start]
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
            else:
                continue
            for name in names:
                todo += files(name)
    return {p.relative_to(REPO).as_posix() for p in seen}


def test_every_agent_module_a_sweeps_run_imports_is_sorted() -> None:
    reached = agent_files_imported_from(AGENT / "evals" / "sweep.py")
    assert {
        "agent/runner.py",
        "core/sentinelops_core/models.py",
        "core/sentinelops_core/patches.py",
        "agent/providers.py",
        "agent/knowledge_base.py",
        "agent/evals/witness.py",
    } <= reached
    unsorted = sorted(p for p in reached if not under(p, gate.RUN_SHAPING) and not under(p, gate.SCORING_ONLY))
    assert not unsorted


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

    from sentinelops_core import models

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
