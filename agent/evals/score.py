"""Score a sweep once: the checker's flags against a hand audit, by ID, and the result against the pass bar.

Fixed before a test sweep runs (BUILD_PLAN.md, "The grader redesign"):

- The audit is committed before scoring and covers every scored run, with or
  without slips, so an unaudited run can't pass as a clean one; `audit.problems`
  refuses a label that points at nothing.
- A flag catches a label when it's on the label's own target (an evidence item's
  ID) and says the label's own complaint. A flag on the target saying another
  complaint is reported beside the catches, not counted.
- Every flag is hand-checked, matched or not, in a committed file: precision is
  the flags called right over all flags, and waits for that file. A flag called
  right with no label on its target is an audit miss; labels are never edited
  after scoring.
- Strict recall is strict labels caught over strict labels; prose-only labels
  (which no flag can match) count in it if the pass bar says so, and their share
  of the strict labels is R4's trigger for a judge.
- The pass bar is a committed file (`pass_bar.json`, R7). Until it's frozen, a
  report is tuning, never a result; once frozen, a sweep must be the test
  split's and have run at or after the freeze, on the frozen paths as they were
  then; nothing frozen may have changed since, and the scenarios and windows
  (`fixed_from_sweep`, which grow after the freeze) must be as they were when
  the sweep ran.
- The report gives every group its own figures: all runs, the runs the first
  grader's rules were and weren't written on, and each fault's. It counts the
  tool calls runs made after they were asked for their answer, and says where
  the rubric wasn't blind to a window (`scenarios.WINDOW_NOTES`).
"""

import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from checker.__main__ import RunCheck, check_run, load
from checker.trial import final_records
from evals import audit as audits
from evals.repo import REPO, Refusal, check_committed, dirty, git, head, unchanged
from evals.rubrics import score as rubric_score
from evals.scenarios import BY_ID, WINDOW_NOTES, Split

PASS_BAR = Path(__file__).with_name("pass_bar.json")
# A transcript from before the count has no tool_calls_after_ask at all, which isn't "never asked" (null).
_NOT_RECORDED: Any = object()
__all__ = [
    "REPO", "Refusal", "check_committed", "PASS_BAR", "Sweep", "load_sweep", "score_sweep", "render",
    "check_before_test_sweep",
]


@dataclass
class Sweep:
    name: str
    meta: dict[str, Any]
    records: list[dict[str, Any]]  # each scored run's final attempt
    transcripts: dict[str, dict[str, Any]]
    checks: dict[str, RunCheck | None]  # None without an answer the checker can read

    @property
    def answers(self) -> dict[str, dict[str, Any] | None]:
        return {r["run"]: self.transcripts[r["run"]].get("hypothesis") if r["status"] in ("answered", "halted") else None for r in self.records}

    def flags(self) -> dict[str, dict[str, str]]:
        return {run: {c.id: c.verdict.value for c in check.flagged} for run, check in self.checks.items() if check}


def load_sweep(folder: Path) -> Sweep:
    meta = audits.read(folder / "sweep.json")
    records = [r for r in final_records(meta) if r["status"] != "error"]
    transcripts, checks = {}, {}
    for r in records:
        path = folder / r["transcript"]
        transcripts[r["run"]] = load(path) if path.exists() else {}
        answered = r["status"] == "answered" and transcripts[r["run"]].get("hypothesis")
        checks[r["run"]] = check_run(r["run"], transcripts[r["run"]]) if answered else None
    return Sweep(meta.get("sweep", folder.name), meta, records, transcripts, checks)


@dataclass
class RunScore:
    run: str
    scenario: str
    seen: bool
    fault: str
    split: str
    window: str
    status: str
    cost_usd: float
    diagnosis: bool | None  # the rubric's; None without an answer
    confidently_wrong: bool
    audit_diagnosis: str
    items: int
    flags: dict[str, str]  # id -> verdict
    strict: list[audits.Label] = field(default_factory=list)
    loose: list[audits.Label] = field(default_factory=list)
    caught: list[audits.Label] = field(default_factory=list)  # strict, by a flag saying its complaint
    other_complaint: list[audits.Label] = field(default_factory=list)  # strict, flagged, but saying something else
    loose_flagged: list[audits.Label] = field(default_factory=list)
    calls: dict[str, str] = field(default_factory=dict)  # id -> right | false_flag, once hand-checked
    audit_misses: list[str] = field(default_factory=list)  # flags called right on a target no label names
    asked: bool | None = None  # asked for its answer, or it answered unasked; None if the transcript doesn't say
    after_ask: int = 0  # the tool calls it made after the ask


