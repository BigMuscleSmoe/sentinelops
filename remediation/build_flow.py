"""Stage the remediation flow's Lambda package, for Terraform to zip and deploy.

    uv run python build_flow.py        # from remediation/, before any plan; CI runs it before its apply

The function runs on arm64 Linux with Python 3.13 (durable functions need
3.13 or later): handler.py, sentinelops_remediation/, sentinelops_core/, and
the commit hooks' commit_scan.py at the root, beside every locked dependency
(the durable execution SDK, the boto3 it pins, pydantic) from arm64 wheels.
Bundling boto3 means the flow never depends on the runtime's version.
Terraform's archive_file zips the staged folder, infra/.build/remediation/,
when it plans, so a plan needs this run first.

Built as agent/build_runtime.py builds the Runtime's package, for the same
reason: the same lock and the same uv give the same bytes on any machine, so
a plan here and CI's apply hash the same zip.
"""

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
REPO = PROJECT.parent
OUT = REPO / "infra" / ".build" / "remediation"
REQUIREMENTS = OUT.parent / "remediation-requirements.txt"
PLATFORM = "aarch64-manylinux2014"
PYTHON = "3.13"
ENTRY = "handler.py"
# Ours, by the name it has at the package's root.
SHIPPED = {
    "handler.py": PROJECT / "handler.py",
    "sentinelops_remediation": PROJECT / "sentinelops_remediation",
    "sentinelops_core": REPO / "core" / "sentinelops_core",
    # The commit hooks' rules, imported as they are by the scan: one copy.
    "commit_scan.py": REPO / ".githooks" / "commit_scan.py",
}
TEXT_SUFFIXES = {".py", ".md", ".json", ".txt"}
# Lambda's limit for a function's code, unzipped.
MAX_UNZIPPED_BYTES = 250 * 1024 * 1024


def top_level_names(folder: Path) -> set[str]:
    """What a folder offers to import: packages, modules, and extension modules by their bare name."""
    names = set()
    for entry in folder.iterdir():
        if entry.name.endswith((".dist-info", ".data")):
            continue
        names.add(entry.name if entry.is_dir() else entry.name.split(".", 1)[0])
    return names


def copy_source(source: Path, target: Path) -> None:
    """Copy one of our files, with LF line endings if it's text: a checkout here may be CRLF."""
    target.parent.mkdir(parents=True, exist_ok=True)
    data = source.read_bytes()
    if source.suffix in TEXT_SUFFIXES:
        data = data.replace(b"\r\n", b"\n")
    target.write_bytes(data)


def copy_ours(out: Path) -> None:
    for name, source in SHIPPED.items():
        if source.is_file():
            copy_source(source, out / name)
            continue
        for path in sorted(source.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                copy_source(path, out / name / path.relative_to(source))


def tree_digest(folder: Path) -> tuple[str, int, int]:
    """A SHA-256 over every file's path and bytes, in path order, with the file count and total size."""
    digest = hashlib.sha256()
    count = size = 0
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        data = path.read_bytes()
        digest.update(path.relative_to(folder).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(data).digest())
        count += 1
        size += len(data)
    return digest.hexdigest(), count, size


def uv(*args: str) -> None:
    subprocess.run(["uv", *args], cwd=PROJECT, check=True)


def main() -> int:
    if shutil.which("uv") is None:
        sys.exit("uv is not on PATH.")
    # Emptied, not removed: Windows won't remove a folder a shell is in.
    OUT.mkdir(parents=True, exist_ok=True)
    for entry in OUT.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    # sentinelops-core comes from its folder: a path dependency --only-binary can't build.
    uv(
        "export", "--frozen", "--no-dev", "--no-emit-project", "--no-emit-package", "sentinelops-core",
        "--no-header", "--format", "requirements-txt", "--output-file", str(REQUIREMENTS),
    )  # fmt: skip
    # Exactly the locked set, hash-checked: --no-deps resolves nothing past it.
    uv(
        "pip", "install", "--target", str(OUT), "--python-platform", PLATFORM, "--python-version", PYTHON,
        "--only-binary", ":all:", "--no-deps", "--link-mode", "copy", "--requirements", str(REQUIREMENTS),
    )  # fmt: skip
    shutil.rmtree(OUT / "bin", ignore_errors=True)
    for record in OUT.glob("*.dist-info/RECORD"):
        record.unlink()
    (OUT / ".lock").unlink(missing_ok=True)  # uv's, for installs that run at once

    ours = {name.removesuffix(".py") for name in SHIPPED}
    clashes = sorted(ours & top_level_names(OUT))
    if clashes:
        sys.exit(f"A dependency installs a top-level name of ours, which one import would shadow: {clashes}")
    copy_ours(OUT)
    if not (OUT / ENTRY).is_file():
        sys.exit(f"The package lacks {ENTRY}.")

    digest, count, size = tree_digest(OUT)
    if size > MAX_UNZIPPED_BYTES:
        sys.exit(f"{size / 2**20:.0f} MB unzipped, past Lambda's {MAX_UNZIPPED_BYTES / 2**20:.0f} MB.")
    print(f"Staged {OUT.relative_to(REPO).as_posix()}: {count} files, {size / 2**20:.1f} MB, sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
