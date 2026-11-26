"""The remediation flow, as a Lambda durable execution: from an answered incident to a draft PR, with no model call.

BUILD_PLAN.md, Phase 4 design and "Evening 5 plan". The trigger starts it
asynchronously with {service, incident_id}, named by the incident's ID, so a
second start is the same execution. Nothing else in the event is used: the
answer is read from the incident item. The steps, each checkpointed and run
at most once per attempt with no retry by the SDK (the GitHub client retries
reads itself; a failure is a result):

  load      the incident and its Hypothesis; can_open_pr, the one gate.
            False: no_pr
  open      validate (validate.py), then the texts built (body.py) and
            scanned, then the fix branch, its file, and a draft PR, or a
            comment on the one open (publish.py). A failed check: rejected
  checks-n  after wait-n (60 s), the waited-on check on the branch's head,
            up to 15 times, until it passes or fails
  conclude  passed: ready for review; failed or not finished: a comment,
            and the PR stays a draft
  record    the result on the incident, as one map

A step returns only typed facts (no token, no model text but the stored
Hypothesis, which is in DynamoDB already), so nothing secret is ever in a
checkpoint or the execution's history. Steps catch the errors they expect and
return them as their type or code, never a message, so neither the SDK's
logs nor the record can hold an ARN. Code between steps runs again on every
replay and does nothing but read step results, so it's deterministic.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aws_durable_execution_sdk_python import DurableContext
from aws_durable_execution_sdk_python.config import Duration, StepConfig
from aws_durable_execution_sdk_python.retries import RetryPresets
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from sentinelops_core.models import Hypothesis
from sentinelops_remediation import body, publish
from sentinelops_remediation.body import CHECK, Incident
from sentinelops_remediation.github import GitHubApp, GitHubError, NotAllowed, SigningFailed
from sentinelops_remediation.incidents import Incidents
from sentinelops_remediation.publish import Pull
from sentinelops_remediation.validate import Rejected, scanned, validate

SERVICE = "checkout-api"
POLL = Duration.from_seconds(60)
POLLS = 15
ONCE = StepConfig(retry_strategy=RetryPresets.none())
# What the record keeps, as the incident's remediation map.
RECORDED = (
    "status",
    "pr",
    "pr_number",
    "pr_url",
    "pushed",
    "checks",
    "conclusion",
    "failed_check",
    "rejected_check",
    "reason",
    "error",
)
# The investigation statuses the trigger stores (trigger/trigger.py).
INVESTIGATION_STATUSES = frozenset({"done", "failed", "refused", "invalid", "error", "busy", "capped", "investigating"})


@dataclass
class Deps:
    incidents: Incidents
    github: Callable[[], GitHubApp]  # one App per invocation; the handler revokes its token when the invocation ends
    account_id: str
    clock: Callable[[], float] = field(default=time.time)


class Session:
    """One invocation's GitHub App, made on first use, its installation token revoked by close()."""

    def __init__(self, make: Callable[[], GitHubApp]) -> None:
        self._make = make
        self._app: GitHubApp | None = None

    def github(self) -> GitHubApp:
        if self._app is None:
            self._app = self._make()
        return self._app

    def close(self) -> None:
        if self._app is None:
            return
        try:
            self._app.revoke()
        except Exception as error:  # a token left unrevoked expires within the hour
            log({"event": "token_revoke_failed", "error": github_error(error)})


def invocation(
    event: Any, context: DurableContext, incidents: Incidents, make_github: Callable[[], GitHubApp], account_id: str
) -> dict[str, Any]:
    """One invocation of the flow: the first, or one after a wait. Its token, if it made one, ends with it,
    however it ends: a durable wait suspends the invocation by raising through here."""
    session = Session(make_github)
    try:
        return run(event, context, Deps(incidents, session.github, account_id))
    finally:
        session.close()


def parse(event: Any) -> tuple[str, str] | None:
    """The service and incident ID the trigger sends, and nothing else."""
    if not isinstance(event, dict) or set(event) != {"service", "incident_id"}:
        return None
    service, incident_id = event["service"], event["incident_id"]
    if service != SERVICE or not isinstance(incident_id, str) or not body.INCIDENT_ID.fullmatch(incident_id):
        return None
    if not incident_id.startswith(f"{SERVICE}-"):
        return None
    return service, incident_id


