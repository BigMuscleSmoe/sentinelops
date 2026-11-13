"""The commit scan (.githooks/commit_scan.py): its rules, and that git refuses a commit it flags.

The end-to-end tests make a throwaway repo pointed at the real hooks and run
git commit in it, so a pass means git itself refused the commit. The fakes
that should be caught are built at run time: written out whole, they'd be
caught in this file.
"""

import gzip
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HOOKS = REPO / ".githooks"
_spec = importlib.util.spec_from_file_location("commit_scan", HOOKS / "commit_scan.py")
assert _spec is not None and _spec.loader is not None
scan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scan)

FAKE_ID = "4318" + "2905" + "7716"
OTHER_ID = "5550" + "1234" + "9876"


def rules(text: str, account: str | None = FAKE_ID) -> list[str]:
    return [f.rule for f in scan.scan_text("f", text, account)]


# --- the rules --------------------------------------------------------------------


def test_the_account_id_is_caught_anywhere() -> None:
    assert rules(f"arn:aws:iam::{FAKE_ID}:role/x") == ["account-id"]
    assert rules(f"bucket sentinelops-tfstate-{FAKE_ID}") == ["account-id"]
    # Where the 12-digit rule looks away: inside a longer number, after a dot, beside letters.
    assert rules(f"9{FAKE_ID}1 0.{FAKE_ID} x{FAKE_ID}y") == ["account-id"] * 3


def test_any_other_12_digit_run_is_caught_without_the_account_id() -> None:
    assert rules(f"arn:aws:iam::{OTHER_ID}:role/x", account=None) == ["12-digits"]
    assert rules(f"{OTHER_ID}.dkr.ecr", account=None) == ["12-digits"]


@pytest.mark.parametrize(
    "text",
    [
        "requestId 4f4bb6ac-832d-48bd-b523-" + "8" * 12,  # a UUID's last group
        "p99 0." + "1" * 12,  # a decimal fraction
        "arn:aws:iam::123456789012:role/x",  # AWS's placeholder
        "arn:aws:iam::000011112222:role/x",  # the tests' fake account
        "epoch ms " + "1" * 13,  # longer than 12
        "hex 0a" + "1" * 12 + "ff",  # inside a hex string
    ],
)
def test_what_only_looks_like_an_account_id_passes(text: str) -> None:
    assert rules(text, account=None) == []


def test_emails_are_caught_unless_reserved_for_examples_or_noreply() -> None:
    assert rules("mail " + "someone" + "@" + "gmail.com") == ["email"]
    assert rules("a@example.com b@x.invalid noreply@anthropic.com c@users.noreply.github.com") == []


def test_home_paths_are_caught() -> None:
    for path in ("C:" + "\\Users\\" + "someone\\x", "/c/" + "Users/someone/x", "/home" + "/someone/x"):
        assert rules(path) == ["home-path"], path


def test_public_ips_are_caught_but_private_ones_and_versions_pass() -> None:
    assert rules("from " + ".".join(["8", "8", "4", "4"])) == ["public-ip"]
    assert rules("10.0.0.1 192.168.1.20 127.0.0.1 0.0.0.0 version 1.43.0 and 1.62.0.1.2") == []


def test_keys_are_caught_unless_marked_as_fakes() -> None:
    assert rules("AKIA" + "Q7R2" * 4) == ["aws-key"]
    assert rules("ASIATESTONLYROLEKEY1 AKIAIOSFODNN7EXAMPLE") == []
    assert rules("sk-ant-" + "api03-" + "Zq8" * 30) == ["anthropic-key"]
    assert rules("sk-ant-api03-left-over-from-a-shell") == []


@pytest.mark.parametrize("name", ["terraform.tfstate", "x.tfstate.backup", "dev.tfvars", ".env", ".env.local", "k.pem"])
def test_state_secrets_and_keys_are_caught_by_name(name: str) -> None:
    assert [f.rule for f in scan.scan_blob(f"infra/{name}", b"clean", FAKE_ID)] == ["file-name"]


def test_a_gzip_file_is_scanned_inside() -> None:
    data = gzip.compress(f'{{"arn": "arn:aws:iam::{FAKE_ID}:role/x"}}\n'.encode())
    assert [str(f) for f in scan.scan_blob("w/logs.jsonl.gz", data, FAKE_ID)] == ["w/logs.jsonl.gz (gunzipped):1: account-id"]


def test_a_finding_says_where_and_which_rule_never_the_text() -> None:
    (finding,) = scan.scan_text("a.json", f"one\ntwo {FAKE_ID}\n", FAKE_ID)
    assert str(finding) == "a.json:2: account-id"
    assert FAKE_ID not in str(finding) and FAKE_ID not in repr(finding)


