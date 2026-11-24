"""evals report: a tuning sweep's outcomes and costs, rubric failures, resends and trims, and citations.

On trial 3's nine runs (tests/fixtures/checker), with citations set as a
sweep with the runbook search would leave them. No model, no AWS.
"""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from answers import CHECKED_PATCH, PATCH, PATCH_OLD, PRICING_1_42_0, PRICING_1_43_0, checked
from checked_runs import RUNS

from evals import tuning
from evals.__main__ import main as evals_main

TRIAL = "20261114T190302Z"
TRIAL_RUNS = {key.split("/", 1)[1]: run for key, run in RUNS.items() if key.startswith(TRIAL + "/")}
SEARCH = {"toolUse": {"toolUseId": "s1", "name": "search_runbooks", "input": {"query": "x"}}}
# How each run's answer cites, and what its run did with it.
CITED = {
    "deploy-1027-1905-r1": ("bad-deploy.md", None),
    "deploy-1029-0156-r1": ("bad-deploy.md", None),
    "deploy-1112-0747-r1": ("db-pool-exhaustion.md", None),  # wrong
    "deploy-1112-0803-r1": (None, {"cited": "bad-deploy.md", "reason": "only listed"}),
    "deploy-1112-2000-r1": (None, None),
    "deploy-1113-0257-r1": ("bad-deploy.md", None),
    "pool-1027-1856-r1": ("db-pool-exhaustion.md", None),
    "pool-1027-1900-r1": ("db-pool-exhaustion.md", None),
    "pool-1027-1901-r1": (None, {"cited": "pool.md", "reason": "never shown"}),
}


def _folder(tmp_path: Path, *, error: bool = False) -> Path:
    folder = tmp_path / TRIAL
    folder.mkdir()
    records = []
    for name, run in TRIAL_RUNS.items():
        run = copy.deepcopy(run)
        cited, dropped = CITED[name]
        run["hypothesis"]["runbook_cited"] = cited
        run["citation_dropped"] = dropped
        run["tool_calls"] = ["get_metrics", "search_runbooks", "get_deploy_history"]
        if name == "pool-1027-1856-r1":  # an item citing the search: miscited
            run["hypothesis"]["evidence"].append({"kind": "count", "source": "r1", "count": 2, "note": "two shown"})
            run["messages"] += [{"role": "assistant", "content": [SEARCH]}]
        (folder / f"{name}.json").write_text(json.dumps(run), encoding="utf-8")
        records.append({"run": name, "scenario": run["eval"]["scenario"], "attempt": 1, "status": "answered", "cost_usd": 0.008, "transcript": f"{name}.json"})
    if error:
        records.append({"run": "pool-1027-1900-r2", "scenario": "pool-1027-1900", "attempt": 1, "status": "error", "cost_usd": 0.0, "transcript": None})
    meta = {
        "sweep": TRIAL, "arm": "haiku", "model": "claude-haiku-5-5", "split": "tuning", "commit": "a" * 40, "dirty": [],
        "runbooks_corpus": "197a0201c9eb", "runs_per_scenario": 1, "scenarios": sorted({r["scenario"] for r in records}),
        "records": records,
    }  # fmt: skip
    (folder / "sweep.json").write_text(json.dumps(meta), encoding="utf-8")
    return folder


def _never() -> dict[str, dict[str, str]]:
    raise AssertionError("no run kept a patch: the report needs no code")


def test_each_citation_is_counted_by_the_faults_own_runbook(tmp_path: Path) -> None:
    result = tuning.report(_folder(tmp_path), snapshot=_never)
    by_run = {r.run: r for r in result.runs}

    assert {name: r.citation for name, r in by_run.items()} == {
        "deploy-1027-1905-r1": "right", "deploy-1029-0156-r1": "right", "deploy-1112-0747-r1": "wrong",
        "deploy-1112-0803-r1": "dropped", "deploy-1112-2000-r1": "none", "deploy-1113-0257-r1": "right",
        "pool-1027-1856-r1": "right", "pool-1027-1900-r1": "right", "pool-1027-1901-r1": "dropped",
    }  # fmt: skip
    groups = result.summary
    assert [groups["all"][f"cited_{k}"] for k in tuning.CITATIONS] == [5, 1, 2, 1]
    assert [groups["bad_deploy"][f"cited_{k}"] for k in tuning.CITATIONS] == [3, 1, 1, 1]
    assert [groups["db_pool_exhaustion"][f"cited_{k}"] for k in tuning.CITATIONS] == [2, 0, 1, 0]
    assert groups["all"]["searches"] == 9 and groups["all"]["runs_searched"] == 9
    assert by_run["pool-1027-1856-r1"].miscited == ["e" + str(len(TRIAL_RUNS["pool-1027-1856-r1"]["hypothesis"]["evidence"]) + 1)]
    assert groups["all"]["miscited"] == 1


