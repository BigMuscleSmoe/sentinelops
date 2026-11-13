"""Grade a run's Hypothesis against its own tool output, in code: no model, no AWS.

Three layers (Phase 3 design, `BUILD_PLAN.md`): parse each tool result back into
data (`view`, through the parsers beside the renderers), extract checkable
claims by rule (`claims`), and check each one (`checks`). A claim no rule can
take counts as unparsed, beside the flags, so vagueness can't hide.

This package is local tooling and the eval harness's: the Runtime package ships
only the agent's top-level modules, so it never ships.
"""

from dataclasses import dataclass, field
from typing import Any

from grader.checks import Finding, Verdict, check
from grader.claims import Unparsed, extract
from grader.view import RunView, view_of


@dataclass
class Grade:
    findings: list[Finding] = field(default_factory=list)
    unparsed: list[Unparsed] = field(default_factory=list)
    unread: list[str] = field(default_factory=list)

    @property
    def flagged(self) -> list[Finding]:
        return [f for f in self.findings if f.flagged]

    @property
    def loose(self) -> list[Finding]:
        return [f for f in self.findings if f.verdict == Verdict.LOOSE]

    def count(self, verdict: Verdict) -> int:
        return sum(f.verdict == verdict for f in self.findings)


def statements(hypothesis: dict[str, Any]) -> list[tuple[str, str]]:
    """Where each piece of a Hypothesis is, and its text."""
    out = [("root_cause", hypothesis.get("root_cause") or "")]
    out += [(f"evidence[{i}]", text) for i, text in enumerate(hypothesis.get("evidence") or [])]
    if hypothesis.get("affected_component"):
        out.append(("affected_component", hypothesis["affected_component"]))
    return out


def grade(transcript: dict[str, Any], run: RunView | None = None) -> Grade:
    """Every checkable claim in the transcript's Hypothesis, checked against the transcript's tool results."""
    run = run or view_of(transcript)
    result = Grade(unread=list(run.unread))
    hypothesis = transcript.get("hypothesis") or {}
    for where, text in statements(hypothesis):
        claims, unparsed = extract(where, text)
        result.unparsed += unparsed
        for claim in claims:
            finding = check(claim, run)
            if finding is not None:
                result.findings.append(finding)
    return result
