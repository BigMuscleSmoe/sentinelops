"""Refuse a commit that would publish what this public repo must never hold.

Git runs it as two hooks (core.hooksPath = .githooks):

    pre-commit   python -I commit_scan.py staged         every staged file, as staged
    commit-msg   python -I commit_scan.py message FILE   the commit message

and on demand:

    python -I .githooks/commit_scan.py tracked    every tracked file, as in the index
    python -I .githooks/commit_scan.py install    point git at these hooks and store the account ID

Any finding exits 1, so git refuses the commit. So does a scan that can't run:
no account ID configured, an unreadable blob, a gzip file that won't open.
A finding names the file, the line, and the rule, never the text it matched,
so nothing it prints can leak what it caught.

The rules:
  account-id     the account ID (git config sentinelops.accountid, local to this
                 clone and never committed), anywhere: inside numbers, gzip
                 files' content, and their raw bytes
  12-digits      any other run of exactly 12 digits, which is how an account ID
                 looks, unless it's a UUID's last group, a decimal fraction, or
                 one of the placeholders below
  email          an email address, unless its domain is reserved for examples
                 or it's a noreply address
  home-path      a path into someone's home folder (Users or home), the way
                 Windows, Git Bash, macOS, or Linux writes it
  public-ip      an IPv4 address that's globally routable
  aws-key        an AWS access key ID, unless it says it's a test or example
  anthropic-key  an Anthropic API key or OAuth token
  file-name      state, plans, tfvars, .env files, and private keys, by name

Standard library only: the hook runs on the machine's Python, before any venv.
"""

import gzip
import ipaddress
import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath

PROFILE = "sentinelops"
ACCOUNT_KEY = "sentinelops.accountid"
# Stand-ins that look like account IDs on purpose: AWS's documented
# placeholder, which the recorder writes in place of the real one, and the
# tests' fake account.
PLACEHOLDER_IDS = {"123456789012", "000011112222"}
EXAMPLE_DOMAINS = re.compile(r"(^|\.)(example\.(com|org|net)|[a-z0-9-]+\.(invalid|test|example|localhost))$", re.I)
NOREPLY = re.compile(r"^(noreply|no-reply)@|@users\.noreply\.github\.com$", re.I)
FORBIDDEN_NAMES = re.compile(r"(\.tfstate(\..*)?|\.tfvars|\.tfvars\.json|\.tfplan|\.pem|\.key|\.p12|\.pfx)$|^\.env(\..*)?$")

UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
TWELVE = re.compile(r"(?<![0-9A-Za-z.])[0-9]{12}(?![0-9A-Za-z]|\.[0-9])")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
HOME = re.compile(r"[A-Za-z]:(\\\\|\\|/)+Users(\\\\|\\|/)+[^\\/\s\"'<>]+|(?<![\w.])/(c/Users|Users|home)/[^/\s\"'<>]+", re.I)
IPV4 = re.compile(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9]|\.[0-9])")
AWS_KEY = re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA|AROA|AIDA)[A-Z0-9]{16}(?![A-Z0-9])")
AWS_KEY_OK = re.compile(r"TEST|EXAMPLE")
# Real keys and tokens carry 90-odd random characters after the prefix.
ANTHROPIC_KEY = re.compile(r"sk-ant-[a-z0-9]+-[A-Za-z0-9_-]{40,}")
ANTHROPIC_KEY_OK = re.compile(r"N0tARealToken")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}" if self.line else f"{self.path}: {self.rule}"


class ScanError(Exception):
    """The scan couldn't run, so the commit can't be cleared."""


def scan_text(path: str, text: str, account_id: str | None) -> list[Finding]:
    """Every finding in one text, by line. The matched text itself is never kept."""
    findings: list[Finding] = []

    def add(offset: int, rule: str) -> None:
        findings.append(Finding(path, text.count("\n", 0, offset) + 1, rule))

    if account_id:
        start = text.find(account_id)
        while start != -1:
            add(start, "account-id")
            start = text.find(account_id, start + 1)
    # UUIDs out first, kept the same length so line numbers hold: a request ID's
    # last group is often 12 digits.
    no_uuids = UUID.sub(lambda m: "u" * len(m.group()), text)
    for m in TWELVE.finditer(no_uuids):
        if m.group() not in PLACEHOLDER_IDS and m.group() != account_id:
            add(m.start(), "12-digits")
    for m in EMAIL.finditer(text):
        if not EXAMPLE_DOMAINS.search(m.group(1)) and not NOREPLY.search(m.group()):
            add(m.start(), "email")
    for m in HOME.finditer(text):
        add(m.start(), "home-path")
    for m in IPV4.finditer(text):
        try:
            address = ipaddress.IPv4Address(m.group())
        except ValueError:
            continue
        if address.is_global:
            add(m.start(), "public-ip")
    for m in AWS_KEY.finditer(text):
        if not AWS_KEY_OK.search(m.group()):
            add(m.start(), "aws-key")
    for m in ANTHROPIC_KEY.finditer(text):
        if not ANTHROPIC_KEY_OK.search(m.group()):
            add(m.start(), "anthropic-key")
    return findings


