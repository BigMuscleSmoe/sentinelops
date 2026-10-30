"""The trigger: the one way an alarm reaches the agent, and where the global
limits live, in code.

EventBridge's rule sentinelops-alarms sends each of checkout-api's alarms going
into ALARM or back to OK. Triage comes first (triage.py), and only a NEW
incident goes on: the lock, so one investigation runs at a time; a slot in the
hourly cap of 20; then one live investigation on AgentCore Runtime, which the
trigger waits for. Then it stops the session, which would otherwise sit idle
for 120 s, stores the answer on the incident, and releases the lock.

Timeouts nest, each outlasting the one inside it: the agent's guard ends a run
at 300 s, the runtime ends a session at 360 s, the trigger waits 370 s for the
answer, Lambda ends the trigger at 400 s, and the lock expires at 420 s
(tests/test_timeouts.py holds them in that order). The invoke is sent once and
never retried, and Lambda doesn't retry this function, so no failure here can
start a second investigation.
"""

import gzip
import json
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from store import Store
from triage import ALARM, OK, Change, Incident, Verdict, iso, parse_time, service_for

INVOKE_READ_TIMEOUT_S = 370
QUALIFIER = "DEFAULT"
# What agent/app.py answers with; anything else is stored as an error.
OUTCOMES = {"done", "failed", "refused", "invalid"}
# A DynamoDB item holds 400 KB. A record past this, gzipped, isn't kept.
MAX_RECORD_GZ_BYTES = 300_000
MAX_TEXT_CHARS = 1_000


def parse_change(event: Mapping[str, Any]) -> Change | None:
    """The alarm change in an EventBridge event, or None for anything else."""
    if event.get("source") != "aws.cloudwatch" or event.get("detail-type") != "CloudWatch Alarm State Change":
        return None
    detail = event.get("detail") or {}
    state = detail.get("state") or {}
    name, value, timestamp = detail.get("alarmName"), state.get("value"), state.get("timestamp")
    if not isinstance(name, str) or value not in (ALARM, OK) or not isinstance(timestamp, str):
        return None
    try:
        at = parse_time(timestamp)
    except ValueError:
        return None
    return Change(name, value, at)


class AgentRuntime:
    """The agent on AgentCore Runtime, one investigation per session."""

    def __init__(self, client: Any, runtime_arn: str) -> None:
        self._client = client
        self._arn = runtime_arn

    def investigate(self, alarm: str, session_id: str) -> Any:
        """One live investigation of the alarm: the app's answer, parsed (agent/app.py)."""
        response = self._client.invoke_agent_runtime(
            agentRuntimeArn=self._arn,
            qualifier=QUALIFIER,
            runtimeSessionId=session_id,
            contentType="application/json",
            accept="application/json",
            payload=json.dumps({"alarm": alarm}).encode(),
        )
        return json.loads(response["response"].read())

    def stop(self, session_id: str) -> str:
        """End the session: "stopped", or the error, such as ResourceNotFoundException once it has ended."""
        try:
            self._client.stop_runtime_session(
                agentRuntimeArn=self._arn, qualifier=QUALIFIER, runtimeSessionId=session_id
            )
        except (ClientError, BotoCoreError) as e:
            return error_name(e)
        return "stopped"


