"""The agent's trace: what a live run did, step by step, for the page.

BUILD_PLAN.md, Phase 5 plan, "The live trace"; the format is
sentinelops_core.trace's. Trace's hooks see each model call and tool call, as
the guard's do, and a single worker thread writes each step to the trace
table, in order. It's best-effort: a write has a short timeout and no retry,
a failure is logged by its type and counted, and nothing here ever raises into
the run or counts against a limit. After 3 failed writes in a row it stops
writing for the run. The hooks only read what Strands hands them, so nothing
the model is sent changes (the CI gate holds the requests to the baseline's).

Every text (a thought, the agent's words, a tool's result, a tool's argument)
is capped and scanned with the remediation flow's rules: the commit hooks'
(.githooks/commit_scan.py, the same file the flow ships) plus no ARN. A field
with a finding is written as "[withheld: <rules>]", never what matched.

Steps builds the events from what the run hands its hooks, and nothing else:
backfill_trace.py builds the recorded run's from its stored record with the
same Steps and writes them with the same Writer.
"""

import json
import logging
import queue
import re
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import commit_scan  # .githooks/commit_scan.py: on pytest's pythonpath here, staged beside our modules in the Runtime
from strands.hooks import AfterModelCallEvent, AfterToolCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry

from hooks import usage_of
from pricing import ModelPrice
from sentinelops_core import trace as fmt
from sentinelops_core.models import Hypothesis, StopReason

logger = logging.getLogger("sentinelops.trace")

ACCOUNT_ENV = "SENTINELOPS_ACCOUNT_ID"
ANSWER_TOOL = Hypothesis.__name__
# The flow's rule beside the hooks' (remediation/sentinelops_remediation/validate.py, ARN).
ARN = re.compile(r"\barn:aws[a-z-]*:", re.ASCII)
_LABEL = re.compile(r"\[source ([a-z][1-9][0-9]{0,2})\]")
ALARM_PREFIX = "sentinelops-checkout-"
ALARMS: dict[str, fmt.AlarmKey] = {"errors": "errors", "latency": "latency", "pool-wait": "pool_wait"}
HALTS: dict[StopReason, fmt.HaltReason] = {
    StopReason.STEP_LIMIT: "tool_calls",
    StopReason.TOKEN_LIMIT: "input_tokens",
    StopReason.COST_LIMIT: "cost",
    StopReason.WALL_CLOCK: "wall_clock",
    StopReason.RESEND_LIMIT: "resends",
}
CITATION_KEPT = "{runbook} was shown in full in this run, so the citation stands."
CITATION_DROPPED = {
    "cut short": "The cited runbook was cut short in this run's results, so the citation was cleared.",
    "only listed": "The cited runbook was only listed by name in this run, so the citation was cleared.",
    "never shown": "The cited runbook was never shown in this run, so the citation was cleared.",
}
PATCH_KEPT = "{edits} to {file} at {version}: +{added} -{removed} lines, applies exactly once, compiles."
PATCH_DROPPED = "The proposed patch failed its checks against the file this run read, so it was dropped."
# A write's limits: no retry, and a run stops writing after this many failures in a row.
CONNECT_TIMEOUT_S = 1.0
READ_TIMEOUT_S = 2.0
FAILURES_TO_STOP = 3
CLOSE_TIMEOUT_S = 5.0

Stamped = tuple[datetime, fmt.EventModel]


def now() -> datetime:
    return datetime.now(UTC)


class Cleaner:
    """Caps a text and scans it with the flow's rules; a finding withholds the whole field."""

    def __init__(self, account_id: str) -> None:
        if not re.fullmatch(r"[0-9]{12}", account_id):
            raise ValueError("not a 12-digit account ID")
        self._account_id = account_id
        self.withheld = 0

    def text(self, name: str, text: str, cap: int = fmt.TEXT_CHARS) -> tuple[str, bool]:
        """(text, truncated): the first cap characters, or "[withheld: <rules>]"."""
        truncated = len(text) > cap
        if truncated:
            text = text[:cap]
        rules = sorted({f.rule for f in commit_scan.scan_text(name, text, self._account_id)})
        if ARN.search(text):
            rules.append("arn")
        if rules:
            self.withheld += 1
            return f"[withheld: {', '.join(rules)}]", truncated
        return text, truncated