def scan_blob(path: str, data: bytes, account_id: str | None) -> list[Finding]:
    """One file's bytes: its name, its text, and a gzip file's raw bytes and content both."""
    findings = []
    if FORBIDDEN_NAMES.search(PurePosixPath(path).name):
        findings.append(Finding(path, 0, "file-name"))
    if data[:2] == b"\x1f\x8b":
        # The gzip header can carry a file name, so the raw bytes are scanned too.
        findings += scan_text(f"{path} (raw)", data.decode("latin-1"), account_id)
        try:
            content = gzip.decompress(data)
        except (OSError, EOFError) as e:
            raise ScanError(f"{path}: not a readable gzip file ({type(e).__name__})") from None
        findings += scan_text(f"{path} (gunzipped)", content.decode("utf-8", errors="replace"), account_id)
    else:
        findings += scan_text(path, data.decode("utf-8", errors="replace"), account_id)
    return findings


def git(*args: str, data: bytes | None = None, repo: str | None = None) -> bytes:
    """A git command's output, run in repo (default: here)."""
    done = subprocess.run(["git", *args], input=data, capture_output=True, check=False, cwd=repo)
    if done.returncode != 0:
        raise ScanError(f"git {args[0]} failed ({done.returncode})")
    return done.stdout


def account_id(repo: str | None = None) -> str:
    """The account ID from this clone's local git config. Unset, the scan can't clear a commit."""
    done = subprocess.run(
        ["git", "config", "--local", "--get", ACCOUNT_KEY], capture_output=True, text=True, check=False, cwd=repo
    )
    value = done.stdout.strip()
    if not re.fullmatch(r"[0-9]{12}", value):
        raise ScanError(
            f"{ACCOUNT_KEY} isn't set to a 12-digit account ID in this clone's git config: "
            "run python -I .githooks/commit_scan.py install"
        )
    return value


def index_blobs(paths: list[str], repo: str | None = None) -> Iterator[tuple[str, bytes]]:
    """Each path's content as staged, read in one git cat-file call."""
    if not paths:
        return
    request = "".join(f":{p}\n" for p in paths).encode("utf-8")
    out = git("cat-file", "--batch", data=request, repo=repo)
    pos = 0
    for path in paths:
        end = out.index(b"\n", pos)
        header = out[pos:end].decode("utf-8", errors="replace").split()
        if len(header) != 3:
            raise ScanError(f"{path}: git couldn't read it from the index")
        size = int(header[2])
        start = end + 1
        yield path, out[start : start + size]
        pos = start + size + 1


def staged_paths(repo: str | None = None) -> list[str]:
    out = git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z", repo=repo)
    return [p for p in out.decode("utf-8").split("\0") if p]


def tracked_paths(repo: str | None = None) -> list[str]:
    out = git("ls-files", "--cached", "--full-name", "-z", repo=repo)
    return [p for p in out.decode("utf-8").split("\0") if p]


def scan_paths(paths: list[str], account: str | None, repo: str | None = None) -> list[Finding]:
    findings = []
    for path, data in index_blobs(paths, repo):
        findings += scan_blob(path, data, account)
    return findings


def scan_message(file: str, account: str | None) -> list[Finding]:
    with open(file, encoding="utf-8", errors="replace") as f:
        lines = [line for line in f.read().splitlines() if not line.startswith("#")]
    return scan_text("commit message", "\n".join(lines), account)


def install() -> int:
    """Point this clone at .githooks and store the account ID, read from STS, without printing it."""
    done = subprocess.run(
        ["aws", "sts", "get-caller-identity", "--profile", PROFILE, "--query", "Account", "--output", "text"],
        capture_output=True,
        text=True,
        check=False,
    )
    value = done.stdout.strip()
    if done.returncode != 0 or not re.fullmatch(r"[0-9]{12}", value):
        print(f"install: couldn't read the account ID from STS with profile {PROFILE}", file=sys.stderr)
        return 1
    git("config", "--local", ACCOUNT_KEY, value)
    git("config", "--local", "core.hooksPath", ".githooks")
    print("install: hooks on (core.hooksPath = .githooks), account ID stored in this clone's git config")
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["install"]:
        return install()
    try:
        if argv[:1] == ["staged"]:
            findings, what = scan_paths(staged_paths(), account_id()), "staged files"
        elif argv[:1] == ["message"] and len(argv) == 2:
            findings, what = scan_message(argv[1], account_id()), "commit message"
        elif argv[:1] == ["tracked"]:
            # Without the account ID (as in CI), the 12-digit rule still covers it.
            try:
                account: str | None = account_id()
            except ScanError:
                account = None
            findings, what = scan_paths(tracked_paths(), account), "tracked files"
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except ScanError as e:
        print(f"commit scan: {e}. Commit refused.", file=sys.stderr)
        return 1
    if not findings:
        return 0
    for finding in findings:
        print(f"commit scan: {finding}", file=sys.stderr)
    print(
        f"commit scan: {len(findings)} finding(s) in the {what}. Commit refused. "
        "Nothing above shows the matched text; open the file at that line.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
