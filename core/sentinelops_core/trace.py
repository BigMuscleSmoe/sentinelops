"""An incident's trace, for the page: one item per step in the sentinelops-trace table.

BUILD_PLAN.md, Phase 5 plan, "The live trace". Two writers share this format:
the agent, from its hooks (agent/run_trace.py), and the remediation flow, with its
PR's progress. Each event is one of the models below, the shapes
web/src/api/contract.ts gives TraceEvent, less seq: the read function numbers
the events in key order.

An item:
  pk          the incident's ID
  sk          the writer and its place: agent#0001, agent#0002, ... (the agent
              counts its steps), then flow#00 ... flow#20 (a slot per flow
              step, FLOW_SLOTS, so a step run again writes the same item).
              "agent" sorts before "flow", as the agent ends before the flow
              starts.
  at          when the step happened, ISO 8601 in UTC with milliseconds
  kind        the event's kind
  event       the event as JSON
  expires_at  7 days on, the table's TTL; left off the recorded run's backfill,
              which keeps

Nothing here scans text. The agent's events carry the model's words and its
tools' results, capped and scanned before they're built (agent/run_trace.py); the
flow's carry numbers and fixed names only.
"""

import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

TABLE_ENV = "SENTINELOPS_TRACE_TABLE"
TTL_S = 7 * 24 * 3600
TEXT_CHARS = 4_000  # a text field's cap: a tool's result, a thought, the agent's words
ARG_CHARS = 200  # a tool argument's
DETAIL_CHARS = 300
INCIDENT_ID = re.compile(r"[a-z][a-z0-9-]{0,40}-\d{8}T\d{6}Z", re.ASCII)
AGENT = "agent"
FLOW = "flow"
POLLS = 15  # the flow's check polls at most (remediation flow.POLLS)
# The flow's slot for each step's event: a step run again writes the same item.
FLOW_SLOTS: dict[str, int] = {
    "load": 0,  # handed_off, as the flow starts
    "load-result": 1,  # flow_failed, if load can't read the incident
    "open": 2,  # pr_opened, pr_same_fix, pr_rejected, or flow_failed
    **{f"checks-{n}": 2 + n for n in range(1, POLLS + 1)},  # pr_checks: 03 to 17
    "conclude": 20,  # pr_ready, or flow_failed
}

AlarmKey = Literal["errors", "latency", "pool_wait"]
ToolName = Literal["get_metrics", "get_recent_logs", "get_deploy_history", "search_runbooks", "read_repo_file"]
HaltReason = Literal["tool_calls", "input_tokens", "cost", "wall_clock", "resends"]
RejectedCheck = Literal["gate", "release", "base", "patch", "scan", "rules"]
Count = Annotated[int, Field(ge=0)]
Number = Annotated[int, Field(ge=1)]
Text = Annotated[str, Field(max_length=TEXT_CHARS)]
CallId = Annotated[str, Field(pattern=r"^t[1-9][0-9]{0,2}$")]


class EventModel(BaseModel):
    """What every event is: its fields fixed, nothing extra."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RunStarted(EventModel):
    kind: Literal["run_started"] = "run_started"
    alarm: AlarmKey


class ModelCall(EventModel):
    kind: Literal["model_call"] = "model_call"
    n: Number
    input_tokens: Count
    output_tokens: Count
    cost_usd: Annotated[float, Field(ge=0)]


class Thinking(EventModel):
    """The model's thinking, as its API summarizes it."""

    kind: Literal["thinking"] = "thinking"
    text: Text


class Said(EventModel):
    """The agent's words between tool calls."""

    kind: Literal["said"] = "said"
    text: Text


class ToolCall(EventModel):
    kind: Literal["tool_call"] = "tool_call"
    call: CallId
    tool: ToolName
    args: dict[Annotated[str, Field(max_length=40)], Annotated[str, Field(max_length=ARG_CHARS)] | int | float | bool]


class ToolResult(EventModel):
    kind: Literal["tool_result"] = "tool_result"
    call: CallId
    source: Annotated[str, Field(pattern=r"^([a-z][1-9][0-9]{0,2})?$")]  # m1, l2, ...; empty for an error
    chars: Count  # the whole result's length; text holds its first TEXT_CHARS
    text: Text
    truncated: bool
    error: bool


