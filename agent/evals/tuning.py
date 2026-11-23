"""A tuning sweep's report: TUNING, never a result (BUILD_PLAN.md, Phase 3, "Step 4 as split").

No audit, no pass bar, no model, no AWS: what `evals grade` reports needs a
hand audit and, once the bar is frozen, a test sweep, and a tuning sweep has
neither. For every run, its outcome and cost, the rubric's items, the answers
sent back and trimmed, its runbook searches, the checker's flags, and its
citation, held to the scenario's own runbook (rubrics.RULES):

- right: runbook_cited is the fault's runbook;
- wrong: it names another;
- dropped: the run cleared what the model cited (runner.check_citation), and why;
- none: nothing cited, nothing dropped.

Groups: every run, then each fault's. Every rubric failure, wrong or dropped
citation, and miscited item is listed by run, for a hand read: the rubric's
diagnosis matches words, so a failure there may be a right answer it misreads.
"""

import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from checker.checks import Verdict
from checker.trial import rejected_answers
from evals.rubrics import RULES
from evals.rubrics import score as rubric_score
from evals.scenarios import BY_ID, WINDOW_NOTES
from evals.score import load_sweep

CITATIONS = ("right", "wrong", "dropped", "none")
# Reported apart from the rest: the citation has its own counts, and pr_gate_value is a value, not a pass.
_NOT_LISTED = ("runbook", "pr_gate_value")


@dataclass
class TuningRun:
    run: str
    scenario: str
    fault: str
    window: str
    status: str
    cost_usd: float
    expected: str  # the fault's runbook
    resends: int = 0
    trims: int = 0
    searches: int = 0
    diagnosis: bool | None = None  # the rubric's; None without an answer
    failed: list[str] = field(default_factory=list)  # rubric items that fail, but runbook and pr_gate_value
    notes: dict[str, str] = field(default_factory=dict)
    cited: str | None = None  # runbook_cited as the answer kept it
    citation: str | None = None  # right, wrong, dropped, or none; None without an answer
    dropped: dict[str, str] | None = None  # what the model cited and why the run cleared it
    flags: dict[str, str] = field(default_factory=dict)  # evidence item -> verdict
    miscited: list[str] = field(default_factory=list)  # evidence items citing a runbook search


@dataclass
class TuningReport:
    sweep: str
    meta: dict[str, Any]
    runs: list[TuningRun]
    errors: int  # attempts that errored, retried or not: never scored

    def group(self, rows: list[TuningRun]) -> dict[str, Any]:
        answered = [r for r in rows if r.citation is not None]
        costs = [r.cost_usd for r in rows]
        citations = Counter(r.citation for r in answered)
        return {
            "runs": len(rows),
            "answered": len(answered),
            "halted": sum(r.status == "halted" for r in rows),
            "without_answer": sum(r.status not in ("answered", "halted") for r in rows),
            "diagnosis_right": sum(bool(r.diagnosis) for r in rows),
            "median_cost": statistics.median(costs) if costs else None,
            "mean_cost": statistics.fmean(costs) if costs else None,
            "max_cost": max(costs) if costs else None,
            "total_cost": sum(costs),
            "resends": sum(r.resends for r in rows),
            "runs_resent": sum(r.resends > 0 for r in rows),
            "trims": sum(r.trims for r in rows),
            "runs_trimmed": sum(r.trims > 0 for r in rows),
            "searches": sum(r.searches for r in rows),
            "runs_searched": sum(r.searches > 0 for r in rows),
            **{f"cited_{kind}": citations[kind] for kind in CITATIONS},
            "flags": sum(len(r.flags) for r in rows),
            "miscited": sum(len(r.miscited) for r in rows),
        }

    @property
    def summary(self) -> dict[str, dict[str, Any]]:
        groups = {"all": self.group(self.runs)}
        for fault in sorted({r.fault for r in self.runs}):
            groups[fault] = self.group([r for r in self.runs if r.fault == fault])
        return groups


def citation(cited: str | None, dropped: dict[str, str] | None, expected: str) -> str:
    if dropped:
        return "dropped"
    if cited is None:
        return "none"
    return "right" if cited == expected else "wrong"


