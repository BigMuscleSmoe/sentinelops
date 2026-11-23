"""Hand labels on a sweep, by ID: what a label points at, what it may say, and the checks it must pass.

A label points at one part of a run's answer (BUILD_PLAN.md, "The grader
redesign", the scorer):

- e1, e2, ...: an evidence item, by position, as the answer's inferences cite them;
- s1, s2, ...: a sentence of root_cause, numbered by `sentences` (the packet shows the numbers);
- i1, i2, ...: an inference, by position.

It says one kind of complaint. Three are what a checker flag can say about an
evidence item; the fourth is for what no flag can:

- contradicted: what it cites shows otherwise (flag: contradicted);
- outside_read: it states something about rows, lines, or a period its read doesn't cover (flag: outside_read);
- miscited: what it cites doesn't exist, or isn't what it says (flag: unresolved), or is a
  runbook search, which is never evidence (flag: miscited);
- unsupported: no evidence carries it (a figure no item states, a cause or comparison that doesn't follow).

A label is code-checkable when it points at an evidence item with one of the
first three; every other label is prose-only, the residue a judge would be for
(R4). One label per target and complaint: two slips of one kind in one item go
in one label's note, so a single flag can't count twice.

An audit, a hand check of the flags, and the user's calibration labels share
this file format; `problems` is the audit tool's refusal, run before anything is
scored against one.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from checker.checks import Verdict

DIAGNOSES = ("right", "partly", "wrong", "none")
KINDS = ("strict", "loose")
COMPLAINTS = ("contradicted", "outside_read", "miscited", "unsupported")
FLAG_COMPLAINT = {
    Verdict.CONTRADICTED: "contradicted",
    Verdict.OUTSIDE_READ: "outside_read",
    Verdict.UNRESOLVED: "miscited",
    Verdict.MISCITED: "miscited",
}
FLAG_CALLS = ("right", "false_flag")
_TARGET = re.compile(r"([esi])([1-9]\d*)")
# A sentence ends at . ! or ? before a space and a capital, digit, or opening mark:
# "1.43.0" and "18:56:06.039Z" don't end one, and "e.g. the" doesn't either.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\"'`])")

GUIDE = (
    "Label each run blind, from this packet only. diagnosis: right = names the injected fault's cause and "
    "mechanism and blames nothing else; partly = one of those missing; wrong = blames something else; none = "
    "no answer. slips: claims past the run's own tool output, each {target, kind, complaint, note}. target: "
    "e<n> an evidence item, s<n> a root_cause sentence, i<n> an inference, as the packet numbers them. kind: "
    "strict = the output contradicts it or never showed it; loose = roughly true but past the data (an "
    "'about' hiding a wider range, a vague scope). complaint: contradicted, outside_read (about rows, lines, "
    "or a period its read doesn't cover), miscited (cites what doesn't exist or isn't what it says), "
    "unsupported (no evidence carries it). One label per target and complaint; a run with no slips has []."
)


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_END.split(text.strip()) if s.strip()]


def targets(hypothesis: dict[str, Any] | None) -> dict[str, str]:
    """Every ID a label may point at in this answer, with the text it names. None for a halted run or no answer."""
    if not hypothesis or hypothesis.get("halted_by"):
        return {}
    out = {f"s{n}": s for n, s in enumerate(sentences(hypothesis.get("root_cause") or ""), 1)}
    for n, item in enumerate(hypothesis.get("evidence") or [], 1):
        out[f"e{n}"] = json.dumps(item, ensure_ascii=False)
    for n, inference in enumerate(hypothesis.get("inferences") or [], 1):
        out[f"i{n}"] = inference.get("claim", "")
    return out


@dataclass(frozen=True)
class Label:
    run: str
    target: str
    kind: str
    complaint: str
    note: str

    @property
    def checkable(self) -> bool:
        """Whether a checker flag could ever match it: an evidence item, with a complaint a flag can say."""
        return self.target.startswith("e") and self.complaint in FLAG_COMPLAINT.values()


def read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def labels(audit: dict[str, Any]) -> list[Label]:
    return [
        Label(run, s["target"], s["kind"], s["complaint"], s.get("note", ""))
        for run, entry in audit.get("runs", {}).items()
        for s in entry.get("slips", [])
    ]


def problems(audit: dict[str, Any], sweep: str, answers: dict[str, dict[str, Any] | None], *, every_run: bool = True) -> list[str]:
    """What's wrong with an audit of these runs' answers (run -> Hypothesis, None without one); [] if nothing.

    every_run: the audit must cover every run (a blind audit of a sweep); the
    calibration labels cover only the runs they were given.
    """
    out = []
    if audit.get("sweep") != sweep:
        out.append(f"it audits sweep {audit.get('sweep')!r}, not {sweep}")
    runs = audit.get("runs")
    if not isinstance(runs, dict):
        return out + ["it has no runs"]
    if every_run:
        out += [f"{run}: not audited" for run in answers if run not in runs]
    for run, entry in runs.items():
        if run not in answers:
            out.append(f"{run}: not a scored run of {sweep}")
            continue
        names = targets(answers[run])
        if entry.get("diagnosis") not in DIAGNOSES:
            out.append(f"{run}: diagnosis {entry.get('diagnosis')!r} isn't one of {', '.join(DIAGNOSES)}")
        elif not names and entry["diagnosis"] != "none":
            out.append(f"{run}: no answer, so its diagnosis is none")
        seen = set()
        for i, slip in enumerate(entry.get("slips", []), 1):
            where = f"{run}: slip {i}"
            target, kind, complaint = slip.get("target"), slip.get("kind"), slip.get("complaint")
            if not isinstance(target, str) or not _TARGET.fullmatch(target):
                out.append(f"{where}: target {target!r} isn't e<n>, s<n>, or i<n>")
            elif target not in names:
                out.append(f"{where}: {target} points at nothing in this answer ({_listing(names)})")
            if kind not in KINDS:
                out.append(f"{where}: kind {kind!r} isn't strict or loose")
            if complaint not in COMPLAINTS:
                out.append(f"{where}: complaint {complaint!r} isn't one of {', '.join(COMPLAINTS)}")
            if not str(slip.get("note", "")).strip():
                out.append(f"{where}: no note")
            if (target, complaint) in seen:
                out.append(f"{where}: a second {complaint} label on {target}: one label per target and complaint")
            seen.add((target, complaint))
    return out


def _listing(names: dict[str, str]) -> str:
    if not names:
        return "it has none"
    by = {p: sorted((int(k[1:]) for k in names if k[0] == p)) for p in "esi"}
    return ", ".join(f"{p}1-{p}{n[-1]}" for p, n in by.items() if n) or "none"


def blank(sweep: str, runs: list[str], about: str = GUIDE) -> dict[str, Any]:
    """An empty label file for these runs, to fill in by hand."""
    return {
        "about": about,
        "sweep": sweep,
        "labeler": "",
        "blind": True,
        "runs": {run: {"diagnosis": "", "note": "", "slips": []} for run in runs},
    }


# --- the hand check of every flag -----------------------------------------------


def flag_template(sweep: str, checker_commit: str | None, flags: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Every flag of a sweep (run -> [{id, verdict, reason, item}]), each with a blank call to fill in."""
    return {
        "about": (
            "Every checker flag on this sweep, hand-checked against its cited result: call right if the item is "
            "wrong as typed, false_flag if it holds; note why. Committed before the report is read."
        ),
        "sweep": sweep,
        "checker": checker_commit,
        "flags": {
            run: {f["id"]: {"verdict": f["verdict"], "reason": f["reason"], "item": f["item"], "call": "", "note": ""} for f in fl}
            for run, fl in flags.items()
            if fl
        },
    }


