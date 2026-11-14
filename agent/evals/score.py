"""Scoring a sweep against a hand audit: rebuilt on the checker in the grader redesign's step 5.

The old scorer graded the prose grader's findings against audit labels by
matching their texts; the prose grader is retired (R6), so its scores are
reproducible only at 57455ab. Step 5's scorer matches the checker's flags to
labels by evidence item ID and holds them to the pass bar committed before the
test sweep (R7: 85% precision, 70% strict recall).

What stays from the old one: an audit must be committed, as it stands, before
anything scores against it, so labels can't move to fit a result.
"""

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RETIRED = (
    "the prose grader is retired (BUILD_PLAN.md, \"The grader redesign\", R6) and its scorer with it; "
    "step 5 rebuilds scoring on the checker. Old grades are reproducible at 57455ab"
)


class Refusal(Exception):
    """Scoring can't go ahead: the result wouldn't be a fair measure."""


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=False)


def check_committed(path: Path) -> str:
    """The commit that last changed the audit; refuses if it isn't committed as it stands."""
    rel = path.resolve().relative_to(REPO).as_posix()
    if _git("ls-files", "--error-unmatch", rel).returncode != 0:
        raise Refusal(f"{rel} isn't committed: commit the audit before grading")
    if _git("diff", "--quiet", "HEAD", "--", rel).returncode != 0:
        raise Refusal(f"{rel} has changes since its commit")
    return _git("log", "-1", "--format=%H", "--", rel).stdout.strip()