class Steps:
    """The run's events from what its hooks see: each step numbered and its text cleaned.

    Call ids (t1, t2, ...) count the tool calls; the answer tool's calls aren't
    steps. Each method returns its events with their times, never raises on
    an unexpected shape (it skips what it can't read), and keeps only typed
    fields and cleaned text.
    """

    def __init__(self, cleaner: Cleaner, price: ModelPrice) -> None:
        self._clean = cleaner
        self._price = price
        self._calls: dict[str, str] = {}  # toolUseId -> t<n>
        self._model_calls = 0
        self.answered_at: datetime | None = None

    def started(self, at: datetime, alarm_name: str) -> list[Stamped]:
        key = ALARMS.get(alarm_name.removeprefix(ALARM_PREFIX)) if alarm_name.startswith(ALARM_PREFIX) else None
        return [] if key is None else [(at, fmt.RunStarted(alarm=key))]

    def model_call(self, at: datetime, message: Mapping[str, Any]) -> list[Stamped]:
        """The call's counts, then its thinking and words, in the order it gave them."""
        usage = usage_of(message)
        self._model_calls += 1
        n = self._model_calls
        out: list[Stamped] = [
            (
                at,
                fmt.ModelCall(
                    n=n,
                    input_tokens=usage.prompt_tokens,
                    output_tokens=usage.output_tokens,
                    cost_usd=round(self._price.cost(usage), 6),
                ),
            )
        ]
        for i, block in enumerate(message.get("content") or []):
            if not isinstance(block, dict):
                continue
            if "reasoningContent" in block:
                text = ((block["reasoningContent"] or {}).get("reasoningText") or {}).get("text")
                if isinstance(text, str) and text.strip():
                    out.append((at, fmt.Thinking(text=self._clean.text(f"thinking {n}.{i}", text.strip())[0])))
            elif isinstance(block.get("text"), str) and block["text"].strip():
                out.append((at, fmt.Said(text=self._clean.text(f"said {n}.{i}", block["text"].strip())[0])))
        return out

    def tool_call(self, at: datetime, tool_use: Mapping[str, Any]) -> list[Stamped]:
        name, use_id = tool_use.get("name"), tool_use.get("toolUseId")
        if name == ANSWER_TOOL:
            self.answered_at = at  # the last one is the answer that stood
            return []
        if not isinstance(use_id, str) or name not in fmt.ToolName.__args__:  # type: ignore[attr-defined]
            return []
        call = f"t{len(self._calls) + 1}"
        self._calls[use_id] = call
        args: dict[str, str | int | float | bool] = {}
        for key, value in (tool_use.get("input") or {}).items():
            if not isinstance(key, str) or len(key) > 40:
                continue
            if isinstance(value, bool | int | float):
                args[key] = value
            else:
                text = value if isinstance(value, str) else json.dumps(value, default=str)
                args[key] = self._clean.text(f"{call} {key}", text, fmt.ARG_CHARS)[0]
        return [(at, fmt.ToolCall(call=call, tool=name, args=args))]

    def tool_result(self, at: datetime, result: Mapping[str, Any]) -> list[Stamped]:
        call = self._calls.get(str(result.get("toolUseId")))
        if call is None:
            return []
        text = "".join(c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict))
        source = ""
        first, newline, rest = text.partition("\n")
        if newline and (label := _LABEL.fullmatch(first)):
            source, text = label[1], rest
        shown, truncated = self._clean.text(f"{call} result", text)
        return [
            (
                at,
                fmt.ToolResult(
                    call=call,
                    source=source,
                    chars=len(text),
                    text=shown,
                    truncated=truncated,
                    error=result.get("status") == "error",
                ),
            )
        ]

    def finished(
        self,
        at: datetime,
        hypothesis: Hypothesis | None,
        citation_dropped: Mapping[str, str] | None,
        remediation_dropped: Mapping[str, str] | None,
    ) -> list[Stamped]:
        """How the run ended: answered and its checks, or halted. A failed run adds nothing."""
        if hypothesis is None:
            return []
        # Shown, not gated on: whether a PR opens is can_open_pr's alone.
        halt = HALTS.get(hypothesis.halted_by) if hypothesis.halted_by else None
        if halt:
            return [(at, fmt.Halted(by=halt))]
        out: list[Stamped] = [(self.answered_at or at, fmt.Answered(confidence=hypothesis.confidence))]
        if citation_dropped is not None:
            detail = CITATION_DROPPED.get(str(citation_dropped.get("reason")), CITATION_DROPPED["never shown"])
            out.append((at, fmt.Check(check="citation", kept=False, detail=detail)))
        elif hypothesis.runbook_cited:
            runbook = self._clean.text("citation", hypothesis.runbook_cited, 100)[0]
            out.append((at, fmt.Check(check="citation", kept=True, detail=CITATION_KEPT.format(runbook=runbook))))
        patch = hypothesis.remediation
        if remediation_dropped is not None:
            out.append((at, fmt.Check(check="patch", kept=False, detail=PATCH_DROPPED)))
        elif patch is not None and patch.diff is not None:
            lines = patch.diff.splitlines()
            added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
            removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
            edits = f"{len(patch.edits)} edit" + ("" if len(patch.edits) == 1 else "s")
            detail = PATCH_KEPT.format(
                edits=edits,
                file=self._clean.text("file", patch.file_path, 100)[0],
                version=self._clean.text("version", str(patch.version), 20)[0],
                added=added,
                removed=removed,
            )
            out.append((at, fmt.Check(check="patch", kept=True, detail=detail)))
        return out


