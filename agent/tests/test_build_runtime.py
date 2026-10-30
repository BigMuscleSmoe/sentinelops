"""build_runtime: what the Runtime's package ships, checked without building it (no network)."""

import ast
from pathlib import Path

import build_runtime
from build_runtime import AGENT, ENTRY, NOT_SHIPPED, TOOLS_PACKAGE, copy_source, ours, top_level_names


def imported_modules(path: Path) -> set[str]:
    """Every top-level name a module imports, including imports inside functions."""
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_every_agent_module_the_entry_reaches_is_shipped_and_no_tooling() -> None:
    shipped = ours()
    reached, todo = set(), [ENTRY.removesuffix(".py")]
    while todo:
        name = todo.pop()
        if name in reached or not (AGENT / f"{name}.py").is_file():
            continue
        reached.add(name)
        todo.extend(imported_modules(AGENT / f"{name}.py"))

    assert {"app", "investigation", "main", "gateway", "runner"} <= reached
    assert reached <= set(shipped)
    assert not {Path(name).stem for name in NOT_SHIPPED} & set(shipped)
    assert shipped["sentinelops_tools"] == TOOLS_PACKAGE
    assert (shipped["prompts"] / "system.md").is_file()


def test_our_text_files_are_copied_with_lf_and_others_as_they_are(tmp_path: Path) -> None:
    (tmp_path / "module.py").write_bytes(b"a = 1\r\nb = 2\r\n")
    (tmp_path / "blob.bin").write_bytes(b"\r\n\x00")

    copy_source(tmp_path / "module.py", tmp_path / "out" / "module.py")
    copy_source(tmp_path / "blob.bin", tmp_path / "out" / "blob.bin")

    assert (tmp_path / "out" / "module.py").read_bytes() == b"a = 1\nb = 2\n"
    assert (tmp_path / "out" / "blob.bin").read_bytes() == b"\r\n\x00"


def test_a_dependency_is_known_by_the_name_it_imports_as(tmp_path: Path) -> None:
    (tmp_path / "package").mkdir()
    (tmp_path / "module.py").touch()
    (tmp_path / "_native.cpython-312-aarch64-linux-gnu.so").touch()
    (tmp_path / "package-1.0.dist-info").mkdir()

    assert top_level_names(tmp_path) == {"package", "module", "_native"}


def test_the_digest_covers_each_files_path_and_bytes(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_bytes(b"x")
    first = build_runtime.tree_digest(tmp_path)
    (tmp_path / "a.py").write_bytes(b"y")
    changed = build_runtime.tree_digest(tmp_path)
    (tmp_path / "a.py").rename(tmp_path / "b.py")
    moved = build_runtime.tree_digest(tmp_path)

    assert first[1:] == changed[1:] == moved[1:] == (1, 1)
    assert len({first[0], changed[0], moved[0]}) == 3
