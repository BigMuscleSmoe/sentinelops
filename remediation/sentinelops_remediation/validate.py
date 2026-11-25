"""The flow's validation step: everything a fix must pass before anything is written to GitHub.

BUILD_PLAN.md, Phase 4 design, the flow's step 2. It reads GitHub as it is
now, checks the patch with the same code that checked it in the run, and
writes nothing. A failure is a result, not an error: Rejected, with the
check's name and a few words. The flow records it and opens no PR, retries
nothing, and calls no model; the diagnosis stands. Only a failure to reach
GitHub or sign in (github.GitHubError, github.SigningFailed) raises.

The checks, in order:
  gate     Hypothesis.can_open_pr, again: nothing reaches GitHub the gate
           didn't pass
  release  the base is the patch's release's branch in the release map
           (sentinelops_core.releases): never main, never a branch the model
           named
  base     the file at that branch's head is, byte for byte, the file the
           agent read (Remediation.file_sha256)
  patch    the edits pass patches.apply_edits again on those bytes and render
           the diff the run kept
  scan     the commit hooks' rules (.githooks/commit_scan.py, imported as it
           is, one copy) over the patched file and everything else the flow
           will post (the PR body, the commit message, a comment), plus one
           rule of the flow's own: no ARN. A finding names the text and the
           line and the rule, never what matched.
  rules    main and the base still refuse updates, deletions, and force
           pushes, so not even this App can write to either. Checked before
           every write (decision 15), never trusted from setup.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

import commit_scan  # .githooks/commit_scan.py: on pytest's pythonpath here, staged beside the package in the zip

from sentinelops_core import code, patches
from sentinelops_core.models import Hypothesis
from sentinelops_core.releases import RELEASES
from sentinelops_remediation.github import FIX_BRANCH, GitHubApp

# The flow's rule beside the hooks': an ARN names an account's resources even
# without its ID (a bucket's, say), and nothing the flow posts needs one.
ARN = re.compile(r"\barn:aws[a-z-]*:", re.ASCII)
# What ruleset protect-main-and-releases puts on main and release/**.
PROTECTING = frozenset({"update", "deletion", "non_fast_forward"})
MAX_FINDINGS_SHOWN = 5


@dataclass(frozen=True)
class Validated:
    """A fix ready to write: where it goes, and what."""

    version: str
    base: str  # the release branch the PR targets
    base_sha: str  # that branch's head when validated
    path: str  # the file, from the repo's root
    text: str  # the file as patched
    diff: str
    branch: str  # the fix branch, sentinelops/fix-<version>-<module>


@dataclass(frozen=True)
class Rejected:
    """Why there's no PR: the check that failed, in a few words."""

    check: str
    reason: str


def fix_branch(version: str, file_path: str) -> str:
    """sentinelops/fix-<version>-<module>: one per release and file, never the fault's name (decision 9)."""
    branch = f"sentinelops/fix-{version}-{PurePosixPath(file_path).stem}"
    if not FIX_BRANCH.fullmatch(branch):
        raise ValueError(f"{branch!r} isn't a fix branch")
    return branch


def validate(
    hypothesis: Hypothesis, github: GitHubApp, account_id: str, outgoing: Mapping[str, str] | None = None
) -> Validated | Rejected:
    """The fix, checked against GitHub now; outgoing is every other text the flow will post, by name."""
    if not isinstance(account_id, str) or not re.fullmatch(r"[0-9]{12}", account_id):
        raise ValueError("account_id isn't a 12-digit account ID")
    patch = hypothesis.remediation
    if not hypothesis.can_open_pr or patch is None or patch.version is None or patch.file_sha256 is None:
        return Rejected("gate", "can_open_pr is False")

    release = RELEASES.get(patch.version)
    if release is None:
        return Rejected("release", "not a release in the release map")
    if release.branch is None or not release.branch.startswith("release/"):
        return Rejected("release", "the release map gives this release no fix branch")

    if not code.readable(patch.file_path):
        return Rejected("patch", "a file that can't be patched")
    path = f"{code.SERVICE_ROOT}/{patch.file_path}"
    head = github.head_commit(release.branch)
    raw = github.file_at(path, head)
    try:
        original = raw.decode("utf-8")
    except UnicodeDecodeError:
        original = None
    if original is None or code.digest(original) != patch.file_sha256:
        return Rejected("base", "the file at the base isn't the file the agent read")

    try:
        checked = patches.apply_edits(patch.file_path, original, [(edit.old, edit.new) for edit in patch.edits])
    except patches.Refused as refused:
        return Rejected("patch", str(refused))
    if checked.diff != patch.diff:
        return Rejected("patch", "the edits don't render the diff the run kept")

    findings = scan({path: checked.text, **(outgoing or {})}, account_id)
    if findings:
        shown = "; ".join(findings[:MAX_FINDINGS_SHOWN])
        more = len(findings) - MAX_FINDINGS_SHOWN
        return Rejected("scan", shown + (f"; and {more} more" if more > 0 else ""))

    for branch in ("main", release.branch):
        if not protected(github, branch):
            return Rejected("rules", f"{branch} no longer refuses updates, deletions, and force pushes")

    return Validated(
        version=patch.version,
        base=release.branch,
        base_sha=head,
        path=path,
        text=checked.text,
        diff=checked.diff,
        branch=fix_branch(patch.version, patch.file_path),
    )


def scan(texts: Mapping[str, str], account_id: str) -> list[str]:
    """Every finding in the texts, as 'name:line: rule': the hooks' rules and the ARN rule. Never the matched text."""
    found: list[str] = []
    for name, text in texts.items():
        found += [str(finding) for finding in commit_scan.scan_text(name, text, account_id)]
        for match in ARN.finditer(text):
            found.append(str(commit_scan.Finding(name, text.count("\n", 0, match.start()) + 1, "arn")))
    return found


def protected(github: GitHubApp, branch: str) -> bool:
    """Whether a branch's rules still refuse updates (with no fork-sync exception), deletions, and force pushes."""
    rules = github.branch_rules(branch)
    if not PROTECTING <= {rule.get("type") for rule in rules}:
        return False
    # An update rule that allows fetch-and-merge lets a fork sync push to it.
    return any(
        rule.get("type") == "update" and not (rule.get("parameters") or {}).get("update_allows_fetch_and_merge")
        for rule in rules
    )
