"""The typed-evidence trial's five pass marks, read from a sweep folder (BUILD_PLAN.md, "The grader redesign").

Approved 2026-11-14 and committed here before the trial runs, so they can't
move to fit its result. A fail means rebuilding the format as R1's fallback,
sources cited beside free text, and running the trial again.

The diagnosis mark uses the rubric, which can count a right answer as wrong
(its ruling-out words miss "unchanged" and "healthy"): every run it fails is
listed for a hand read, and the hand read decides.
"""

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from checker.__main__ import RunCheck, check_run, load
from checker.resolve import Status
from evals import rubrics
from evals.scenarios import BY_ID

# Sweep 20261113T201129Z: the median cost of its 26 answered runs, in the old format.
BASELINE_MEDIAN_USD = 0.0054395
# Set for 9 runs, one per alarm; a folder of another size is held to the same shares.
MAX_WITHOUT_ANSWER = 1 / 9  # runs with no valid answer inside the resend cap
MIN_FOLLOWED = 0.90  # citations that can be followed
MIN_TYPED = 2 / 3  # typed items over all evidence items and inferences
MIN_DIAGNOSED = 8 / 9  # right by the rubric, its failures read by hand
MAX_COST_RATIO = 1.5  # the median answered run against the baseline


@dataclass
class TrialRun:
    run: str
    status: str
    cost_usd: float
    resends: int
    check: RunCheck | None
    diagnosis: bool | None  # the rubric's; None without an answer


@dataclass
class Mark:
    name: str
    value: str
    bar: str
    passed: bool


@dataclass
class Trial:
    runs: list[TrialRun] = field(default_factory=list)
    marks: list[Mark] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(mark.passed for mark in self.marks)


def final_records(sweep: dict[str, Any]) -> list[dict[str, Any]]:
    """Each run's last attempt: a retried run keeps its errored attempt's record too."""
    final: dict[str, dict[str, Any]] = {}
    for record in sweep["records"]:
        if record["run"] not in final or record["attempt"] > final[record["run"]]["attempt"]:
            final[record["run"]] = record
    return list(final.values())


def rejected_answers(transcript: dict[str, Any]) -> int:
    """How many answers failed validation and went back to the model."""
    names: dict[str, str] = {}
    count = 0
    for message in transcript.get("messages", []):
        for block in message.get("content", []):
            if "toolUse" in block:
                names[block["toolUse"]["toolUseId"]] = block["toolUse"]["name"]
            elif "toolResult" in block:
                result = block["toolResult"]
                count += names.get(result["toolUseId"]) == "Hypothesis" and result.get("status") == "error"
    return count


def trial(folder: Path) -> Trial:
    sweep = json.loads((folder / "sweep.json").read_text(encoding="utf-8"))
    out = Trial()
    for record in final_records(sweep):
        path = folder / record["transcript"]
        transcript = load(path) if path.exists() else {}
        answered = record["status"] == "answered"
        hypothesis = transcript.get("hypothesis") if answered else None
        diagnosis = None
        if hypothesis:
            diagnosis = rubrics.score(BY_ID[record["scenario"]], hypothesis).diagnosis
        out.runs.append(
            TrialRun(
                record["run"],
                record["status"],
                record["cost_usd"],
                rejected_answers(transcript),
                check_run(record["run"], transcript) if hypothesis else None,
                diagnosis,
            )
        )

    checks = [r.check for r in out.runs if r.check]
    without = sum(r.check is None for r in out.runs)
    typed = sum(c.typed for c in checks)
    followed = sum(c.count(Status.RESOLVED) + c.count(Status.OUTSIDE_READ) for c in checks)
    claims = sum(c.evidence + c.inferences for c in checks)
    n = len(out.runs)
    diagnosed = sum(bool(r.diagnosis) for r in out.runs)
    costs = [r.cost_usd for r in out.runs if r.check]
    median = statistics.median(costs) if costs else None
    followed_share = followed / typed if typed else 0.0
    typed_share = typed / claims if claims else 0.0
    # A hair under each bar's share, so 1 of 9 and 8 of 9 sit exactly on it in floating point.
    out.marks = [
        Mark("runs without a valid answer", f"{without} of {n}", "at most 1 in 9", n > 0 and without <= MAX_WITHOUT_ANSWER * n + 1e-9),
        Mark("citations followed", f"{followed} of {typed} ({followed_share:.0%})", f"at least {MIN_FOLLOWED:.0%}", followed_share >= MIN_FOLLOWED),
        Mark("claims typed, not inferences", f"{typed} of {claims} ({typed_share:.0%})", "at least two-thirds", typed_share >= MIN_TYPED - 1e-9),
        Mark("diagnoses right, by the rubric", f"{diagnosed} of {n}", "at least 8 in 9", n > 0 and diagnosed >= MIN_DIAGNOSED * n - 1e-9),
        Mark(
            "median cost of an answered run",
            "-" if median is None else f"${median:.4f} ({median / BASELINE_MEDIAN_USD:.2f}x)",
            f"at most {MAX_COST_RATIO}x ${BASELINE_MEDIAN_USD:.4f}",
            median is not None and median <= MAX_COST_RATIO * BASELINE_MEDIAN_USD,
        ),
    ]
    return out


def render(result: Trial) -> str:
    out = []
    for r in result.runs:
        diagnosis = {True: "right", False: "wrong by the rubric: read it by hand", None: "-"}[r.diagnosis]
        followed = "-" if r.check is None or r.check.resolved_share is None else f"{r.check.resolved_share:.0%}"
        out.append(
            f"{r.run}: {r.status}, {r.resends} resends, ${r.cost_usd:.4f}, citations followed {followed}, diagnosis {diagnosis}"
        )
    out.append("")
    for mark in result.marks:
        out.append(f"{'PASS' if mark.passed else 'FAIL'}  {mark.name}: {mark.value} (bar: {mark.bar})")
    out.append(f"trial {'passed' if result.passed else 'failed'}")
    return "\n".join(out)