class Check(EventModel):
    """A check in code on the answer: the citation, or the patch. Its detail is from fixed templates."""

    kind: Literal["check"] = "check"
    check: Literal["citation", "patch"]
    kept: bool
    detail: Annotated[str, Field(max_length=DETAIL_CHARS)]


class Answered(EventModel):
    kind: Literal["answered"] = "answered"
    confidence: Annotated[float, Field(ge=0, le=1)]


class Halted(EventModel):
    kind: Literal["halted"] = "halted"
    by: HaltReason


class HandedOff(EventModel):
    kind: Literal["handed_off"] = "handed_off"


class PrOpened(EventModel):
    kind: Literal["pr_opened"] = "pr_opened"
    number: Number


class PrSameFix(EventModel):
    """The same fix was already open: a comment on it, at most one a day (commented says whether this one made it)."""

    kind: Literal["pr_same_fix"] = "pr_same_fix"
    number: Number
    commented: bool


class PrChecks(EventModel):
    kind: Literal["pr_checks"] = "pr_checks"
    check: Annotated[str, Field(max_length=100)]
    state: Literal["pending", "success", "failure"]
    poll: Annotated[int, Field(ge=1, le=POLLS)]


class PrReady(EventModel):
    kind: Literal["pr_ready"] = "pr_ready"
    number: Number


class PrRejected(EventModel):
    """The flow's own check refused the patch."""

    kind: Literal["pr_rejected"] = "pr_rejected"
    check: RejectedCheck


class FlowFailed(EventModel):
    """The flow stopped: an error's type or code, never its message."""

    kind: Literal["flow_failed"] = "flow_failed"
    error: Annotated[str, Field(max_length=200)]


Event = Annotated[
    RunStarted
    | ModelCall
    | Thinking
    | Said
    | ToolCall
    | ToolResult
    | Check
    | Answered
    | Halted
    | HandedOff
    | PrOpened
    | PrSameFix
    | PrChecks
    | PrReady
    | PrRejected
    | FlowFailed,
    Field(discriminator="kind"),
]
EVENT: TypeAdapter[Event] = TypeAdapter(Event)


def agent_key(n: int) -> str:
    """The agent's nth step's sort key, from 1."""
    if not 1 <= n <= 9999:
        raise ValueError("an agent step is numbered 1 to 9999")
    return f"{AGENT}#{n:04d}"


def flow_key(step: str) -> str:
    """The sort key of a flow step's event, from its slot."""
    return f"{FLOW}#{FLOW_SLOTS[step]:02d}"


def iso(at: datetime) -> str:
    """ISO 8601 in UTC with milliseconds, ending in Z, as the page reads times."""
    at = at.astimezone(UTC)
    return at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"


def item(incident_id: str, key: str, at: datetime, event: EventModel, *, expires: bool = True) -> dict[str, Any]:
    """The table item for one event, in DynamoDB's typed form."""
    if not INCIDENT_ID.fullmatch(incident_id):
        raise ValueError("not an incident's ID")
    if not re.fullmatch(rf"({AGENT}#[0-9]{{4}}|{FLOW}#[0-9]{{2}})", key):
        raise ValueError("not a trace key")
    found: dict[str, Any] = {
        "pk": {"S": incident_id},
        "sk": {"S": key},
        "at": {"S": iso(at)},
        "kind": {"S": event.kind},  # type: ignore[attr-defined]
        "event": {"S": event.model_dump_json()},
    }
    if expires:
        found["expires_at"] = {"N": str(int(at.timestamp()) + TTL_S)}
    return found


def put(client: Any, table: str, incident_id: str, key: str, at: datetime, event: EventModel, *, expires: bool = True) -> None:
    """Write one event. Raises what the client raises: each writer decides what a failure means to it."""
    client.put_item(TableName=table, Item=item(incident_id, key, at, event, expires=expires))


def read(found: dict[str, Any]) -> tuple[str, str, Event]:
    """An item's key, time, and event, checked against its model."""
    return found["sk"]["S"], found["at"]["S"], EVENT.validate_json(found["event"]["S"])
