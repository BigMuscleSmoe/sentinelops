"""Check saved runs' typed evidence: uv run python -m checker <transcript.json> [...] [--json]

Or the planted errors' recall on them: uv run python -m checker --plant <transcript.json> [...]
Or a sweep's trial marks: uv run python -m checker --trial scratch/evals/<sweep>

No model, no AWS. For each run (BUILD_PLAN.md, "The grader redesign"): what
each evidence item points at and whether it holds there; the share of claims
typed rather than left as inferences, and of items whose citations can be
followed; the figures in root_cause no item states (counted, not flagged); the
inferences resting on a flagged item; and the answers trimmed.
"""

import argparse
import gzip
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from checker.checks import ItemCheck, Verdict, check_resolved
from checker.figures import uncited
from checker.resolve import Resolution, Status, resolve_all
from checker.sources import read_sources


@dataclass
class RunCheck:
    run: str
    halted: bool
    evidence: int
    inferences: int
    items: list[Resolution] = field(default_factory=list)  # where each item points
    checks: list[ItemCheck] = field(default_factory=list)  # whether it holds there
    uncited: list[str] = field(default_factory=list)  # root_cause's figures no item states
    shaky: list[str] = field(default_factory=list)  # inferences resting on a flagged item
    trims: int = 0

    def count(self, status: Status) -> int:
        return sum(item.status is status for item in self.items)

    def verdicts(self, verdict: Verdict) -> int:
        return sum(check.verdict is verdict for check in self.checks)

    @property
    def flagged(self) -> list[ItemCheck]:
        return [check for check in self.checks if check.flagged]

    @property
    def unchecked(self) -> int:
        """Fields stated beyond what an item's kind uses, which no check reads."""
        return sum(len(check.unchecked) for check in self.checks)

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
        trims=len(transcript.get("trims") or []),
    )
    if halted:  # a halted run's answer is the guard's: nothing in it is the model's claim
        return check
    run = read_sources(transcript)
    check.items = resolve_all(hypothesis, run)
    evidence = hypothesis.get("evidence") or []
    check.checks = [check_resolved(item, r, run) for item, r in zip(evidence, check.items, strict=True)]
    check.uncited = uncited(hypothesis)
    by_id = {c.id: c for c in check.checks}
    for i, inference in enumerate(hypothesis.get("inferences") or [], 1):
        weak = [f"{e} ({by_id[e].verdict.value})" for e in inference.get("rests_on") or [] if e in by_id and by_id[e].flagged]
        missing = [e for e in inference.get("rests_on") or [] if e not in by_id]
        if weak or missing:
            check.shaky.append(f"i{i} rests on " + ", ".join(weak + [f"{e} (no such item)" for e in missing]))
    return check


def render(checks: list[RunCheck]) -> str:
    out = []
    for check in checks:
        if check.halted:
            out.append(f"{check.run}: halted, nothing to check")
            continue
        typed = "-" if check.typed_share is None else f"{check.typed_share:.0%}"
        followed = "-" if check.resolved_share is None else f"{check.resolved_share:.0%}"
        out.append(
            f"{check.run}: {check.evidence} evidence, {check.inferences} inferences (typed {typed}, "
            f"citations followed {followed}); {_verdicts(check.checks)}; "
            f"{len(check.uncited)} uncited figures in root_cause; {check.trims} trims"
        )
        for item in check.checks:
            if item.verdict is not Verdict.HOLDS:
                out.append(f"  {item.id} {item.kind} {item.source or '-'}: {item.verdict.value}: {item.reason}")
        if check.uncited:
            out.append(f"  uncited: {', '.join(check.uncited)}")
        out.extend(f"  {line}" for line in check.shaky)
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
    out.append(
        f"checks: {_verdicts([i for c in answered for i in c.checks])}; "
        f"{sum(len(c.uncited) for c in answered)} uncited figures; "
        f"{sum(c.unchecked for c in answered)} fields stated beyond their kinds, unchecked; "
        f"{sum(len(c.shaky) for c in answered)} inferences on a flagged item; {sum(c.trims for c in answered)} trims"
    )
    return "\n".join(out)


def _verdicts(checks: list[ItemCheck]) -> str:
    def n(v: Verdict) -> int:
        return sum(c.verdict is v for c in checks)

    flagged = sum(c.flagged for c in checks)
    parts = f"{n(Verdict.HOLDS)} hold, {n(Verdict.LOOSE)} loose, {flagged} flagged"
    if flagged:
        parts += (
            f" ({n(Verdict.CONTRADICTED)} contradicted, {n(Verdict.OUTSIDE_READ)} outside the read, "
            f"{n(Verdict.UNRESOLVED)} unresolved)"
        )
    untyped = n(Verdict.UNTYPED)
    return parts + (f", {untyped} untyped" if untyped else "")


def plant_report(paths: list[Path]) -> tuple[str, bool]:
    from checker.plant import plant_run, recall

    planted, unplanted = [], []
    for path in paths:
        transcript = load(path)
        hypothesis = transcript.get("hypothesis") or {}
        if hypothesis.get("halted_by"):
            continue
        found, none = plant_run(path.stem, hypothesis, read_sources(transcript))
        planted += found
        unplanted += none
    result = recall(planted)

    def line(name: str, share: dict[str, int]) -> str:
        caught, n = share["caught"], share["planted"]
        return f"  {name}: {caught} of {n}" + (f" ({caught / n:.0%})" if n else "")

    out = [line("planted errors caught", result["all"]).strip(), "by kind:"]
    out += [line(k, v) for k, v in result["by_kind"].items()]
    out.append("by mutation:")
    out += [line(k, v) for k, v in result["by_mutation"].items()]
    out += [f"missed: {m}" for m in result["missed"]]
    out.append(f"items that hold with no mutant: {len(unplanted)}" + (f" ({', '.join(unplanted)})" if unplanted else ""))
    return "\n".join(out), not result["missed"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="checker", description=__doc__.splitlines()[0])
    parser.add_argument("transcripts", nargs="*", type=Path)
    parser.add_argument("--json", action="store_true", help="print each run's items as JSON")
    parser.add_argument("--plant", action="store_true", help="plant errors in the items that hold: the checker's recall")
    parser.add_argument("--trial", type=Path, help="a sweep's folder: the typed-evidence trial's pass marks")
    args = parser.parse_args(argv)
    if args.trial:
        from checker.trial import render as render_trial, trial

        result = trial(args.trial)
        print(render_trial(result))
        return 0 if result.passed else 1
    if not args.transcripts:
        parser.error("give transcripts, or --trial and a sweep's folder")
    if args.plant:
        text, caught_all = plant_report(args.transcripts)
        print(text)
        return 0 if caught_all else 1
    checks = [check_run(path.stem, load(path)) for path in args.transcripts]
    if args.json:
        rows = [
            {
                "run": c.run, "halted": c.halted, "evidence": c.evidence, "inferences": c.inferences,
                "uncited": c.uncited, "shaky": c.shaky, "trims": c.trims, "unchecked": c.unchecked,
                "typed_share": c.typed_share,
                "resolved_share": c.resolved_share,
                "items": [i.row() for i in c.checks],
            }
            for c in checks
        ]
        print(json.dumps(rows, indent=1, default=str))
    else:
        print(render(checks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
