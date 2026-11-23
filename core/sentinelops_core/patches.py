"""A proposed fix, checked as text and syntax and never run: the same checks in the agent's run and in the remediation flow.

A patch is one to three exact-text edits to one file the agent read (BUILD_PLAN.md,
Phase 4 design, decision 6): each replaces a passage that occurs exactly once,
and code renders the diff, since models write exact replacements far more
reliably than unified diffs. Nothing in a patch is imported, executed, or
tested here; its tests run in GitHub Actions on the PR. compile() turns the
file into bytecode to find errors ast.parse doesn't, and runs none of it.

Refused says why, in a few words, for the run's log and the evals' counts.
"""

import ast
import difflib
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from sentinelops_core.code import SERVICE_ROOT, readable

MAX_EDITS = 3
MAX_CHANGED_LINES = 40
MAX_NEW_CHARS = 4_000
# checkout-api runs on Lambda's Python 3.12, so a patch must parse as 3.12 wherever it's checked.
SERVICE_PYTHON = (3, 12)


class Refused(ValueError):
    """Why a patch can't be used."""


@dataclass(frozen=True)
class Checked:
    text: str  # the file with the edits made
    diff: str  # unified, with paths from the repo's root


def apply_edits(path: str, original: str, edits: Sequence[tuple[str, str]]) -> Checked:
    """The file with each (old, new) edit made, and its diff, if the patch passes every check; else Refused."""
    if not readable(path):
        raise Refused("a file that can't be patched")
    if not edits:
        raise Refused("no edits")
    if len(edits) > MAX_EDITS:
        raise Refused(f"{len(edits)} edits, past {MAX_EDITS}")
    spans = []
    for i, (old, new) in enumerate(edits, 1):
        spans.append((_span(i, original, old, new), new))
    spans.sort(key=lambda span: span[0][0])
    for (before, _), (after, _) in zip(spans, spans[1:]):
        if after[0] < before[1]:
            raise Refused("edits overlap")
    if sum(len(new) for _, new in spans) > MAX_NEW_CHARS:
        raise Refused(f"more than {MAX_NEW_CHARS} characters of new text")

    text = original
    for (start, end), new in reversed(spans):
        text = text[:start] + new + text[end:]
    if original.endswith("\n") and not text.endswith("\n"):
        raise Refused("drops the file's last newline")
    _parses(path, text)
    try:
        imported = Counter(_imports(original))
    except SyntaxError:
        raise Refused("the file as read doesn't parse") from None
    if Counter(_imports(text)) - imported:
        raise Refused("adds an import")

    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            text.splitlines(keepends=True),
            fromfile=f"a/{SERVICE_ROOT}/{path}",
            tofile=f"b/{SERVICE_ROOT}/{path}",
        )
    )
    changed = sum(
        1 for line in diff.splitlines() if line[:1] in "+-" and not line.startswith(("+++ ", "--- "))
    )
    if changed > MAX_CHANGED_LINES:
        raise Refused(f"{changed} changed lines, past {MAX_CHANGED_LINES}")
    return Checked(text, diff)


def _span(i: int, original: str, old: str, new: str) -> tuple[int, int]:
    """Where edit i's old text sits in the file: it must be there exactly once."""
    if not old:
        raise Refused(f"edit {i}: old is empty")
    if old == new:
        raise Refused(f"edit {i}: changes nothing")
    if "\r" in old or "\r" in new:
        raise Refused(f"edit {i}: holds a carriage return")
    if not new.isascii():
        raise Refused(f"edit {i}: new text isn't ASCII")
    start = original.find(old)
    if start < 0:
        raise Refused(f"edit {i}: old text isn't in the file")
    if original.find(old, start + 1) >= 0:
        raise Refused(f"edit {i}: old text is in the file more than once")
    return start, start + len(old)


def _parses(path: str, text: str) -> None:
    try:
        ast.parse(text, filename=path, feature_version=SERVICE_PYTHON)
    except SyntaxError:
        raise Refused("doesn't parse") from None
    try:
        compile(text, path, "exec", dont_inherit=True)
    except (SyntaxError, ValueError):
        raise Refused("doesn't compile") from None


def _imports(text: str) -> list[tuple[object, ...]]:
    """Every import in the file, anywhere in it, as what it binds."""
    found: list[tuple[object, ...]] = []
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            found += [("import", alias.name, alias.asname) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            found += [("from", node.module, node.level, alias.name, alias.asname) for alias in node.names]
    return found