def test_every_tracked_file_passes() -> None:
    """The backstop: a commit made without the hooks still fails CI here. In CI
    there's no account ID configured, and the 12-digit rule stands in for it."""
    try:
        account = scan.account_id(str(REPO))
    except scan.ScanError:
        account = None
    assert [str(f) for f in scan.scan_paths(scan.tracked_paths(str(REPO)), account, str(REPO))] == []


def test_the_hooks_are_executable_and_stay_lf() -> None:
    staged = subprocess.run(
        ["git", "ls-files", "-s", ".githooks"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    modes = {line.split("\t")[1]: line.split()[0] for line in staged.splitlines()}
    assert modes[".githooks/pre-commit"] == modes[".githooks/commit-msg"] == "100755"
    attrs = subprocess.run(
        ["git", "check-attr", "eol", ".githooks/pre-commit", ".githooks/commit-msg"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout
    assert attrs.count("eol: lf") == 2


# --- git refuses the commit -------------------------------------------------------


class Sandbox:
    """A throwaway repo whose hooks are this repo's, with no global or system git config."""

    def __init__(self, root: Path, account: str | None = FAKE_ID) -> None:
        self.root = root
        empty = root.parent / "empty.gitconfig"
        empty.write_text("", encoding="utf-8")
        self.env = {
            **os.environ,
            # The hooks call python: this one, with the venv's, is the one on PATH.
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
            "GIT_CONFIG_GLOBAL": str(empty),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "core.hooksPath", HOOKS.as_posix())
        if account is not None:
            self.git("config", scan.ACCOUNT_KEY, account)

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=self.root, env=self.env, capture_output=True, text=True, check=False
        )

    def commit(self, files: dict[str, bytes | str], message: str = "add") -> subprocess.CompletedProcess[str]:
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            data = content.encode("utf-8") if isinstance(content, str) else content
            path.write_bytes(data)
            self.git("add", "-f", name)
        return self.git("commit", "-q", "-m", message)

    def commits(self) -> int:
        done = self.git("rev-list", "--count", "HEAD")
        return int(done.stdout) if done.returncode == 0 else 0


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    (tmp_path / "repo").mkdir()
    return Sandbox(tmp_path / "repo")


def test_a_clean_commit_goes_through(sandbox: Sandbox) -> None:
    done = sandbox.commit({"notes.txt": "pool wait p99 1,500 ms at 18:57\n"})
    assert done.returncode == 0, done.stderr
    assert sandbox.commits() == 1


def test_git_refuses_a_commit_holding_the_account_id(sandbox: Sandbox) -> None:
    sandbox.commit({"ok.txt": "fine\n"})
    done = sandbox.commit({"infra/out.json": f'{{"role": "arn:aws:iam::{FAKE_ID}:role/x"}}\n'})
    assert done.returncode != 0
    assert "infra/out.json:1: account-id" in done.stderr
    assert "Commit refused" in done.stderr
    assert FAKE_ID not in done.stderr + done.stdout
    assert sandbox.commits() == 1


def test_git_refuses_the_account_id_inside_a_gzip_fixture(sandbox: Sandbox) -> None:
    data = gzip.compress(f"line\nreq 4f4bb6ac-832d-48bd-b523-{FAKE_ID}\nacct {FAKE_ID}\n".encode())
    done = sandbox.commit({"fixtures/runs.json.gz": data})
    assert done.returncode != 0
    assert "fixtures/runs.json.gz (gunzipped):3: account-id" in done.stderr
    assert sandbox.commits() == 0


def test_git_refuses_a_commit_message_holding_the_account_id(sandbox: Sandbox) -> None:
    done = sandbox.commit({"ok.txt": "fine\n"}, message=f"deploy to {FAKE_ID}")
    assert done.returncode != 0
    assert "commit message:1: account-id" in done.stderr
    assert sandbox.commits() == 0


def test_git_refuses_a_state_file_even_when_forced_past_gitignore(sandbox: Sandbox) -> None:
    done = sandbox.commit({"infra/terraform.tfstate": "{}\n"})
    assert done.returncode != 0
    assert "infra/terraform.tfstate: file-name" in done.stderr
    assert sandbox.commits() == 0


def test_without_the_account_id_configured_nothing_commits(tmp_path: Path) -> None:
    (tmp_path / "repo").mkdir()
    bare = Sandbox(tmp_path / "repo", account=None)
    done = bare.commit({"ok.txt": "fine\n"})
    assert done.returncode != 0
    assert "sentinelops.accountid isn't set" in done.stderr
    assert bare.commits() == 0


def test_the_co_author_line_passes(sandbox: Sandbox) -> None:
    message = "agent: a change\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n"
    done = sandbox.commit({"ok.txt": "fine\n"}, message=message)
    assert done.returncode == 0, done.stderr