def run(event: Any, context: DurableContext, deps: Deps) -> dict[str, Any]:
    parsed = parse(event)
    if parsed is None:
        return log({"event": "remediation", "status": "failed", "error": "not an event the flow takes"})
    service, incident_id = parsed

    loaded = context.step(lambda _: load(deps, service, incident_id), name="load", config=ONCE)
    if loaded["status"] != "eligible":
        return _record(context, deps, service, incident_id, loaded)

    opened = context.step(lambda _: open_pr(deps, loaded), name="open", config=ONCE)
    if opened["status"] not in ("opened", "commented"):
        return _record(context, deps, service, incident_id, opened)
    pull = Pull(**opened["pull"])
    incident = Incident(**loaded["incident"])

    checks: dict[str, Any] = {"state": "pending", "conclusion": None}
    for n in range(1, POLLS + 1):
        context.wait(POLL, name=f"wait-{n}")
        checks = context.step(lambda _: poll(deps, incident_id, pull.head_sha, n), name=f"checks-{n}", config=ONCE)
        if checks["state"] != "pending":
            break

    ended = context.step(lambda _: conclude(deps, pull, incident, checks), name="conclude", config=ONCE)
    return _record(context, deps, service, incident_id, ended)


def load(deps: Deps, service: str, incident_id: str) -> dict[str, Any]:
    """The incident's answer, and whether it may open a PR: can_open_pr decides, nothing else."""
    try:
        item = deps.incidents.get(service, incident_id)
    except (ClientError, BotoCoreError) as error:
        return _failed("load", aws_error(error))
    if item is None or item.incident != incident_id:
        return _failed("load", "no such incident")
    if item.status != "done" or item.hypothesis is None:
        status = item.status if item.status in INVESTIGATION_STATUSES else "unknown"
        log({"event": "remediation", "incident": incident_id, "step": "load", "status": "no_pr", "investigation": status})
        return {"status": "no_pr"}
    try:
        hypothesis = Hypothesis.model_validate_json(item.hypothesis)
    except ValidationError:
        return _failed("load", "the stored answer isn't a Hypothesis")
    if not hypothesis.can_open_pr:
        log({"event": "remediation", "incident": incident_id, "step": "load", "status": "no_pr"})
        return {"status": "no_pr"}
    try:
        incident = Incident(item.incident, item.opened_by, item.opened_at)
    except ValueError:
        return _failed("load", "the incident's fields aren't in their forms")
    log({"event": "remediation", "incident": incident_id, "step": "load", "status": "eligible"})
    return {
        "status": "eligible",
        "hypothesis": item.hypothesis,
        "incident": {"id": incident.id, "alarm": incident.alarm, "opened_at": incident.opened_at},
    }


def open_pr(deps: Deps, loaded: dict[str, Any]) -> dict[str, Any]:
    """Validate, build and scan every text, then the branch, the file, and the draft PR or a comment."""
    hypothesis = Hypothesis.model_validate_json(loaded["hypothesis"])
    incident = Incident(**loaded["incident"])
    try:
        github = deps.github()
        fix = validate(hypothesis, github, deps.account_id)
        if isinstance(fix, Rejected):
            return _rejected(incident.id, fix)
        pr_body = body.pr_body(hypothesis, fix, incident)
        outgoing = {
            "title": body.title(fix, incident),
            "body": pr_body,
            "commit message": body.commit_message(fix, incident),
            "comment": body.same_fix_comment(fix, incident, pushed=False),
            "comment, pushed": body.same_fix_comment(fix, incident, pushed=True),
            "checks comment": body.checks_comment(incident, None),
        }
        rejected = scanned(outgoing, deps.account_id)
        if rejected is not None:
            return _rejected(incident.id, rejected)
        pull = publish.open_or_update(github, fix, pr_body, incident)
    except (GitHubError, SigningFailed, NotAllowed, ValueError) as error:
        return _failed("open", github_error(error), incident.id)
    log(
        {
            "event": "remediation",
            "incident": incident.id,
            "step": "open",
            "status": pull.status,
            "pr_number": pull.number,
            "pushed": pull.pushed,
        }
    )
    return {"status": pull.status, "pull": pull.to_dict()}