def score_run(
    record: dict[str, Any],
    check: RunCheck | None,
    hypothesis: dict[str, Any] | None,
    entry: dict[str, Any],
    run_labels: list[audits.Label],
    calls: dict[str, Any] | None,
    transcript: dict[str, Any] | None = None,
) -> RunScore:
    scenario = BY_ID[record["scenario"]]
    after = (transcript or {}).get("tool_calls_after_ask", _NOT_RECORDED)
    rubric = rubric_score(scenario, hypothesis, check.checks) if check and hypothesis else None
    flags = {c.id: c.verdict.value for c in check.flagged} if check else {}
    complaints = {c.id: audits.FLAG_COMPLAINT[c.verdict] for c in (check.flagged if check else [])}
    out = RunScore(
        run=record["run"],
        scenario=scenario.id,
        seen=scenario.seen,
        fault=scenario.fault.value,
        split=scenario.split.value,
        window=scenario.window,
        status=record["status"],
        cost_usd=record["cost_usd"],
        diagnosis=rubric.diagnosis if rubric else None,
        confidently_wrong=bool(rubric and rubric.confidently_wrong),
        audit_diagnosis=entry.get("diagnosis", ""),
        items=len(check.checks) if check else 0,
        flags=flags,
        strict=[lb for lb in run_labels if lb.kind == "strict"],
        loose=[lb for lb in run_labels if lb.kind == "loose"],
        asked=None if after is _NOT_RECORDED else after is not None,
        after_ask=len(after) if isinstance(after, list) else 0,
    )
    out.caught = [lb for lb in out.strict if complaints.get(lb.target) == lb.complaint]
    out.other_complaint = [lb for lb in out.strict if lb.target in flags and complaints[lb.target] != lb.complaint]
    out.loose_flagged = [lb for lb in out.loose if lb.target in flags]
    if calls is not None:
        out.calls = {i: calls[i]["call"] for i in flags}
        labeled = {lb.target for lb in run_labels}
        out.audit_misses = [i for i, call in out.calls.items() if call == "right" and i not in labeled]
    return out


@dataclass
class Report:
    sweep: str
    arm: str
    model: str
    bar: dict[str, Any]
    audit_commit: str
    flags_commit: str | None
    scores: list[RunScore]

    def group(self, rows: list[RunScore]) -> dict[str, Any]:
        strict = [lb for r in rows for lb in r.strict]
        prose = [lb for lb in strict if not lb.checkable]
        caught = sum(len(r.caught) for r in rows)
        flags = sum(len(r.flags) for r in rows)
        answered = [r for r in rows if r.diagnosis is not None]
        recorded = [r for r in rows if r.asked is not None]
        recall_over = len(strict) if self.bar["recall_includes_prose_only"] else len(strict) - len(prose)
        out: dict[str, Any] = {
            "runs": len(rows),
            "answered": len(answered),
            "diagnosis_right": sum(bool(r.diagnosis) for r in rows),
            "confidently_wrong": sum(r.confidently_wrong for r in rows),
            "rubric_agrees_with_audit": sum(bool(r.diagnosis) == (r.audit_diagnosis == "right") for r in rows),
            "items": sum(r.items for r in rows),
            "strict_labels": len(strict),
            "prose_only": len(prose),
            "prose_share": len(prose) / len(strict) if strict else None,
            "caught": caught,
            "caught_other_complaint": sum(len(r.other_complaint) for r in rows),
            "strict_recall": caught / recall_over if recall_over else None,
            "checkable_recall": caught / (len(strict) - len(prose)) if len(strict) > len(prose) else None,
            "loose_labels": sum(len(r.loose) for r in rows),
            "loose_flagged": sum(len(r.loose_flagged) for r in rows),
            "flags": flags,
            "strict_per_run": len(strict) / len(answered) if answered else None,
            "flags_per_run": flags / len(answered) if answered else None,
            "median_cost": statistics.median([r.cost_usd for r in answered]) if answered else None,
            "mean_cost": statistics.fmean([r.cost_usd for r in answered]) if answered else None,
            "asked_recorded": len(recorded),
            "asked": sum(bool(r.asked) for r in recorded),
            "calls_after_ask": sum(r.after_ask for r in rows),
            "runs_with_calls_after_ask": sum(r.after_ask > 0 for r in rows),
        }
        if self.flags_commit is not None:
            right = sum(call == "right" for r in rows for call in r.calls.values())
            out["flags_right"] = right
            out["precision"] = right / flags if flags else None
            out["audit_misses"] = sum(len(r.audit_misses) for r in rows)
        return out

    @property
    def summary(self) -> dict[str, dict[str, Any]]:
        """Every run; the runs the first grader's rules weren't and were written on; then each fault's."""
        groups = {
            "all": self.group(self.scores),
            "unseen": self.group([s for s in self.scores if not s.seen]),
            "seen": self.group([s for s in self.scores if s.seen]),
        }
        for fault in sorted({s.fault for s in self.scores}):
            groups[fault] = self.group([s for s in self.scores if s.fault == fault])
        return groups

    @property
    def split(self) -> str:
        splits = sorted({s.split for s in self.scores})
        return splits[0] if len(splits) == 1 else "mixed" if splits else "-"

    @property
    def verdict(self) -> str:
        """PASS or FAIL against the bar; or why there's no verdict."""
        if not self.bar["frozen"]:
            return "TUNING: the pass bar isn't frozen, so this is never a result"
        whole = self.group(self.scores)
        if self.flags_commit is None or whole["precision"] is None:
            return "WAITING: precision needs the hand check of every flag (--flags)"
        recall = whole["strict_recall"] or 0.0
        ok = whole["precision"] >= self.bar["precision"] and recall >= self.bar["strict_recall"]
        return "PASS" if ok else "FAIL"