class Writer:
    """Writes one run's events in order, from one worker thread, so the run never waits on a write."""

    def __init__(
        self,
        client: Any,
        table: str,
        incident_id: str,
        *,
        expires: bool = True,
        background: bool = True,
    ) -> None:
        if not fmt.INCIDENT_ID.fullmatch(incident_id):
            raise ValueError("not an incident's ID")
        self._client = client
        self._table = table
        self._incident = incident_id
        self._expires = expires
        self._n = 0
        self._failed_in_a_row = 0
        self.written = self.failed = self.skipped = 0
        self._queue: queue.SimpleQueue[tuple[str, Stamped] | None] = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._work, name="trace-writer", daemon=True) if background else None
        if self._thread is not None:
            self._thread.start()

    def put(self, events: list[Stamped]) -> None:
        """Queue the events, each with the next key. Never raises, never waits on a write."""
        for stamped in events:
            self._n += 1
            if self._n > 9999:
                self.skipped += 1
                continue
            job = (fmt.agent_key(self._n), stamped)
            if self._thread is None:
                self._write(job)
            else:
                self._queue.put(job)

    def close(self, timeout: float = CLOSE_TIMEOUT_S) -> dict[str, int]:
        """Wait up to timeout for the queued writes; what's left is never written. Returns the counts."""
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join(timeout)
        counts = {"written": self.written, "failed": self.failed, "skipped": self.skipped, "queued": self._n}
        _log("trace_closed", incident=self._incident, **counts)
        return counts

    def _work(self) -> None:
        while (job := self._queue.get()) is not None:
            self._write(job)

    def _write(self, job: tuple[str, Stamped]) -> None:
        key, (at, event) = job
        if self._failed_in_a_row >= FAILURES_TO_STOP:
            self.skipped += 1
            return
        try:
            fmt.put(self._client, self._table, self._incident, key, at, event, expires=self._expires)
        except Exception as error:  # noqa: BLE001 - best-effort: any failure is counted, never raised
            self.failed += 1
            self._failed_in_a_row += 1
            # The type only: an AWS error's message can name an ARN.
            _log("trace_write_failed", level=logging.WARNING, key=key, error=_error_name(error))
            return
        self.written += 1
        self._failed_in_a_row = 0