def poll(deps: Deps, incident_id: str, sha: str, n: int) -> dict[str, Any]:
    """The waited-on check, once. A failure to read it is pending: the next poll tries again."""
    try:
        state = publish.check_state(deps.github(), sha)
    except (GitHubError, SigningFailed, NotAllowed) as error:
        state = {"state": "pending", "conclusion": None, "error": github_error(error)}
    log({"event": "remediation", "incident": incident_id, "step": f"checks-{n}", **state})
    return state


def conclude(deps: Deps, pull: Pull, incident: Incident, checks: dict[str, Any]) -> dict[str, Any]:
    """Ready for review if the check passed; otherwise a comment saying so, the PR left a draft."""
    result: dict[str, Any] = {"pr": pull.status, "pr_number": pull.number, "pr_url": pull.url, "pushed": pull.pushed}
    try:
        github = deps.github()
        if checks["state"] == "success":
            publish.mark_ready(github, pull)
            result |= {"status": "ready", "checks": "success"}
        else:
            publish.report_checks(github, pull, incident, checks["conclusion"])
            failed = checks["state"] == "failure"
            result |= {
                "status": "checks_failed" if failed else "checks_timeout",
                "checks": checks["state"],
                "conclusion": checks["conclusion"],
                "failed_check": CHECK,
            }
    except (GitHubError, SigningFailed, NotAllowed) as error:
        result |= {"status": "failed", "error": github_error(error)}
    log({"event": "remediation", "incident": incident.id, "step": "conclude", "status": result["status"]})
    return result


def _record(context: DurableContext, deps: Deps, service: str, incident_id: str, result: dict[str, Any]) -> dict[str, Any]:
    return context.step(lambda _: record(deps, service, incident_id, result), name="record", config=ONCE)


def record(deps: Deps, service: str, incident_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """The flow's result on the incident, as its remediation map: the incident record is the notice (decision 16)."""
    fields = {name: result[name] for name in RECORDED if result.get(name) is not None}
    fields["finished_at"] = datetime.fromtimestamp(deps.clock(), UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    try:
        deps.incidents.record(service, incident_id, fields)
        stored = "stored"
    except (ClientError, BotoCoreError) as error:
        stored = aws_error(error)
    shown = {k: v for k, v in fields.items() if k not in ("pr_url", "reason")}
    log({"event": "remediation_result", "incident": incident_id, **shown, "record": stored})
    return {**fields, "record": stored}


def _rejected(incident_id: str, rejected: Rejected) -> dict[str, Any]:
    log({"event": "remediation", "incident": incident_id, "step": "open", "status": "rejected", "check": rejected.check})
    return {"status": "rejected", "rejected_check": rejected.check, "reason": rejected.reason}


def _failed(step: str, error: str, incident_id: str | None = None) -> dict[str, Any]:
    log({"event": "remediation", "incident": incident_id, "step": step, "status": "failed", "error": error})
    return {"status": "failed", "error": error}


def aws_error(error: Exception) -> str:
    """An AWS error's code, or the exception's type: never the message, which can name an ARN."""
    if isinstance(error, ClientError):
        return error.response.get("Error", {}).get("Code") or "ClientError"
    return type(error).__name__


def github_error(error: Exception) -> str:
    """A GitHub or signing failure as its endpoint and status, or its type: never GitHub's message or a value."""
    if isinstance(error, GitHubError):
        return f"{type(error).__name__}: {error.endpoint} HTTP {error.status}"
    if isinstance(error, SigningFailed):
        return str(error)  # "kms:Sign failed (<code>)"
    return type(error).__name__


def log(line: dict[str, Any]) -> dict[str, Any]:
    """One JSON line to stdout: typed fields only, never the model's text, a token, or a URL (Phase 5's log rule)."""
    print(json.dumps(line, default=str), flush=True)
    return line
