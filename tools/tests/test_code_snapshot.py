"""The code snapshot: each release's checkout/ from its own commit, checked, and nothing else.

The real releases are built from this clone, whose history must hold both
commits (CI checks out with fetch-depth 0). The builder's refusals are tested
on trees made with git's plumbing, which can hold a symlink on any OS.
"""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import build_code_snapshot as build
from sentinelops_core import code
from sentinelops_core.releases import RELEASES, Release
from sentinelops_tools import repo

REPO = build.REPO
SERVICE = REPO / code.SERVICE_ROOT
GOOD = b'"""Fine."""\n\nimport json\nfrom collections.abc import Mapping\n\nfrom checkout import db\nfrom . import config\n'


@pytest.fixture(scope="module")
def snapshot() -> dict:
    return build.build_snapshot()


def files(snapshot: dict, version: str) -> dict[str, str]:
    return snapshot["releases"][version]["files"]


def null_check() -> str:
    """sim/releases.py's NULL_CHECK, read from its source: what the runtime removes to run 1.43.0."""
    tree = ast.parse((SERVICE / "sim" / "releases.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and [t.id for t in node.targets if isinstance(t, ast.Name)] == ["NULL_CHECK"]:
            return ast.literal_eval(node.value)
    raise AssertionError("sim/releases.py has no NULL_CHECK")


def test_1_42_0_is_mains_checkout_exactly_as_it_runs(snapshot: dict) -> None:
    tracked = build.git(REPO, "ls-files", "-z", "--", f"{code.SERVICE_ROOT}/checkout/").decode().split("\0")
    running = {
        path: (REPO / code.SERVICE_ROOT / path).read_text(encoding="utf-8")  # LF, whatever the checkout's
        for path in (p.removeprefix(f"{code.SERVICE_ROOT}/") for p in tracked if p)
        if code.readable(path)
    }

    assert files(snapshot, "1.42.0") == running, "checkout/ changed on main: that's a new release (sentinelops_core.releases)"


def test_1_43_0_is_1_42_0_without_exactly_the_null_check_the_runtime_removes(snapshot: dict) -> None:
    healthy, bad = files(snapshot, "1.42.0"), files(snapshot, "1.43.0")

    assert healthy["checkout/pricing.py"].count(null_check()) == 1
    assert bad == {**healthy, "checkout/pricing.py": healthy["checkout/pricing.py"].replace(null_check(), "", 1)}


def test_1_43_0s_commit_is_one_commit_on_1_42_0s_changing_only_pricing() -> None:
    healthy, bad = RELEASES["1.42.0"].commit, RELEASES["1.43.0"].commit

    assert build.git(REPO, "rev-parse", f"{bad}^").decode().strip() == healthy
    changed = build.git(REPO, "diff", "--name-only", healthy, bad).decode().split()
    assert changed == [f"{code.SERVICE_ROOT}/checkout/pricing.py"]


def test_a_fix_goes_only_to_a_release_branch_never_main() -> None:
    assert RELEASES["1.42.0"].branch is None and RELEASES["1.42.0"].ref == "refs/tags/v1.42.0"
    assert RELEASES["1.43.0"].branch == "release/1.43.0"
    assert RELEASES["1.43.0"].ref == f"refs/heads/{RELEASES['1.43.0'].branch}"
    for release in RELEASES.values():
        assert release.branch is None or release.branch.startswith("release/")
        assert len(release.commit) == 40


def test_the_snapshot_holds_only_files_the_rule_names(snapshot: dict) -> None:
    assert repo.parse(snapshot) == {v: r["files"] for v, r in snapshot["releases"].items()}
    assert all(path.startswith("checkout/") for r in snapshot["releases"].values() for path in r["files"])


# The builder on trees made to break it.


def commit(tmp_path: Path, entries: dict[str, tuple[str, bytes]], untracked: dict[str, bytes] | None = None) -> str:
    """A commit of exactly these entries (path: mode, content), made without touching the working tree."""
    env = {**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test", "GIT_COMMITTER_NAME": "test",
           "GIT_COMMITTER_EMAIL": "test", "GIT_INDEX_FILE": str(tmp_path / "index")}
    if not (tmp_path / ".git").exists():
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)

    def run(*args: str, data: bytes | None = None) -> str:
        return subprocess.run(["git", "-C", str(tmp_path), *args], input=data, capture_output=True, check=True, env=env).stdout.decode().strip()

    for path, (mode, content) in entries.items():
        blob = run("hash-object", "-w", "--stdin", data=content)
        run("update-index", "--add", "--cacheinfo", f"{mode},{blob},{code.SERVICE_ROOT}/{path}")
    tree = run("write-tree")
    for path, content in (untracked or {}).items():
        target = tmp_path / code.SERVICE_ROOT / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return run("commit-tree", tree, "-m", "test")


def test_only_checkouts_own_modules_are_taken_from_the_tree_and_nothing_from_the_disk(tmp_path: Path) -> None:
    sha = commit(
        tmp_path,
        {
            "checkout/app.py": ("100644", GOOD),
            "checkout/notes.md": ("100644", b"# notes\n"),
            "checkout/sub/deep.py": ("100644", GOOD),
            "handler.py": ("100644", b"import sim\n"),
            "sim/flags.py": ("100644", b"FAULT = 1\n"),
            "controller/faults.py": ("100644", b"x = 1\n"),
        },
        untracked={"checkout/extra.py": GOOD, "checkout/pricing.py": GOOD},
    )

    assert build.release_files(tmp_path, sha) == ({"checkout/app.py": GOOD.decode()}, [])


@pytest.mark.parametrize(
    ("mode", "content", "problem"),
    [
        ("120000", b"../sim/flags.py", "checkout/bad.py: not a regular file (mode 120000)"),
        ("100755", GOOD, "checkout/bad.py: not a regular file (mode 100755)"),
        ("100644", b"x = 1\n" * 20000, "checkout/bad.py: over 100 KB"),
        ("100644", b'NAME = "caf\xc3\xa9"\n', "checkout/bad.py: not ASCII"),
        ("100644", b"x = 1\r\n", "checkout/bad.py: has a CR"),
        ("100644", b"x = 1", "checkout/bad.py: no newline at the end"),
        ("100644", b"def broken(:\n", "checkout/bad.py: doesn't parse as Python 3.12 (line 1)"),
        ("100644", b"from sim.flags import FlagReader\n", "checkout/bad.py: imports sim.flags, outside checkout and the standard library"),
        ("100644", b"import boto3\n", "checkout/bad.py: imports boto3, outside checkout and the standard library"),
        ("100644", b"def f():\n    import controller.faults\n", "checkout/bad.py: imports controller.faults, outside checkout and the standard library"),
    ],
    ids=["symlink", "executable", "too big", "non-ASCII", "CR", "no last newline", "unparseable", "sim", "boto3", "nested"],
)
def test_a_file_the_rule_names_must_pass_every_check(tmp_path: Path, mode: str, content: bytes, problem: str) -> None:
    sha = commit(tmp_path, {"checkout/app.py": ("100644", GOOD), "checkout/bad.py": (mode, content)})

    assert build.release_files(tmp_path, sha) == ({"checkout/app.py": GOOD.decode()}, [problem])


def test_every_problem_is_listed_and_nothing_is_built(tmp_path: Path) -> None:
    sha = commit(tmp_path, {"checkout/a.py": ("120000", b"x"), "checkout/b.py": ("100644", b"import sim\n")})
    releases = {"1.42.0": Release("1.42.0", sha, "refs/tags/v1.42.0", None), "9.9.9": Release("9.9.9", "f" * 40, "x", None)}

    with pytest.raises(build.BuildError) as raised:
        build.build_snapshot(tmp_path, releases)

    assert str(raised.value).splitlines() == [
        "1.42.0: checkout/a.py: not a regular file (mode 120000)",
        "1.42.0: checkout/b.py: imports sim, outside checkout and the standard library",
        "9.9.9: commit ffffffffffff isn't in this clone (CI checks out with fetch-depth 0)",
    ]


def test_a_release_with_no_checkout_files_is_refused(tmp_path: Path) -> None:
    sha = commit(tmp_path, {"sim/flags.py": ("100644", b"x = 1\n")})

    assert build.release_files(tmp_path, sha) == ({}, [f"commit {sha[:12]} has no files under checkout/"])


# The packages the functions' zips are made from.


def test_each_zip_holds_exactly_the_package_the_rule_module_and_for_the_reader_the_snapshot(tmp_path: Path, snapshot: dict) -> None:
    build.stage(snapshot, tmp_path)
    tools = sorted(f"sentinelops_tools/{p.name}" for p in build.TOOLS_PACKAGE.glob("*.py"))
    shipped = sorted([*tools, "sentinelops_core/__init__.py", "sentinelops_core/code.py"])

    def listed(folder: Path) -> list[str]:
        return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())

    assert listed(tmp_path / "tools-package") == shipped
    assert listed(tmp_path / "code-reader") == sorted([*shipped, "code_snapshot.json"])
    assert repo.load(tmp_path / "code-reader" / "code_snapshot.json") == repo.parse(snapshot)
    for path in shipped:
        assert b"\r" not in (tmp_path / "code-reader" / path).read_bytes()


def test_the_reader_package_runs_without_pydantic_and_reads_its_own_snapshot(tmp_path: Path, snapshot: dict) -> None:
    build.stage(snapshot, tmp_path)
    folder = tmp_path / "code-reader"
    script = (
        "import json, os, sys\n"
        "sys.modules['pydantic'] = None\n"  # absent in the Lambda runtime: importing it would fail
        f"sys.path.insert(0, {str(folder)!r})\n"
        "os.environ['SENTINELOPS_CODE_SNAPSHOT'] = 'code_snapshot.json'\n"
        "os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'\n"  # Lambda sets it; boto3 wants one for its clients
        "from sentinelops_tools import handlers\n"
        "assert handlers.__file__.startswith(sys.path[0]), handlers.__file__\n"
        "assert 'sentinelops_core.models' not in sys.modules\n"
        "env = handlers._clients_once()\n"
        "print(json.dumps(handlers.handle('read_repo_file', {'version': '1.43.0', 'path': 'checkout/pricing.py'}, None, env=env)))\n"
    )
    out = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, check=True).stdout

    assert json.loads(out)["text"] == repo.read(repo.parse(snapshot), "1.43.0", "checkout/pricing.py")