def check_test_split(sweep: Sweep) -> None:
    """Under a frozen bar only a test-split sweep is a result: one holding any other scenario is refused."""
    ids = sorted({r["scenario"] for r in sweep.records} | set(sweep.meta.get("scenarios") or []))
    unknown = [i for i in ids if i not in BY_ID]
    if unknown:
        raise Refusal(f"the sweep names scenarios that aren't in scenarios.py: {', '.join(unknown)}")
    other = [i for i in ids if BY_ID[i].split is not Split.TEST]
    if other:
        raise Refusal(f"the bar is frozen, and only a test-split sweep is a result: this one holds tuning scenarios ({', '.join(other)})")


def check_freeze(bar: dict[str, Any], bar_commit: str, meta: dict[str, Any]) -> None:
    """A frozen bar's commit is the freeze: the sweep ran at or after it, on the frozen paths as they were, and they're unchanged now.

    The scenarios and the windows grow after the freeze (the test breaks are
    made after it), so they're held to the sweep's own commit instead: the
    bar's fixed_from_sweep paths must be now as they were when the sweep ran.
    """
    paths = bar["frozen_paths"]
    fixed = bar.get("fixed_from_sweep")
    if fixed is None:
        raise Refusal("the frozen bar names no fixed_from_sweep paths, so nothing holds the scenarios and windows to the sweep's own")
    commit = meta.get("commit")
    if not commit:
        raise Refusal("the sweep doesn't record the commit it ran on, so it can't be held to the freeze")
    if meta.get("dirty"):
        raise Refusal(f"the sweep ran with uncommitted changes: {', '.join(meta['dirty'][:5])}")
    if git("merge-base", "--is-ancestor", bar_commit, commit).returncode != 0:
        raise Refusal(f"the sweep ran on {commit[:7]}, before the freeze at {bar_commit[:7]} (or the bar changed after it ran)")
    if not unchanged(bar_commit, paths, commit):
        raise Refusal(f"the sweep ran on frozen paths that differ from the freeze at {bar_commit[:7]}")
    if not unchanged(bar_commit, paths):
        raise Refusal(f"frozen paths have changed since the freeze at {bar_commit[:7]}: score with them as frozen")
    if fixed and not unchanged(commit, fixed):
        raise Refusal(f"{', '.join(fixed)} changed since the sweep ran on {commit[:7]}: score with them as they were then")


def check_before_test_sweep(bar_path: Path | None = None, committed: Callable[[Path], str] = check_committed) -> None:
    """Before a test sweep spends anything: raises Refusal unless it could be graded as it would run now.

    The bar is frozen and committed, the tree is clean, and HEAD is at or
    after the freeze with the frozen paths as frozen: what check_freeze will
    ask of the sweep at grading, asked of the commit it's about to run on.
    """
    bar_path = bar_path or PASS_BAR
    bar = audits.read(bar_path)
    if not bar["frozen"]:
        raise Refusal("the test split runs only after the freeze, and the pass bar isn't frozen")
    check_freeze(bar, committed(bar_path), {"commit": head(), "dirty": dirty()})


