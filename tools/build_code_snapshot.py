"""Build checkout-api's code snapshot and the tool functions' packages, before a plan.

The read tool shows checkout-api's code as each release ran it. The code comes
from here, built at deploy from each release's commit (sentinelops_core.releases),
never from the working directory: git's tree for checkout/ at that commit, by
sentinelops_core.code's rule, so a file the rule doesn't name is never read.
A file the rule names must also be a regular file (no symlink), 100 KB at most,
ASCII with LF line endings and a last newline, Python that parses, and import
nothing outside checkout and the standard library. Anything else fails the
build, every problem listed, before anything is written.

It writes two folders in infra/.build/, each one Lambda zip's whole content:
- tools-package/: the tools' package and sentinelops_core's code module, for
  the four tools that read AWS.
- code-reader/: the same, plus code_snapshot.json, for read_repo_file.
Line endings are made LF as files are copied, so Windows and CI stage the same
bytes; the last line printed is the snapshot's SHA-256, to compare.

Run from tools/, before a plan: uv run --locked python build_code_snapshot.py.
The release commits must be in the clone: CI checks out with fetch-depth 0.
"""

import ast
import hashlib
import json
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from sentinelops_core import code
from sentinelops_core.releases import RELEASES, Release
from sentinelops_tools.repo import FORMAT

REPO = Path(__file__).resolve().parent.parent
TOOLS_PACKAGE = REPO / "tools" / "sentinelops_tools"
CORE_PACKAGE = REPO / "core" / "sentinelops_core"
# The only modules of sentinelops_core a function needs, both stdlib only: never
# models.py, whose pydantic isn't in the Lambda runtime.
CORE_SHIPPED = ("__init__.py", "code.py")
BUILD = REPO / "infra" / ".build"
TOOLS_OUT = BUILD / "tools-package"
READER_OUT = BUILD / "code-reader"
SNAPSHOT_FILE = "code_snapshot.json"  # the read tool's function names it in its environment
MAX_FILE_BYTES = 100 * 1024
REGULAR_FILE = "100644"
ALLOWED_IMPORT_ROOTS = frozenset({"checkout"}) | sys.stdlib_module_names


class BuildError(Exception):
    """A release whose code the read tool mustn't show. Its message lists every problem found."""


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True).stdout


def release_files(repo: Path, commit: str) -> tuple[dict[str, str], list[str]]:
    """checkout/'s files at `commit`, by path, and every problem found in them."""
    try:
        git(repo, "cat-file", "-e", f"{commit}^{{commit}}")
    except subprocess.CalledProcessError:
        return {}, [f"commit {commit[:12]} isn't in this clone (CI checks out with fetch-depth 0)"]
    prefix = f"{code.SERVICE_ROOT}/"
    listed = git(repo, "ls-tree", "-r", "-z", "--full-tree", commit, "--", f"{prefix}checkout/")
    files: dict[str, str] = {}
    problems: list[str] = []
    for record in filter(None, listed.split(b"\0")):
        meta, _, raw_path = record.partition(b"\t")
        mode, kind, sha = meta.decode("ascii").split()
        path = raw_path.decode("utf-8", "replace").removeprefix(prefix)
        if not code.readable(path):
            continue  # the rule decides what's read: a file it doesn't name is never opened
        if mode != REGULAR_FILE or kind != "blob":
            problems.append(f"{path}: not a regular file (mode {mode})")
            continue
        if int(git(repo, "cat-file", "-s", sha)) > MAX_FILE_BYTES:
            problems.append(f"{path}: over {MAX_FILE_BYTES // 1024} KB")
            continue
        data = git(repo, "cat-file", "blob", sha)
        found = text_problems(path, data)
        if found:
            problems.extend(found)
            continue
        files[path] = data.decode("ascii")
    if not files and not problems:
        problems.append(f"commit {commit[:12]} has no files under checkout/")
    return files, problems


def text_problems(path: str, data: bytes) -> list[str]:
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        return [f"{path}: not ASCII"]
    if "\r" in text:
        return [f"{path}: has a CR"]
    if text and not text.endswith("\n"):
        return [f"{path}: no newline at the end"]
    try:
        tree = ast.parse(text, filename=path, feature_version=(3, 12))
    except SyntaxError as e:
        return [f"{path}: doesn't parse as Python 3.12 (line {e.lineno})"]
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        else:
            continue  # a relative import stays inside checkout/
        for name in names:
            if name.split(".")[0] not in ALLOWED_IMPORT_ROOTS:
                problems.append(f"{path}: imports {name}, outside checkout and the standard library")
    return problems


def build_snapshot(repo: Path = REPO, releases: Mapping[str, Release] = RELEASES) -> dict:
    """Every release's code, as the read tool's snapshot. BuildError lists every problem."""
    built: dict[str, dict] = {}
    problems: list[str] = []
    for version, release in sorted(releases.items()):
        files, found = release_files(repo, release.commit)
        problems.extend(f"{version}: {problem}" for problem in found)
        built[version] = {"commit": release.commit, "files": files}
    if problems:
        raise BuildError("\n".join(problems))
    return {"format": FORMAT, "releases": built}


def snapshot_bytes(snapshot: dict) -> bytes:
    return (json.dumps(snapshot, indent=1, sort_keys=True) + "\n").encode("ascii")


def stage(snapshot: dict, build: Path = BUILD) -> str:
    """Write both packages afresh; returns the snapshot's SHA-256."""
    tools_out, reader_out = build / TOOLS_OUT.name, build / READER_OUT.name
    for out in (tools_out, reader_out):
        if out.exists():
            shutil.rmtree(out)
        _copy_package(TOOLS_PACKAGE, sorted(p.name for p in TOOLS_PACKAGE.glob("*.py")), out / TOOLS_PACKAGE.name)
        _copy_package(CORE_PACKAGE, CORE_SHIPPED, out / CORE_PACKAGE.name)
    data = snapshot_bytes(snapshot)
    (reader_out / SNAPSHOT_FILE).write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _copy_package(source: Path, names: list[str] | tuple[str, ...], out: Path) -> None:
    out.mkdir(parents=True)
    for name in names:
        (out / name).write_bytes((source / name).read_bytes().replace(b"\r\n", b"\n"))


def main() -> int:
    try:
        snapshot = build_snapshot()
    except BuildError as e:
        print(f"The code snapshot can't be built:\n{e}", file=sys.stderr)
        return 1
    digest = stage(snapshot)
    for version, release in snapshot["releases"].items():
        print(f"{version} ({release['commit'][:12]}): {len(release['files'])} files", file=sys.stderr)
    print(f"staged {TOOLS_OUT.relative_to(REPO).as_posix()} and {READER_OUT.relative_to(REPO).as_posix()}", file=sys.stderr)
    print(f"snapshot sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
