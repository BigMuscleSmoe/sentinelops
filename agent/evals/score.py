"""Grade a sweep once: the frozen grader and the rubrics on every scored run, scored against a hand audit.

Fixed before any sweep ran, so neither the runs nor the audit could shape it:

- The grader is the one frozen at FROZEN_GRADER: grading refuses if agent/grader
  or the tools' package (whose parsers the grader reads through) differs from it.
- The audit must be committed before grading, and must cover every scored run,
  with or without slips, so an unaudited run can't pass as a clean one.
- A grader finding matches an audit label when both are on the same part of the
  Hypothesis (`where`) and their texts overlap: one contains the other, or they
  share a distinctive token (a time, a number of two or more characters or with a
  decimal point, or an identifier with an underscore or a digit).
- Recall is strict slips caught (a flagged finding matches) over strict slips
  labeled. A flagged finding no label matches goes to a re-check by hand, filed
  apart (`recheck`) as a false flag or an audit miss, and precision waits for it.
  Labels are never edited after grading: an audit miss is reported as one.
- The unparsed share sits beside the flags, so vagueness can't pass as accuracy.
"""

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals.rubrics import ascii_text
from evals.rubrics import score as rubric_score
from evals.scenarios import BY_ID
from grader import Grade, grade
from grader.checks import Finding, Verdict

FROZEN_GRADER = "57455ab"
FROZEN_PATHS = ("agent/grader", "tools/sentinelops_tools")
REPO = Path(__file__).resolve().parents[2]
_TOKEN = re.compile(r"\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?|\d[\d,]*(?:\.\d+)?|[a-z0-9_.]*[a-z_][a-z0-9_.]*")
_SPACE = re.compile(r"\s+")


class Refusal(Exception):
    """Grading can't go ahead: the result wouldn't be a fair measure."""


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=False)


def check_frozen(commit: str = FROZEN_GRADER) -> None:
    if _git("diff", "--quiet", commit, "--", *FROZEN_PATHS).returncode != 0:
        raise Refusal(f"the grader differs from {commit} ({', '.join(FROZEN_PATHS)}): grade with it as frozen")
    extra = _git("ls-files", "--others", "--exclude-standard", "--", *FROZEN_PATHS).stdout.split()
    if extra:
        raise Refusal(f"untracked files beside the frozen grader: {', '.join(extra)}")


def check_committed(path: Path) -> str:
    """The commit that last changed the audit; refuses if it isn't committed as it stands."""
    rel = path.resolve().relative_to(REPO).as_posix()
    if _git("ls-files", "--error-unmatch", rel).returncode != 0:
        raise Refusal(f"{rel} isn't committed: commit the audit before grading")
    if _git("diff", "--quiet", "HEAD", "--", rel).returncode != 0:
        raise Refusal(f"{rel} has changes since its commit")
    return _git("log", "-1", "--format=%H", "--", rel).stdout.strip()


def _norm(text: str) -> str:
    return _SPACE.sub(" ", ascii_text(text).lower().replace("'", "").replace('"', "")).strip()


def distinctive(text: str) -> set[str]:
    out = set()
    for token in _TOKEN.findall(_norm(text)):
        token = token.strip(".,")
        if ":" in token or ("_" in token) or (any(c.isdigit() for c in token) and any(c.isalpha() for c in token)):
            out.add(token)
        elif token.replace(",", "").replace(".", "").isdigit() and (len(token) >= 2 or "." in token):
            out.add(token.replace(",", ""))
    return out


def matches(finding: Finding, label: dict[str, Any]) -> bool:
    if finding.where != label["where"]:
        return False
    a, b = _norm(finding.text), _norm(label["quote"])
    return a in b or b in a or bool(distinctive(a) & distinctive(b))


@dataclass
class RunScore:
    run: str
    scenario: str
    seen: bool
    rubric: dict[str, bool]
    diagnosis: bool
    confidently_wrong: bool
    audit_diagnosis: str
    claims: int
    flagged: int
    loose: int
    unparsed: int
    strict_labels: int
    strict_caught: int
    loose_labels: int
    loose_graded_loose: int
    unmatched_flags: list[dict[str, str]] = field(default_factory=list)


def score_run(transcript: dict[str, Any], audit: dict[str, Any], graded: Grade | None = None) -> RunScore:
    meta = transcript["eval"]
    scenario = BY_ID[meta["scenario"]]
    g = graded or grade(transcript)
    rubric = rubric_score(scenario, transcript["hypothesis"])
    labels = audit.get("slips", [])
    strict = [lb for lb in labels if lb["kind"] == "strict"]
    loose = [lb for lb in labels if lb["kind"] == "loose"]
    flagged = g.flagged
    unmatched = [f for f in flagged if not any(matches(f, lb) for lb in labels)]
    return RunScore(
        run=meta["run"],
        scenario=scenario.id,
        seen=scenario.seen,
        rubric={k: v for k, v in rubric.items.items() if k != "pr_gate_value"},
        diagnosis=rubric.diagnosis,
        confidently_wrong=rubric.confidently_wrong,
        audit_diagnosis=audit.get("diagnosis", ""),
        claims=len(g.findings),
        flagged=len(flagged),
        loose=len(g.loose),
        unparsed=len(g.unparsed),
        strict_labels=len(strict),
        strict_caught=sum(any(matches(f, lb) for f in flagged) for lb in strict),
        loose_labels=len(loose),
        loose_graded_loose=sum(
            any(f.verdict == Verdict.LOOSE and matches(f, lb) for f in g.findings) for lb in loose
        ),
        unmatched_flags=[{"where": f.where, "text": f.text, "check": str(f.check), "detail": f.detail} for f in unmatched],
    )


def summarize(scores: list[RunScore], recheck: dict[str, list[dict[str, str]]] | None) -> dict[str, Any]:
    def group(rows: list[RunScore]) -> dict[str, Any]:
        if not rows:
            return {"runs": 0}
        flagged = sum(r.flagged for r in rows)
        unmatched = sum(len(r.unmatched_flags) for r in rows)
        out: dict[str, Any] = {
            "runs": len(rows),
            "diagnosis_right": sum(r.diagnosis for r in rows),
            "confidently_wrong": sum(r.confidently_wrong for r in rows),
            "rubric": {k: sum(r.rubric[k] for r in rows) for k in rows[0].rubric},
            "rubric_agrees_with_audit": sum(r.diagnosis == (r.audit_diagnosis == "right") for r in rows),
            "strict_labeled": sum(r.strict_labels for r in rows),
            "strict_caught": sum(r.strict_caught for r in rows),
            "loose_labeled": sum(r.loose_labels for r in rows),
            "loose_graded_loose": sum(r.loose_graded_loose for r in rows),
            "claims": sum(r.claims for r in rows),
            "flagged": flagged,
            "unmatched_flags": unmatched,
            "unparsed": sum(r.unparsed for r in rows),
        }
        out["unparsed_share"] = round(out["unparsed"] / max(1, out["claims"] + out["unparsed"]), 3)
        if recheck is not None:
            calls = [c["call"] for r in rows for c in recheck.get(r.run, [])]
            out["false_flags"] = calls.count("false_flag")
            out["audit_misses"] = calls.count("audit_miss")
            out["recheck_missing"] = unmatched - len(calls)
        return out

    return {
        "unseen": group([s for s in scores if not s.seen]),
        "seen": group([s for s in scores if s.seen]),
        "all": group(scores),
    }
