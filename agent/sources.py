"""Each tool result as a source the model can cite: a label first, and numbered log lines.

Typed evidence cites a result by its label (m1, the run's first metrics read;
l2, its second log read; d1, its first deploy read) and log lines by number,
so code can check each item against exactly what it points at (BUILD_PLAN.md,
"The grader redesign", R1). The tools' own text is unchanged: the marks go on
here, the same for local tools and Gateway's, and `unmark` takes them off, so
the tools' parsers read their own text back.

Log lines get numbers because timestamps alone can repeat across a read.
"""

import functools
import inspect
import re
from collections import Counter
from collections.abc import Callable
from typing import Any, cast

PREFIXES = {"get_metrics": "m", "get_recent_logs": "l", "get_deploy_history": "d"}
ALARM = "alarm"
_LABEL = re.compile(r"\[source ([a-z])([1-9]\d*)\]$")
_NUMBERED = re.compile(r"\[([1-9]\d*)\] (\{.*)$")


class Sources:
    """The run's count of results by kind, so each gets the next label."""

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()

    def mark(self, tool: str, text: str) -> str:
        prefix = PREFIXES[tool]
        self._counts[prefix] += 1
        body = number_lines(text) if prefix == PREFIXES["get_recent_logs"] else text
        return f"[source {prefix}{self._counts[prefix]}]\n{body}"


def number_lines(text: str) -> str:
    """Number each log line, a line of the result that starts with '{', from 1."""
    out, n = [], 0
    for line in text.split("\n"):
        if line.startswith("{"):
            n += 1
            line = f"[{n}] {line}"
        out.append(line)
    return "\n".join(out)


def unmark(text: str) -> tuple[str | None, str, dict[int, int]]:
    """A marked result's label, its text as the tool wrote it, and each line number's index among its log lines.

    The label is None for a result with none: one from before labels, or a tool's error.
    """
    lines = text.split("\n")
    label = None
    if lines and (found := _LABEL.match(lines[0])):
        label = f"{found[1]}{found[2]}"
        lines = lines[1:]
    numbers: dict[int, int] = {}
    out = []
    for line in lines:
        if found := _NUMBERED.match(line):
            numbers[int(found[1])] = len(numbers)
            line = found[2]
        out.append(line)
    return label, "\n".join(out), numbers


def cited[F: Callable[..., Any]](sources: Sources, fn: F) -> F:
    """Wrap a tool so its result is marked as the next source of its kind.

    A tool with no prefix is refused here, before any run, rather than at its first call.
    """
    name = fn.__name__
    if name not in PREFIXES:
        raise ValueError(f"{name} has no source prefix: add one to sources.PREFIXES")

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> str:
            return sources.mark(name, await fn(*args, **kwargs))

        return cast(F, async_wrapper)

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        return sources.mark(name, fn(*args, **kwargs))

    return cast(F, wrapper)