class Trigger:
    def __init__(
        self,
        store: Store,
        runtime: AgentRuntime,
        *,
        clock: Callable[[], float] = time.time,
        session_id: Callable[[Incident], str] | None = None,
    ) -> None:
        """`clock` and `session_id` are for tests."""
        self._store = store
        self._runtime = runtime
        self._clock = clock
        self._session_id = session_id or new_session_id

    def handle(self, event: Mapping[str, Any]) -> dict[str, Any]:
        change = parse_change(event)
        service = None if change is None else service_for(change.alarm)
        if change is None or service is None:
            return log({"event": "ignored", "id": event.get("id"), "detail_type": event.get("detail-type")})

        result = self._store.triage(service, change)
        line = log(
            {
                "event": "alarm_change",
                "alarm": change.alarm,
                "state": change.state,
                "at": iso(change.at),
                "verdict": result.verdict.value,
                "incident": None if result.incident is None else result.incident.id,
            }
        )
        if result.verdict is not Verdict.NEW or result.incident is None:
            return line
        return self._investigate(service, result.incident, change.alarm)

    def _investigate(self, service: str, incident: Incident, alarm: str) -> dict[str, Any]:
        session_id = self._session_id(incident)
        if not self._store.lock(session_id, self._clock()):
            return self._finish(service, incident, {"status": "busy"})
        try:
            if not self._store.take_cap_slot(self._clock()):
                return self._finish(service, incident, {"status": "capped"})
            started = self._clock()
            self._store.update_incident(
                service,
                incident.id,
                {"status": "investigating", "session_id": session_id, "invoked_at": iso_epoch(started)},
            )
            # The session to stop, for the kill switch, while the run is going.
            log({"event": "investigating", "incident": incident.id, "alarm": alarm, "session_id": session_id})
            try:
                fields = answer_fields(self._runtime.investigate(alarm, session_id))
            except (ClientError, BotoCoreError, ValueError) as e:
                fields = {"status": "error", "error": error_name(e)}
            finally:
                stopped = self._runtime.stop(session_id)
            ended = self._clock()
            fields |= {
                "session_id": session_id,
                "session_stop": stopped,
                "finished_at": iso_epoch(ended),
                "invoke_s": round(ended - started, 1),
            }
            return self._finish(service, incident, fields)
        finally:
            self._store.unlock(session_id)

    def _finish(self, service: str, incident: Incident, fields: dict[str, Any]) -> dict[str, Any]:
        self._store.update_incident(service, incident.id, fields)
        shown = {k: v for k, v in fields.items() if k not in ("hypothesis", "record_gz")}
        # The diagnosis itself, so it reads in CloudWatch as well as on the incident.
        hypothesis = json.loads(fields["hypothesis"]) if "hypothesis" in fields else None
        return log({"event": "investigation", "incident": incident.id, **shown, "hypothesis": hypothesis})


def answer_fields(answer: Any) -> dict[str, Any]:
    """What the incident keeps of the runtime's answer: its outcome, the Hypothesis, the
    run's numbers, and the whole record, gzipped."""
    if not isinstance(answer, dict):
        return {"status": "error", "error": "unexpected answer"}
    outcome = answer.get("outcome")
    fields: dict[str, Any] = {"status": outcome if outcome in OUTCOMES else "error", "outcome": str(outcome)}
    if answer.get("hypothesis") is not None:
        fields["hypothesis"] = json.dumps(answer["hypothesis"])
    for name in ("reason", "failure"):
        if isinstance(answer.get(name), str):
            fields[name] = answer[name][:MAX_TEXT_CHARS]
    for name in ("input_tokens", "cost_usd", "seconds"):
        value = answer.get(name)
        if isinstance(value, int | float) and not isinstance(value, bool):
            fields[name] = value
    if isinstance(answer.get("tool_calls"), list):
        fields["tool_calls"] = [str(t) for t in answer["tool_calls"]]
    if isinstance(answer.get("model_calls"), list):
        fields["model_calls"] = len(answer["model_calls"])
    record = json.dumps(answer, separators=(",", ":"), default=str).encode()
    packed = gzip.compress(record, mtime=0)
    fields["record_bytes"] = len(record)
    if len(packed) <= MAX_RECORD_GZ_BYTES:
        fields["record_gz"] = packed
    return fields


def new_session_id(incident: Incident) -> str:
    """A fresh session for each investigation, named for its incident: 62 characters, past AgentCore's 33."""
    return f"{incident.id}-{uuid.uuid4().hex}"


def error_name(e: Exception) -> str:
    """An AWS error's code, or the exception's type: never the message, which can name an ARN."""
    if isinstance(e, ClientError):
        return e.response.get("Error", {}).get("Code") or "ClientError"
    return type(e).__name__


def iso_epoch(seconds: float) -> str:
    return iso(datetime.fromtimestamp(seconds, UTC))


def log(line: dict[str, Any]) -> dict[str, Any]:
    """One JSON line to stdout, which Lambda passes to CloudWatch unchanged."""
    print(json.dumps(line, default=str), flush=True)
    return line