def flag_problems(calls: dict[str, Any], sweep: str, flags: dict[str, dict[str, str]]) -> list[str]:
    """What's wrong with a hand check of these flags (run -> {id: verdict}); [] if it calls every one."""
    out = []
    if calls.get("sweep") != sweep:
        out.append(f"it checks sweep {calls.get('sweep')!r}, not {sweep}")
    got = calls.get("flags") or {}
    for run, by_id in flags.items():
        for item_id, verdict in by_id.items():
            entry = (got.get(run) or {}).get(item_id)
            if entry is None:
                out.append(f"{run} {item_id}: flagged {verdict}, not checked")
            elif entry.get("verdict") != verdict:
                out.append(f"{run} {item_id}: checked as {entry.get('verdict')}, but the checker now says {verdict}")
            elif entry.get("call") not in FLAG_CALLS:
                out.append(f"{run} {item_id}: call {entry.get('call')!r} isn't right or false_flag")
    for run, by_id in got.items():
        for item_id in by_id:
            if item_id not in flags.get(run, {}):
                out.append(f"{run} {item_id}: checked, but not a flag")
    return out


# --- calibration: two labelers' files compared by ID (R8) -----------------------


@dataclass
class Comparison:
    runs: list[str]
    diagnoses: list[tuple[str, str, str]]  # run, mine, theirs
    agree: list[tuple[Label, Label]]  # same target, complaint, and kind
    other_kind: list[tuple[Label, Label]]  # same target and complaint, strict on one side, loose on the other
    only_mine: list[Label]
    only_theirs: list[Label]

    @property
    def diagnoses_agree(self) -> int:
        return sum(m == t for _, m, t in self.diagnoses)


