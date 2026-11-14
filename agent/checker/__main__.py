"""Resolve saved runs' typed evidence: uv run python -m checker <transcript.json> [...] [--json]

Or a sweep's trial marks: uv run python -m checker --trial scratch/evals/<sweep>

No model, no AWS. For each run: what each evidence item points at, or why it
points at nothing, and the trial's counts (BUILD_PLAN.md, "The grader
redesign"): the share of claims typed rather than left as inferences, and the
share of items whose citations resolve.
"""

import argparse
import gzip
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from checker.resolve import Resolution, Status, resolve_all
from checker.sources import read_sources


@dataclass
class RunCheck:
    run: str
    halted: bool
    evidence: int
    inferences: int
    items: list[Resolution] = field(default_factory=list)

    def count(self, status: Status) -> int:
        return sum(item.status is status for item in self.items)

    @property
    def typed(self) -> int:
        return self.evidence - self.count(Status.UNTYPED)

    @property
    def typed_share(self) -> float | None:
        """Typed items over all evidence items and inferences; None when the run answered neither."""
        total = self.evidence + self.inferences
        return self.typed / total if total else None

    @property
    def resolved_share(self) -> float | None:
        """Items whose citations can be followed (outside_read included) over all typed items."""
        if not self.typed:
            return None
        return (self.count(Status.RESOLVED) + self.count(Status.OUTSIDE_READ)) / self.typed


def load(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    return json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)


def check_run(name: str, transcript: dict[str, Any]) -> RunCheck:
    hypothesis = transcript.get("hypothesis") or {}
    halted = bool(hypothesis.get("halted_by"))
    check = RunCheck(
        name,
        halted,
        0 if halted else len(hypothesis.get("evidence") or []),
        0 if halted else len(hypothesis.get("inferences") or []),
    )
    if not halted:  # a halted run's answer is the guard's: nothing in it is the model's claim
        check.items = resolve_all(hypothesis, read_sources(transcript))
    return check


def render(checks: list[RunCheck]) -> str:
    out = []
    for check in checks:
        if check.halted:
            out.append(f"{check.run}: halted, nothing to check")
            continue
        typed = "-" if check.typed_share is None else f"{check.typed_share:.0%}"
        resolved = "-" if check.resolved_share is None else f"{check.resolved_share:.0%}"
        out.append(
            f"{check.run}: {check.evidence} evidence, {check.inferences} inferences (typed {typed}); "
            f"{check.count(Status.RESOLVED)} resolved, {check.count(Status.OUTSIDE_READ)} outside the read, "
            f"{check.count(Status.UNRESOLVED)} unresolved (citations followed {resolved})"
        )
        for item in check.items:
            if item.status is not Status.RESOLVED:
                out.append(f"  {item.id} {item.kind} {item.source or '-'}: {item.status.value}: {item.reason}")
    answered = [c for c in checks if not c.halted]
    evidence = sum(c.evidence for c in answered)
    inferences = sum(c.inferences for c in answered)
    typed_items = sum(c.typed for c in answered)
    followed = sum(c.count(Status.RESOLVED) + c.count(Status.OUTSIDE_READ) for c in answered)
    out.append(
        f"total: {len(answered)} of {len(checks)} runs answered; {evidence} evidence, {inferences} inferences"
        + (f" (typed {typed_items / (evidence + inferences):.0%})" if evidence + inferences else "")
        + f"; {followed} of {typed_items} typed items' citations followed"
        + (f" ({followed / typed_items:.0%})" if typed_items else "")
        + f", {sum(c.count(Status.OUTSIDE_READ) for c in answered)} outside the read"
    )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="checker", description=__doc__.splitlines()[0])
    parser.add_argument("transcripts", nargs="*", type=Path)
    parser.add_argument("--json", action="store_true", help="print each run's items as JSON")
    parser.add_argument("--trial", type=Path, help="a sweep's folder: the typed-evidence trial's pass marks")
    args = parser.parse_args(argv)
    if args.trial:
        from checker.trial import render as render_trial, trial

        result = trial(args.trial)
        print(render_trial(result))
        return 0 if result.passed else 1
    if not args.transcripts:
        parser.error("give transcripts, or --trial and a sweep's folder")
    checks = [check_run(path.stem, load(path)) for path in args.transcripts]
    if args.json:
        rows = [
            {**asdict(c), "typed_share": c.typed_share, "resolved_share": c.resolved_share} for c in checks
        ]
        print(json.dumps(rows, indent=1, default=str))
    else:
        print(render(checks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
