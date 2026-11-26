"""The flow's writes to GitHub: the fix branch, its one file, the draft PR or a comment, and the end of the CI wait.

BUILD_PLAN.md, Phase 4 design, the flow's steps 3-5. A durable step runs at
least once, and a create that timed out may have landed (github.py raises
Unavailable for it at once), so every write first looks for its own result:
the branch by name, the file by its content, the PR by its head and the
incident's marker in its body, a comment by its marker. Nothing here merges,
deletes, or touches a branch but the fix branch, and the client's own checks
(github.py) refuse anything else whatever this module asks.
"""

import base64
from dataclasses import asdict, dataclass
from typing import Any

from sentinelops_remediation import body as texts
from sentinelops_remediation.body import CHECK, Incident
from sentinelops_remediation.github import OWNER, GitHubApp, Refused, Unavailable
from sentinelops_remediation.validate import Validated

# What a finished check run can conclude; only success passes.
CONCLUSIONS = frozenset(
    {"success", "failure", "neutral", "cancelled", "skipped", "timed_out", "action_required", "stale"}
)
COMMENT_PAGES = 10


@dataclass(frozen=True)
class Pull:
    """The PR the flow opened or found, as a step's result: no token, no text the model wrote."""

    status: str  # "opened" (this incident's PR) or "commented" (another incident's, open)
    number: int
    url: str
    node_id: str
    draft: bool
    head_sha: str
    pushed: bool  # whether this incident committed the file

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def open_or_update(github: GitHubApp, fix: Validated, hypothesis_body: str, incident: Incident) -> Pull:
    """The fix branch with the patched file, and a draft PR for it, or a comment on the one open."""
    if _branch_head(github, fix.branch) is None:
        try:
            github.call("create_ref", body={"ref": f"refs/heads/{fix.branch}", "sha": fix.base_sha})
        except Unavailable:
            pass  # it may have landed: look again
        except Refused as refused:
            if refused.status != 422:  # 422: it exists already (made a moment ago)
                raise
        if _branch_head(github, fix.branch) is None:
            raise Unavailable("create_ref", 0, "the branch isn't there after creating it")

    pushed = False
    wanted = fix.text.encode("utf-8")
    current, blob = github.file_and_blob(fix.path, fix.branch)
    if current != wanted:
        content = base64.b64encode(wanted).decode("ascii")
        message = texts.commit_message(fix, incident)
        try:
            github.call(
                "put_file",
                {"path": fix.path},
                body={"message": message, "content": content, "branch": fix.branch, "sha": blob},
            )
        except Unavailable:
            pass
        if github.file_at(fix.path, fix.branch) != wanted:
            raise Unavailable("put_file", 0, "the file isn't the fix after committing it")
        pushed = True
    head = github.head_commit(fix.branch)

    mine = texts.marker(incident.id)
    found = _open_pull(github, fix)
    if found is None:
        request = {
            "title": texts.title(fix, incident),
            "body": hypothesis_body,
            "head": fix.branch,
            "base": fix.base,
            "draft": True,
        }
        try:
            github.call("create_pull", body=request)
        except Unavailable:
            pass
        found = _open_pull(github, fix)
        if found is None:
            raise Unavailable("create_pull", 0, "no open PR after opening one")
        status = "opened"
    elif mine in (found.get("body") or ""):
        status = "opened"  # this incident's own, from an earlier try of this step
    else:
        status = "commented"
        number = _number(found)
        if not _has_comment(github, number, mine):
            comment = texts.same_fix_comment(fix, incident, pushed)
            try:
                github.call("create_comment", {"number": number}, body={"body": comment})
            except Unavailable:
                if not _has_comment(github, number, mine):
                    raise
    return Pull(
        status=status,
        number=_number(found),
        url=_text(found, "html_url"),
        node_id=_text(found, "node_id"),
        draft=found.get("draft") is True,
        head_sha=head,
        pushed=pushed,
    )


def check_state(github: GitHubApp, sha: str) -> dict[str, Any]:
    """The waited-on check on a commit: pending (not there or not finished), success, or failure and how."""
    found = github.call("check_runs", {"sha": sha}, {"check_name": CHECK, "per_page": 100})
    runs = [r for r in (found or {}).get("check_runs") or [] if isinstance(r, dict) and r.get("name") == CHECK]
    if not runs:
        return {"state": "pending", "conclusion": None}
    latest = max(runs, key=lambda r: (str(r.get("started_at") or ""), r.get("id") if type(r.get("id")) is int else 0))
    if latest.get("status") != "completed":
        return {"state": "pending", "conclusion": None}
    conclusion = latest.get("conclusion") if latest.get("conclusion") in CONCLUSIONS else "unknown"
    return {"state": "success" if conclusion == "success" else "failure", "conclusion": conclusion}


def mark_ready(github: GitHubApp, pull: Pull) -> None:
    """Out of draft, once the check passed: the one GraphQL mutation the client sends."""
    if pull.draft:
        github.mark_ready_for_review(pull.node_id)


def report_checks(github: GitHubApp, pull: Pull, incident: Incident, conclusion: str | None) -> None:
    """A comment saying the check failed (or didn't finish), once: the PR stays a draft."""
    mine = texts.marker(incident.id, "checks")
    if _has_comment(github, pull.number, mine):
        return
    try:
        github.call("create_comment", {"number": pull.number}, body={"body": texts.checks_comment(incident, conclusion)})
    except Unavailable:
        if not _has_comment(github, pull.number, mine):
            raise


def _branch_head(github: GitHubApp, branch: str) -> str | None:
    try:
        return github.head_commit(branch)
    except Refused as refused:
        if refused.status == 404:
            return None
        raise


def _open_pull(github: GitHubApp, fix: Validated) -> dict[str, Any] | None:
    pulls = github.call("list_pulls", query={"head": f"{OWNER}:{fix.branch}", "base": fix.base, "state": "open"})
    if not isinstance(pulls, list):
        raise Refused("list_pulls", 200, "not a list of PRs")
    mine = [p for p in pulls if isinstance(p, dict) and (p.get("head") or {}).get("ref") == fix.branch]
    return min(mine, key=_number) if mine else None


def _has_comment(github: GitHubApp, number: int, marker: str) -> bool:
    for page in range(1, COMMENT_PAGES + 1):
        comments = github.call("list_comments", {"number": number}, {"per_page": 100, "page": page})
        if not isinstance(comments, list):
            raise Refused("list_comments", 200, "not a list of comments")
        if any(isinstance(c, dict) and (c.get("body") or "").startswith(marker) for c in comments):
            return True
        if len(comments) < 100:
            return False
    raise Refused("list_comments", 200, f"more than {COMMENT_PAGES * 100} comments")


def _number(pull: dict[str, Any]) -> int:
    number = pull.get("number")
    if type(number) is not int or number <= 0:
        raise Refused("list_pulls", 200, "a PR without a number")
    return number


def _text(pull: dict[str, Any], field: str) -> str:
    value = pull.get(field)
    if not isinstance(value, str) or not value or len(value) > 200:
        raise Refused("list_pulls", 200, f"a PR without its {field}")
    return value
