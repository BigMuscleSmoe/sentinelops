"""The repo's state, as the scorer and the sweep read it: what's committed, and at which commit.

A file something is scored against (an audit, a flag check, the pass bar) must
be committed as it stands, so it can't move to fit a result. A sweep records the
commit it ran on, so a test sweep can be held to the freeze (BUILD_PLAN.md,
"The grader redesign").
"""

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
# What a sweep's runs and their grading depend on: dirty here means a sweep ran on uncommitted code.
WATCHED = ("agent", "tools")


class Refusal(Exception):
    """Scoring can't go ahead: the result wouldn't be a fair measure."""


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=False)


def check_committed(path: Path) -> str:
    """The commit that last changed the file; refuses if it isn't committed as it stands."""
    try:
        rel = path.resolve().relative_to(REPO).as_posix()
    except ValueError:
        raise Refusal(f"{path} isn't in the repo, so it isn't committed") from None
    if git("ls-files", "--error-unmatch", rel).returncode != 0:
        raise Refusal(f"{rel} isn't committed: commit it before scoring against it")
    if git("diff", "--quiet", "HEAD", "--", rel).returncode != 0:
        raise Refusal(f"{rel} has changes since its commit")
    return git("log", "-1", "--format=%H", "--", rel).stdout.strip()


def head() -> str | None:
    out = git("rev-parse", "HEAD")
    return out.stdout.strip() if out.returncode == 0 else None


def dirty(paths: tuple[str, ...] = WATCHED) -> list[str]:
    """Changed or untracked files under paths (gitignored ones, such as scratch/, aren't counted)."""
    out = git("status", "--porcelain", "--untracked-files=all", "--", *paths)
    return [line[3:] for line in out.stdout.splitlines() if line.strip()]


def unchanged(since: str, paths: list[str], until: str | None = None) -> bool:
    """True if paths are the same at until (or in the working tree) as at since. False if either commit is missing."""
    args = ["diff", "--quiet", since] + ([until] if until else []) + ["--", *paths]
    return git(*args).returncode == 0
