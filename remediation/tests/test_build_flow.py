"""The flow's package holds the flow, core, and the commit hooks' scanner, and nothing of the tooling here."""

import ast
import sys
import tomllib
from pathlib import Path

import build_flow

PROJECT = Path(__file__).resolve().parents[1]
# Third-party imports the shipped code may make: the SDK, the boto3 it pins, and core's pydantic.
LOCKED = {"aws_durable_execution_sdk_python", "boto3", "botocore", "pydantic"}


def staged(tmp_path: Path) -> Path:
    out = tmp_path / "remediation"
    build_flow.copy_ours(out)
    return out


def test_the_package_ships_the_flow_core_and_the_scanner_only(tmp_path: Path) -> None:
    out = staged(tmp_path)
    assert sorted(p.name for p in out.iterdir()) == ["commit_scan.py", "handler.py", "sentinelops_core", "sentinelops_remediation"]
    shipped = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
    assert not any("__pycache__" in name or name.startswith("tests/") for name in shipped)
    for tooling in ("check_app.py", "record_responses.py", "build_flow.py", "check_markdown.py", "uv.lock", "pyproject.toml"):
        assert tooling not in shipped
    assert (out / "commit_scan.py").read_bytes() == (PROJECT.parent / ".githooks" / "commit_scan.py").read_bytes().replace(b"\r\n", b"\n")


def test_our_text_ships_with_lf_line_endings(tmp_path: Path) -> None:
    out = staged(tmp_path)
    assert not [p.name for p in out.rglob("*.py") if b"\r" in p.read_bytes()]


def test_every_import_the_package_makes_is_shipped_stdlib_or_locked(tmp_path: Path) -> None:
    out = staged(tmp_path)
    ours = {name.removesuffix(".py") for name in build_flow.SHIPPED}
    imported = set()
    for path in out.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
    outside = imported - ours - set(sys.stdlib_module_names)
    assert outside <= LOCKED, outside - LOCKED


def test_the_runtime_dependencies_are_the_sdk_and_core() -> None:
    project = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["dependencies"] == ["aws-durable-execution-sdk-python==3.1.0", "sentinelops-core"]
    assert project["requires-python"] == ">=3.13,<3.14" and build_flow.PYTHON == "3.13"