def test_the_report_lists_every_wrong_and_dropped_citation_and_rubric_failure(tmp_path: Path) -> None:
    text = tuning.render(tuning.report(_folder(tmp_path, error=True)))

    assert text.startswith(f"Tuning sweep {TRIAL}: TUNING, never a result")
    assert "commit aaaaaaa, clean tree, runbooks corpus 197a0201c9eb" in text
    assert "9 of 9 planned runs scored; 1 errored attempt(s), never scored" in text
    assert "  citations: 5 right, 1 wrong, 2 dropped, 1 none" in text
    assert "  deploy-1112-0747-r1: cited db-pool-exhaustion.md; the fault's runbook is bad-deploy.md" in text
    assert "  deploy-1112-0803-r1: cited bad-deploy.md (only listed); the fault's runbook is bad-deploy.md" in text
    assert "  pool-1027-1901-r1: cited pool.md (never shown); the fault's runbook is db-pool-exhaustion.md" in text
    # The thin trial run the rubric tests name (test_evals.THIN), listed for a hand read.
    assert "  pool-1027-1900-r1: diagnosis right; fails evidence_symptom" in text
    assert "Evidence items citing a runbook search or code read (miscited):\n  pool-1027-1856-r1: e" in text
    assert "Patches, each to read by hand:\n  (none)" in text and "Code reads:\n  (none)" in text
    assert "runbook" not in text.split("Rubric failures")[1].split("Citations wrong")[0]  # counted apart, not as a failure


def test_the_command_reports_a_sweep_and_refuses_a_folder_without_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert evals_main(["report", str(_folder(tmp_path))]) == 0
    assert "TUNING, never a result" in capsys.readouterr().out
    empty = tmp_path / "empty"
    empty.mkdir()
    assert evals_main(["report", str(empty)]) == 1
    assert "holds no sweep.json" in capsys.readouterr().err


def test_a_run_without_an_answer_has_no_citation(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    meta: dict[str, Any] = json.loads((folder / "sweep.json").read_text(encoding="utf-8"))
    meta["records"][0]["status"] = "no_hypothesis"
    (folder / "sweep.json").write_text(json.dumps(meta), encoding="utf-8")

    result = tuning.report(folder)

    assert result.runs[0].citation is None and result.runs[0].diagnosis is None
    assert result.summary["all"]["answered"] == 8 and result.summary["all"]["without_answer"] == 1


def test_every_patch_and_code_read_is_listed_for_a_hand_read(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    kept, dropped = folder / "deploy-1027-1905-r1.json", folder / "deploy-1029-0156-r1.json"
    run = json.loads(kept.read_text(encoding="utf-8"))
    run["hypothesis"]["remediation"] = CHECKED_PATCH
    reads = [
        {"toolUse": {"name": "read_repo_file", "toolUseId": "t8", "input": {"version": "1.43.0", "path": "checkout/pricing.py"}}},
        {"toolUse": {"name": "read_repo_file", "toolUseId": "t9", "input": {"version": "1.42.0"}}},
    ]
    run["messages"] += [{"role": "assistant", "content": reads}]
    kept.write_text(json.dumps(run), encoding="utf-8")
    run = json.loads(dropped.read_text(encoding="utf-8"))
    run["remediation_dropped"] = {"source": "c1", "file_path": "checkout/pricing.py", "reason": "edit 1: old text isn't in the file"}
    dropped.write_text(json.dumps(run), encoding="utf-8")
    snapshot = {"1.42.0": {"checkout/pricing.py": PRICING_1_42_0}, "1.43.0": {"checkout/pricing.py": PRICING_1_43_0}}

    result = tuning.report(folder, snapshot=lambda: snapshot)
    text = tuning.render(result)

    by_run = {r.run: r for r in result.runs}
    assert by_run["deploy-1027-1905-r1"].reads == ["1.43.0 checkout/pricing.py", "1.42.0 (listing)"]
    assert by_run["deploy-1027-1905-r1"].patched_is == ["1.42.0"]  # the null check put back, exactly
    assert [result.summary["all"][f"patch_{k}"] for k in tuning.PATCHES] == [1, 1, 7]
    assert result.summary["all"]["reads"] == 2 and result.summary["all"]["runs_read"] == 1
    assert "  patches: 1 kept, 1 dropped, 4 none" in text  # the bad deploys' group
    assert (
        "  deploy-1027-1905-r1 (bad_deploy): kept, checkout/pricing.py at 1.43.0; the file as patched is 1.42.0's exactly\n"
        f"    rationale: {CHECKED_PATCH['rationale']}\n"
    ) in text
    assert "    +    if promo is None:\n    +        return subtotal_cents\n" in text
    assert "  deploy-1029-0156-r1 (bad_deploy): dropped, checkout/pricing.py from c1: edit 1: old text isn't in the file" in text
    assert "Code reads:\n  deploy-1027-1905-r1: 1.43.0 checkout/pricing.py; 1.42.0 (listing)\n" in text


def test_a_kept_patch_that_makes_no_releases_file_says_so(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    path = folder / "deploy-1027-1905-r1.json"
    run = json.loads(path.read_text(encoding="utf-8"))
    other = {**PATCH, "edits": [{"old": PATCH_OLD, "new": PATCH_OLD.replace("    return ", "    if not promo:\n        return subtotal_cents\n    return ")}]}
    run["hypothesis"]["remediation"] = checked(other, PRICING_1_43_0, "1.43.0")
    path.write_text(json.dumps(run), encoding="utf-8")
    snapshot = {"1.42.0": {"checkout/pricing.py": PRICING_1_42_0}, "1.43.0": {"checkout/pricing.py": PRICING_1_43_0}}

    text = tuning.render(tuning.report(folder, snapshot=lambda: snapshot))

    assert "  deploy-1027-1905-r1 (bad_deploy): kept, checkout/pricing.py at 1.43.0; the file as patched is no release's" in text