def score_sweep(
    folder: Path,
    audit_path: Path,
    flags_path: Path | None = None,
    bar_path: Path = PASS_BAR,
    committed: Callable[[Path], str] = check_committed,
) -> Report:
    """The sweep scored against its audit; raises Refusal when the result wouldn't be a fair measure."""
    bar = audits.read(bar_path)
    bar_commit = committed(bar_path)
    sweep = load_sweep(folder)
    if bar["frozen"]:
        check_test_split(sweep)
        check_freeze(bar, bar_commit, sweep.meta)
    audit = audits.read(audit_path)
    audit_commit = committed(audit_path)
    wrong = audits.problems(audit, sweep.name, sweep.answers)
    if wrong:
        raise Refusal("the audit doesn't fit the sweep:\n  " + "\n  ".join(wrong))
    calls, flags_commit = None, None
    if flags_path is not None:
        calls = audits.read(flags_path)
        flags_commit = committed(flags_path)
        wrong = audits.flag_problems(calls, sweep.name, sweep.flags())
        if wrong:
            raise Refusal("the flag check doesn't cover the sweep's flags as they are:\n  " + "\n  ".join(wrong))
    by_run: dict[str, list[audits.Label]] = {}
    for lb in audits.labels(audit):
        by_run.setdefault(lb.run, []).append(lb)
    scores = [
        score_run(
            r,
            sweep.checks[r["run"]],
            sweep.answers[r["run"]],
            audit["runs"][r["run"]],
            by_run.get(r["run"], []),
            None if calls is None else (calls["flags"].get(r["run"]) or {}),
            sweep.transcripts[r["run"]],
        )
        for r in sweep.records
    ]
    return Report(sweep.name, sweep.meta.get("arm", "haiku"), sweep.meta.get("model", "?"), bar, audit_commit, flags_commit, scores)


def _pct(part: float | None) -> str:
    return "-" if part is None else f"{part:.0%}"


def _asked(g: dict[str, Any]) -> str:
    """How many runs were asked for their answer, and the tool calls made after the ask."""
    if not g["asked_recorded"]:
        return "  asked for the answer: not recorded (transcripts from before the count)"
    missing = g["runs"] - g["asked_recorded"]
    return (
        f"  asked for the answer in {g['asked']} of {g['asked_recorded']} runs"
        + (f" ({missing} not recorded)" if missing else "")
        + f"; tool calls after the ask: {g['calls_after_ask']}, in {g['runs_with_calls_after_ask']} run(s)"
    )


def render(report: Report) -> str:
    bar = report.bar
    lines = [
        f"sweep {report.sweep}, arm {report.arm} ({report.model}), the {report.split} split; audit {report.audit_commit[:7]}"
        + (f", flags checked {report.flags_commit[:7]}" if report.flags_commit else ", flags not yet checked"),
        f"verdict: {report.verdict}",
        "",
    ]
    for name, g in report.summary.items():
        # An empty group says nothing, and seen or unseen holding every run only repeats "all".
        if not g["runs"] or (name in ("seen", "unseen") and g["runs"] == len(report.scores)):
            continue
        lines += [
            f"{name}: {g['runs']} runs ({g['answered']} answered), diagnoses right {g['diagnosis_right']}, "
            f"confidently wrong {g['confidently_wrong']}, rubric agrees with the audit on {g['rubric_agrees_with_audit']}",
            f"  strict recall {g['caught']} of {g['strict_labels'] if bar['recall_includes_prose_only'] else g['strict_labels'] - g['prose_only']} "
            f"({_pct(g['strict_recall'])}; bar {bar['strict_recall']:.0%}); on checkable labels {_pct(g['checkable_recall'])}; "
            f"{g['caught_other_complaint']} more flagged with another complaint",
            f"  prose-only {g['prose_only']} of {g['strict_labels']} strict labels ({_pct(g['prose_share'])}; "
            f"a judge if {bar['judge_if_prose_share_at_least']:.0%} or more)",
            f"  flags {g['flags']} on {g['items']} items"
            + (
                f"; right {g['flags_right']} ({_pct(g['precision'])}; bar {bar['precision']:.0%}); audit misses {g['audit_misses']}"
                if "precision" in g
                else "; precision waits on the hand check"
            ),
            f"  loose labels {g['loose_labels']}, {g['loose_flagged']} of them flagged",
            "  per answered run: "
            + (
                f"{g['strict_per_run']:.2f} strict slips, {g['flags_per_run']:.2f} flags, "
                f"median ${g['median_cost']:.4f}, mean ${g['mean_cost']:.4f}"
                if g["answered"]
                else "-"
            ),
            _asked(g),
        ]
    notes = {s.window: WINDOW_NOTES[s.window] for s in report.scores if s.window in WINDOW_NOTES}
    if notes:
        lines += ["", "notes:"] + [f"  window {window}: {note}" for window, note in sorted(notes.items())]
    misses = [(s.run, lb) for s in report.scores for lb in s.strict if lb not in s.caught]
    if misses:
        lines += ["", "strict labels not caught:"]
        lines += [f"  {run} {lb.target} {lb.complaint}{'' if lb.checkable else ' (prose-only)'}: {lb.note}" for run, lb in misses]
    if report.flags_commit is not None:
        extra = [(s.run, i) for s in report.scores for i in s.audit_misses]
        if extra:
            lines += ["", "flags called right that no label names (audit misses):"]
            lines += [f"  {run} {i} ({next(s for s in report.scores if s.run == run).flags[i]})" for run, i in extra]
    return "\n".join(lines)
