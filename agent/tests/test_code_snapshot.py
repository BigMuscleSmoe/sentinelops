"""code_snapshot: a run here reads each release's code as the deploy builds it, from the release commits in this clone.

The builder's own refusals are tools/tests/test_code_snapshot.py's; these hold
the agent's side to the same bytes and the same refusal. Git only: no AWS, no model.
"""

import hashlib
import subprocess

import pytest

import code_snapshot
from answers import PRICING_1_42_0, PRICING_1_43_0
from code_snapshot import CodeSnapshotError
from knowledge_base import script
from sentinelops_core.releases import RELEASES
from sentinelops_tools import repo


def test_a_run_here_reads_each_release_as_the_deploy_builds_it() -> None:
    built = code_snapshot.build()
    builder = script("build_code_snapshot")
    document = builder.build_snapshot()

    assert built.files == repo.parse(document)  # as the read tool's function parses its shipped file
    assert built.digest == hashlib.sha256(builder.snapshot_bytes(document)).hexdigest()  # the deploy's printed digest
    assert set(built.files) == set(RELEASES)
    assert built.files["1.42.0"]["checkout/pricing.py"] == PRICING_1_42_0
    assert built.files["1.43.0"]["checkout/pricing.py"] == PRICING_1_43_0


def test_a_release_the_builder_refuses_refuses_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    builder = script("build_code_snapshot")
    missing = "isn't in this clone (CI checks out with fetch-depth 0)"
    monkeypatch.setattr(builder, "release_files", lambda repo, commit: ({}, [f"commit {commit[:12]} {missing}"]))

    with pytest.raises(CodeSnapshotError) as refused:
        code_snapshot.build()

    assert str(refused.value) == (
        "checkout-api's code snapshot can't be built from this clone:\n"
        f"1.42.0: commit 3fb990f5c865 {missing}\n1.43.0: commit ee9ef23a8d7e {missing}"
    )


def test_git_failing_refuses_the_run_without_its_output(monkeypatch: pytest.MonkeyPatch) -> None:
    builder = script("build_code_snapshot")
    real = builder.git

    def broken(repo: object, *args: str) -> bytes:
        if args[0] == "ls-tree":  # the commit is there, and listing its tree fails
            raise subprocess.CalledProcessError(128, ["git", *args], stderr=b"fatal: bad object")
        return real(repo, *args)

    monkeypatch.setattr(builder, "git", broken)

    with pytest.raises(CodeSnapshotError, match=r"^checkout-api's code snapshot can't be built: git failed \(CalledProcessError\)$"):
        code_snapshot.build()
