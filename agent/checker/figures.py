"""Figures in root_cause that no evidence item states (BUILD_PLAN.md, "The grader redesign", step 4).

A time, a version, or a number in root_cause should come from an evidence item,
where the checker can test it. One that appears in none is reported as uncited:
its own count at first, not a flag, since a figure can be right and still uncited
(the alarm's own threshold, say).

A figure appears in an item when one of the item's stated fields (value, low,
high, count, seconds, at, start, end) holds it: a time by its clock, from the
hour down to what root_cause gives ("18:56" for 18:56:06.039); a version
exactly; a number within half a step of its last digit, as the checks read
numbers. The note isn't read: the schema keeps figures out of it.
"""

import re
from dataclasses import dataclass
from typing import Any

_ASCII = {0x2192: "->", 0x2013: "-", 0x2014: "-", 0x2212: "-"}
_ISO = re.compile(r"\d{4}-\d\d-\d\dT(\d\d:\d\d(?::\d\d(?:\.\d+)?)?)Z?")
_CLOCK = re.compile(r"(?<![\w:.])(\d{1,2}:\d\d(?::\d\d(?:\.\d+)?)?)(?![\w:])")
_VERSION = re.compile(r"(?<![\w.])\d+\.\d+\.\d+(?!\.?\d)(?![A-Za-z_])")
_NUMBER = re.compile(r"(?<![\w.:/])(\d[\d,]*(?:\.\d+)?)(?:\s?(?:%|ms|s)(?![A-Za-z]))?(?!\.?\d)(?![A-Za-z_])")
STATED = ("value", "low", "high", "count", "seconds", "at", "start", "end")


@dataclass(frozen=True)
class Figures:
    times: tuple[str, ...]  # clocks, HH:MM[:SS[.fff]]
    versions: tuple[str, ...]
    numbers: tuple[str, ...]  # as written, commas dropped


def figures(text: str) -> Figures:
    """The times, versions, and numbers a text states, each once, in order."""
    text = str(text).translate(_ASCII)
    times = [m[1] for m in _ISO.finditer(text)]
    text = _ISO.sub(" ", text)
    times += [m[1] for m in _CLOCK.finditer(text)]
    text = _CLOCK.sub(" ", text)
    versions = _VERSION.findall(text)
    text = _VERSION.sub(" ", text)
    numbers = [m[1].replace(",", "") for m in _NUMBER.finditer(text)]
    return Figures(_once(_clock(t) for t in times), _once(versions), _once(numbers))


def uncited(hypothesis: dict[str, Any]) -> list[str]:
    """root_cause's figures that no typed evidence item states, as root_cause writes them."""
    stated = [figures(str(item[k])) for item in hypothesis.get("evidence") or [] if isinstance(item, dict)
              for k in STATED if item.get(k) not in (None, "", [])]
    times = {t for f in stated for t in f.times}
    versions = {v for f in stated for v in f.versions}
    numbers = [float(n) for f in stated for n in f.numbers]
    cause = figures(hypothesis.get("root_cause") or "")
    out = [t for t in cause.times if not any(s.startswith(t) for s in times)]
    out += [v for v in cause.versions if v not in versions]
    for n in cause.numbers:
        value, half = float(n), 0.5 * 10.0 ** -(len(n.split(".")[1]) if "." in n else 0)
        if not any(abs(value - s) <= half + 1e-9 for s in numbers):
            out.append(n)
    return out


def _clock(text: str) -> str:
    return text.zfill(len(text) + 1) if text.index(":") == 1 else text


def _once(items: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))
