"""Repo-wide rules, enforced as tests so drift fails the build instead of a review.

Scans every file git knows about (tracked or new and not ignored), so code in
remediation/, tools/, or trigger/ is covered the day it's added.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

ASCII_SUFFIXES = {".py", ".tf", ".yml", ".yaml", ".toml"}
ASCII_DIRS = ("agent/prompts/",)

# The one place allowed to decide whether a PR may be opened.
PR_GATE_HOME = "agent/models.py"
PR_GATE_PATTERNS = [
    re.compile(r"confidence\s*(<=?|>=?)"),
    re.compile(r"(<=?|>=?)\s*[\w.]*confidence\b"),
    re.compile(r"\bPR_CONFIDENCE_THRESHOLD\b"),
    re.compile(r"\bhalted_by\s+is\b"),
]


def repo_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [p for p in out.splitlines() if (REPO / p).is_file()]


def is_test_file(path: str) -> bool:
    return "/tests/" in f"/{path}" or Path(path).name.startswith("test_")


def test_source_files_are_ascii_only() -> None:
    offenders = []
    for path in repo_files():
        if Path(path).suffix not in ASCII_SUFFIXES and not path.startswith(ASCII_DIRS):
            continue
        text = (REPO / path).read_bytes().decode("utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            bad = sorted({c for c in line if ord(c) > 127})
            if bad:
                offenders.append(f"{path}:{lineno}: {' '.join(f'U+{ord(c):04X}' for c in bad)}")

    assert not offenders, "Non-ASCII characters in source files:\n" + "\n".join(offenders)


def test_pr_gate_is_only_decided_by_can_open_pr() -> None:
    offenders = []
    for path in repo_files():
        if not path.endswith(".py") or path == PR_GATE_HOME or is_test_file(path):
            continue
        text = (REPO / path).read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if any(p.search(line) for p in PR_GATE_PATTERNS):
                offenders.append(f"{path}:{lineno}: {line.strip()}")

    assert not offenders, (
        "PR-gate logic outside Hypothesis.can_open_pr. Check `hypothesis.can_open_pr` "
        "instead, or extend it in models.py:\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize(
    "line",
    [
        "if hypothesis.confidence >= 0.7:",
        "if h.confidence < THRESHOLD:",
        "if 0.7 <= payload.confidence:",
        "ok = conf > PR_CONFIDENCE_THRESHOLD",
        "if hypothesis.halted_by is None and ...",
    ],
)
def test_pr_gate_patterns_catch_re_derived_rules(line: str) -> None:
    assert any(p.search(line) for p in PR_GATE_PATTERNS)


@pytest.mark.parametrize(
    "line",
    [
        "if not hypothesis.can_open_pr:",
        'confidence=0.0,',
        "confidence: float = Field(",
    ],
)
def test_pr_gate_patterns_allow_normal_use(line: str) -> None:
    assert not any(p.search(line) for p in PR_GATE_PATTERNS)
