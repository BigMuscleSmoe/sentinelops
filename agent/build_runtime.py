"""Stage the agent's package for AgentCore Runtime, for Terraform to zip and deploy.

    uv run python build_runtime.py        # from agent/, before any plan; CI runs it before its apply

The Runtime runs direct code on arm64 Linux with Python 3.12: our modules,
prompts/, and sentinelops_tools/ at the root, beside every locked dependency
installed from arm64 wheels. Terraform's archive_file zips the staged folder,
infra/.build/agent-runtime/, when it plans, so a plan needs this run first.

The same lock and the same uv give the same bytes on any machine, so a plan
here and CI's apply hash the same zip. For that, uv's bin/ scripts (their
shebangs name this machine's Python), the RECORD files (which hash those
scripts), and uv's .lock are left out, and our own text files are copied with
LF line endings.
"""

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

AGENT = Path(__file__).resolve().parent
REPO = AGENT.parent
TOOLS_PACKAGE = REPO / "tools" / "sentinelops_tools"
OUT = REPO / "infra" / ".build" / "agent-runtime"
REQUIREMENTS = OUT.parent / "agent-runtime-requirements.txt"
PLATFORM = "aarch64-manylinux2014"
PYTHON = "3.12"
ENTRY = "app.py"
# Tooling that runs on this machine, not in the Runtime.
NOT_SHIPPED = {"build_runtime.py", "gateway_tools.py"}
TEXT_SUFFIXES = {".py", ".md", ".json", ".txt"}
# The Runtime's limit for direct code, unzipped.
MAX_UNZIPPED_BYTES = 750 * 1024 * 1024


def ours() -> dict[str, Path]:
    """What we ship by top-level name: the agent's modules, its prompts, and the tools package."""
    shipped = {path.stem: path for path in sorted(AGENT.glob("*.py")) if path.name not in NOT_SHIPPED}
    shipped["prompts"] = AGENT / "prompts"
    shipped["sentinelops_tools"] = TOOLS_PACKAGE
    return shipped


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
    for name, source in ours().items():
        if source.is_file():
            copy_source(source, out / source.name)
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
    subprocess.run(["uv", *args], cwd=AGENT, check=True)


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
    # The tools package comes from its folder, not as a dependency: it's a path
    # dependency, and --only-binary can't build it.
    uv(
        "export", "--frozen", "--no-dev", "--no-emit-project", "--no-emit-package", "sentinelops-tools",
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

    clashes = sorted(set(ours()) & top_level_names(OUT))
    if clashes:
        sys.exit(f"A dependency installs a top-level name of ours, which one import would shadow: {clashes}")
    copy_ours(OUT)
    if not (OUT / ENTRY).is_file() or not (OUT / "prompts" / "system.md").is_file():
        sys.exit(f"The package lacks {ENTRY} or prompts/system.md.")

    digest, count, size = tree_digest(OUT)
    if size > MAX_UNZIPPED_BYTES:
        sys.exit(f"{size / 2**20:.0f} MB unzipped, past the Runtime's {MAX_UNZIPPED_BYTES / 2**20:.0f} MB.")
    print(f"Staged {OUT.relative_to(REPO).as_posix()}: {count} files, {size / 2**20:.1f} MB, sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