def compare(mine: dict[str, Any], theirs: dict[str, Any]) -> Comparison:
    """Mine against theirs on the runs they labeled, label by label: the same target and complaint is the same slip."""
    runs = [run for run in theirs["runs"] if run in mine["runs"]]
    key = {(lb.run, lb.target, lb.complaint): lb for lb in labels(mine) if lb.run in runs}
    their = {(lb.run, lb.target, lb.complaint): lb for lb in labels(theirs) if lb.run in runs}
    both = [k for k in key if k in their]
    return Comparison(
        runs=runs,
        diagnoses=[(run, mine["runs"][run]["diagnosis"], theirs["runs"][run]["diagnosis"]) for run in runs],
        agree=[(key[k], their[k]) for k in both if key[k].kind == their[k].kind],
        other_kind=[(key[k], their[k]) for k in both if key[k].kind != their[k].kind],
        only_mine=[key[k] for k in key if k not in their],
        only_theirs=[their[k] for k in their if k not in key],
    )


def render_comparison(c: Comparison, mine: str = "mine", theirs: str = "theirs") -> str:
    lines = []
    for run, m, t in c.diagnoses:
        lines.append(f"{run}: diagnosis {mine} {m}, {theirs} {t}" + ("" if m == t else "  <- differs"))

    def row(lb: Label) -> str:
        return f"{lb.run} {lb.target} {lb.kind} {lb.complaint}: {lb.note}"

    for title, rows in (
        ("agree", [row(a) for a, _ in c.agree]),
        ("strict on one side, loose on the other", [f"{row(a)} | {theirs}: {b.kind}, {b.note}" for a, b in c.other_kind]),
        (f"only {mine}", [row(lb) for lb in c.only_mine]),
        (f"only {theirs}", [row(lb) for lb in c.only_theirs]),
    ):
        lines.append(f"\n{title} ({len(rows)}):")
        lines += [f"  {r}" for r in rows] or ["  none"]
    union = len(c.agree) + len(c.other_kind) + len(c.only_mine) + len(c.only_theirs)
    lines.append(
        f"\n{len(c.runs)} runs; diagnoses agree on {c.diagnoses_agree} of {len(c.runs)}; "
        f"slips: {len(c.agree)} of {union} agree on target, complaint, and kind; {len(c.other_kind)} differ only in kind"
    )
    return "\n".join(lines)
