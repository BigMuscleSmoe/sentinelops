"""checkout-api's code at a release, as the read tool shows it.

The code comes from a snapshot built at deploy from each release's commit
(tools/build_code_snapshot.py) and shipped in the read tool's zip: the
function holds no GitHub credential and reads nothing at run time but this
dict. A path is checked by sentinelops_core.code's rule and then looked up by
exact key, so no filesystem path is ever built from what the model wrote.

Every path the tool doesn't show gets the same answer, whether or not such a
file exists anywhere, so the tool can't be used to probe the tree.
"""

import json
import re
from collections.abc import Mapping
from pathlib import Path

from sentinelops_core import code

# The snapshot's file in the read tool's zip, named by its function's environment.
CODE_SNAPSHOT_ENV = "SENTINELOPS_CODE_SNAPSHOT"
FORMAT = 1

Snapshot = Mapping[str, Mapping[str, str]]  # release -> path -> the file's text

NO_CODE = "read_repo_file has no code to read here."
NO_RELEASE = "read_repo_file keeps no code for that release."


def no_file(version: str) -> str:
    return (
        f"read_repo_file has no such file at {version}. Name one exactly as a stack frame names it "
        "(checkout/<module>.py), or leave path out to list the release's files."
    )


def read(snapshot: Snapshot | None, version: object, path: object = None) -> str:
    """One whole file of `version`, its lines numbered; or, with no path, the release's files."""
    if snapshot is None:
        return NO_CODE
    if not isinstance(version, str) or version not in snapshot:
        return NO_RELEASE
    files = snapshot[version]
    if path is None:
        return listing(version, files)
    if not isinstance(path, str) or not code.readable(path) or path not in files:
        return no_file(version)
    return code.render_file(version, path, files[path])


def listing(version: str, files: Mapping[str, str]) -> str:
    rows = [f"checkout-api {version}: {len(files)} files"]
    for path in sorted(files):
        lines = files[path].count("\n")
        rows.append(f"{path} ({lines} line{'' if lines == 1 else 's'})")
    return "\n".join(rows)


def parse(document: object) -> dict[str, dict[str, str]]:
    """The snapshot's files by release, from its JSON; ValueError on anything the build wouldn't write."""
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise ValueError(f"not a code snapshot of format {FORMAT}")
    releases = document.get("releases")
    if not isinstance(releases, dict) or not releases:
        raise ValueError("a snapshot holds at least one release")
    snapshot: dict[str, dict[str, str]] = {}
    for version, release in releases.items():
        if not re.fullmatch(r"\d+\.\d+\.\d+", version, re.ASCII) or not isinstance(release, dict):
            raise ValueError(f"{version!r} isn't a release")
        files = release.get("files")
        if not isinstance(files, dict) or not files:
            raise ValueError(f"{version} has no files")
        for path, text in files.items():
            if not code.readable(path) or not isinstance(text, str):
                raise ValueError(f"{version}: {path!r} isn't one of checkout/'s modules")
        snapshot[version] = dict(files)
    return snapshot


def load(path: Path) -> dict[str, dict[str, str]]:
    return parse(json.loads(path.read_text(encoding="utf-8")))
