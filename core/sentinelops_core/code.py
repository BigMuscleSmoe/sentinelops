"""Which of checkout-api's files may be read or patched, and how a read looks.

Only the service's own modules under checkout/ (BUILD_PLAN.md, Phase 4 design,
decision 1): never handler.py, sim/, controller/, traffic/, tools/, tests/, or
anything outside services/checkout-api/. The snapshot builder, the read tool,
the agent's patch check, and the remediation flow all take the rule from here,
so what can be read and what can be patched can't drift apart.

A path passes only exactly as given. One that would pass once cleaned up (./,
//, .., a backslash, upper case, %) is refused, not repaired: nothing here
builds a filesystem path from what the model wrote.

A read shows the file with each line numbered as cat -n prints it (the number,
a tab, the line), under a first line naming the file and the release. It shows
no commit: deploy history's commit IDs are its own (decision 5).
"""

import hashlib
import re
from dataclasses import dataclass

# Where checkout/ sits in the repo, for a diff's paths and the GitHub API's.
SERVICE_ROOT = "services/checkout-api"
READ_TOOL = "read_repo_file"
MAX_PATH_CHARS = 100

_READABLE = re.compile(r"checkout/[a-z_][a-z0-9_]*\.py", re.ASCII)
_VERSION = r"\d+\.\d+\.\d+"
_HEADER = re.compile(rf"(checkout/[a-z_][a-z0-9_]*\.py) at ({_VERSION}): (\d+) lines?", re.ASCII)
_NUMBERED = re.compile(r" *([1-9]\d*)\t(.*)", re.ASCII | re.DOTALL)


def readable(path: object) -> bool:
    """True only for a path, exactly as given, to one of checkout/'s own modules."""
    return isinstance(path, str) and len(path) <= MAX_PATH_CHARS and _READABLE.fullmatch(path) is not None


@dataclass(frozen=True)
class FileRead:
    """One whole file as a read showed it."""

    version: str
    path: str
    text: str  # the file's own text: LF line endings, ending in a newline unless empty


def render_file(version: str, path: str, text: str) -> str:
    """A read of one whole file: its name and release, then every line numbered from 1."""
    if not readable(path):
        raise ValueError(f"{path!r} isn't one of checkout/'s modules")
    if not re.fullmatch(_VERSION, version):
        raise ValueError(f"{version!r} isn't a release number")
    if "\r" in text or (text and not text.endswith("\n")):
        raise ValueError("a file must use LF line endings and end in a newline")
    lines = text.split("\n")[:-1] if text else []
    head = f"{path} at {version}: {len(lines)} line{'' if len(lines) == 1 else 's'}"
    return "\n".join([head, *(f"{n:>6}\t{line}" for n, line in enumerate(lines, 1))])


def parse_file(text: str) -> FileRead | None:
    """The file a read showed, or None if it isn't a whole file: a listing, an error, or lines missing."""
    rows = text.split("\n")
    header = _HEADER.fullmatch(rows[0])
    if header is None:
        return None
    path, version, count = header[1], header[2], int(header[3])
    body = rows[1:] if count else [r for r in rows[1:] if r]
    if len(body) != count:
        return None
    lines = []
    for expected, row in enumerate(body, 1):
        numbered = _NUMBERED.fullmatch(row)
        if numbered is None or int(numbered[1]) != expected:
            return None
        lines.append(numbered[2])
    return FileRead(version, path, "".join(line + "\n" for line in lines))


def digest(text: str) -> str:
    """The SHA-256 of a file's text, as the remediation flow hashes the same file on GitHub."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