def report(folder: Path) -> TuningReport:
    sweep = load_sweep(folder)
    errors = sum(r["status"] == "error" for r in sweep.meta["records"])
    runs = []
    for record in sweep.records:
        scenario = BY_ID[record["scenario"]]
        transcript = sweep.transcripts[record["run"]]
        row = TuningRun(
            run=record["run"],
            scenario=scenario.id,
            fault=scenario.fault.value,
            window=scenario.window,
            status=record["status"],
            cost_usd=record["cost_usd"],
            expected=RULES[scenario.fault].runbook,
            resends=rejected_answers(transcript),
            trims=len(transcript.get("trims") or []),
            searches=sum(name == "search_runbooks" for name in transcript.get("tool_calls") or []),
        )
        check = sweep.checks[record["run"]]
        hypothesis = transcript.get("hypothesis")
        if check is not None and hypothesis:
            rubric = rubric_score(scenario, hypothesis, check.checks)
            row.diagnosis = rubric.diagnosis
            row.failed = [k for k, v in rubric.items.items() if not v and k not in _NOT_LISTED]
            row.notes = dict(rubric.notes)
            row.cited = hypothesis.get("runbook_cited")
            row.dropped = transcript.get("citation_dropped")
            row.citation = citation(row.cited, row.dropped, row.expected)
            row.flags = {c.id: c.verdict.value for c in check.flagged}
            row.miscited = [c.id for c in check.checks if c.verdict is Verdict.MISCITED]
        runs.append(row)
    return TuningReport(sweep.name, sweep.meta, runs, errors)


def _money(value: float | None) -> str:
    return "-" if value is None else f"${value:.4f}"


def render(result: TuningReport) -> str:
    meta = result.meta
    commit = (meta.get("commit") or "")[:7] or "no recorded commit"
    planned = len(meta.get("scenarios") or []) * int(meta.get("runs_per_scenario") or 0)
    out = [
        f"Tuning sweep {result.sweep}: TUNING, never a result",
        f"arm {meta.get('arm', '-')} ({meta.get('model', '-')}), split {meta.get('split', '-')}, commit {commit}"
        + (", uncommitted changes" if meta.get("dirty") else ", clean tree")
        + f", runbooks corpus {meta.get('runbooks_corpus') or 'not recorded'}",
        f"{len(result.runs)} of {planned} planned runs scored; {result.errors} errored attempt(s), never scored",
    ]
    for name, g in result.summary.items():
        out += [
            "",
            f"{name} ({g['runs']} runs): {g['answered']} answered, {g['halted']} halted, {g['without_answer']} without an answer",
            f"  diagnosis right, by the rubric: {g['diagnosis_right']} of {g['runs']}",
            f"  cost: median {_money(g['median_cost'])}, mean {_money(g['mean_cost'])}, max {_money(g['max_cost'])}, "
            f"total {_money(g['total_cost'])}",
            f"  answers sent back: {g['resends']} (in {g['runs_resent']} runs); trimmed: {g['trims']} (in {g['runs_trimmed']} runs)",
            f"  runbook searches: {g['searches']}, in {g['runs_searched']} of {g['runs']} runs",
            "  citations: " + ", ".join(f"{g[f'cited_{kind}']} {kind}" for kind in CITATIONS),
            f"  checker flags: {g['flags']} ({g['miscited']} miscited: a runbook search cited as evidence)",
        ]

    failed = [r for r in result.runs if r.failed]
    out += ["", "Rubric failures, each to read by hand:"]
    out += [
        f"  {r.run}: {'diagnosis wrong' if r.diagnosis is False else 'diagnosis right'}; fails {', '.join(r.failed)}"
        + "".join(f"; {k}: {v}" for k, v in r.notes.items())
        for r in failed
    ] or ["  (none)"]
    for kind in ("wrong", "dropped"):
        rows = [r for r in result.runs if r.citation == kind]
        out += ["", f"Citations {kind}, each to read by hand:"]
        out += [
            f"  {r.run}: cited {(r.dropped or {}).get('cited') if kind == 'dropped' else r.cited}"
            + (f" ({r.dropped['reason']})" if r.dropped else "")
            + f"; the fault's runbook is {r.expected}"
            for r in rows
        ] or ["  (none)"]
    miscited = [r for r in result.runs if r.miscited]
    out += ["", "Evidence items citing a runbook search (miscited):"]
    out += [f"  {r.run}: {', '.join(r.miscited)}" for r in miscited] or ["  (none)"]

    out += ["", "Runs:", f"  {'run':<26} {'status':<14} {'cost':>8}  {'diagnosis':<9} {'citation':<8} {'cited':<22} resends trims searches flags"]
    for r in result.runs:
        diagnosis = "-" if r.diagnosis is None else "right" if r.diagnosis else "wrong"
        cited = r.cited or ((r.dropped or {}).get("cited") or "-")
        out.append(
            f"  {r.run:<26} {r.status:<14} {_money(r.cost_usd):>8}  {diagnosis:<9} {r.citation or '-':<8} {cited[:22]:<22} "
            f"{r.resends:>7} {r.trims:>5} {r.searches:>8} {len(r.flags):>5}"
        )
    notes = sorted({r.window for r in result.runs} & set(WINDOW_NOTES))
    if notes:
        out += ["", "Windows the rubric isn't blind to:"] + [f"  {w}: {WINDOW_NOTES[w]}" for w in notes]
    return "\n".join(out)