class Trace:
    """One live run's trace: Steps for the events, a Writer for the table, and the hooks that feed them."""

    def __init__(self, steps: Steps, writer: Writer, clock: Callable[[], datetime] = now) -> None:
        self.steps = steps
        self.writer = writer
        self._clock = clock
        self.hooks = _TraceHooks(self)

    def started(self, alarm_name: str) -> None:
        self._safely("started", lambda: self.steps.started(self._clock(), alarm_name))

    def finished(self, report: Any) -> None:
        """The end of the run, from its RunReport: answered and the checks, or halted."""
        self._safely(
            "finished",
            lambda: self.steps.finished(
                self._clock(), report.hypothesis, report.citation_dropped, report.remediation_dropped
            ),
        )

    def close(self) -> dict[str, int]:
        try:
            return self.writer.close()
        except Exception as error:  # noqa: BLE001
            _log("trace_failed", level=logging.WARNING, step="close", error=type(error).__name__)
            return {}

    def _safely(self, step: str, build: Callable[[], list[Stamped]]) -> None:
        try:
            self.writer.put(build())
        except Exception as error:  # noqa: BLE001 - nothing here may stop or change the run
            _log("trace_failed", level=logging.WARNING, step=step, error=type(error).__name__)


class _TraceHooks(HookProvider):
    """Reads the run's events and hands them to the trace. Every callback swallows its own errors."""

    def __init__(self, trace: Trace) -> None:
        self._trace = trace

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(AfterModelCallEvent, self.after_model_call)
        registry.add_callback(BeforeToolCallEvent, self.before_tool_call)
        registry.add_callback(AfterToolCallEvent, self.after_tool_call)

    def after_model_call(self, event: AfterModelCallEvent) -> None:
        if event.exception is not None or event.stop_response is None:
            return
        message = event.stop_response.message
        self._trace._safely("model_call", lambda: self._trace.steps.model_call(self._trace._clock(), message))

    def before_tool_call(self, event: BeforeToolCallEvent) -> None:
        tool_use = dict(event.tool_use)
        self._trace._safely("tool_call", lambda: self._trace.steps.tool_call(self._trace._clock(), tool_use))

    def after_tool_call(self, event: AfterToolCallEvent) -> None:
        result = event.result
        if not isinstance(result, dict):
            return
        self._trace._safely("tool_result", lambda: self._trace.steps.tool_result(self._trace._clock(), result))


def open_trace(environ: Mapping[str, str], session: Any, incident_id: str, price: ModelPrice) -> Trace | None:
    """A live run's trace, or None (logged) when the table or account isn't set: the run goes on without one."""
    table, account_id = environ.get(fmt.TABLE_ENV), environ.get(ACCOUNT_ENV)
    if not table or not account_id:
        _log("trace_off", incident=incident_id, reason="the trace table or the account ID isn't set")
        return None
    try:
        from botocore.config import Config

        client = session.client(
            "dynamodb",
            config=Config(
                connect_timeout=CONNECT_TIMEOUT_S,
                read_timeout=READ_TIMEOUT_S,
                retries={"total_max_attempts": 1, "mode": "standard"},
            ),
        )
        return Trace(Steps(Cleaner(account_id), price), Writer(client, table, incident_id))
    except Exception as error:  # noqa: BLE001
        _log("trace_off", level=logging.WARNING, incident=incident_id, reason=type(error).__name__)
        return None


def _error_name(error: Exception) -> str:
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        if isinstance(code, str) and code:
            return code
    return type(error).__name__


def _log(event: str, level: int = logging.INFO, **fields: object) -> None:
    logger.log(level, json.dumps({"event": event, **fields}))

