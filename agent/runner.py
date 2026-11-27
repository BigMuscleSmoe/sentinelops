"""One investigation: the Strands agent loop, under its guard."""

import json
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from guards import Limits, RunGuard, guarded_tool, run_guarded
from hooks import GuardHooks, ModelCall, ModelRefused
from pricing import ModelPrice
from sentinelops_core import code, patches
from sentinelops_core.models import Hypothesis
from sentinelops_tools.deploys import parse_history
from sentinelops_tools.runbooks import parse_results
from sources import Sources, cited

logger = logging.getLogger("sentinelops.runner")

PROMPT_FILE = Path(__file__).parent / "prompts" / "system.md"
MAX_FAILURE_CHARS = 500
# What the model is sent when it gives its answer as text. Only a model that
# can't be forced to call a tool can: asked for its answer with tool_choice auto
# (providers.StrictAnswerModel), it may still reply in text. An answer is a call
# to the answer tool, so text is refused, whole or not, and counts as a resend.
ANSWER_AS_TEXT = (
    "No answer was recorded: your reply was text, and an answer is only taken from a call to the "
    "Hypothesis tool. Call the Hypothesis tool now with what you found."
)


def system_prompt() -> str:
    return PROMPT_FILE.read_text(encoding="utf-8")


@dataclass
class RunReport:
    hypothesis: Hypothesis | None
    failure: str | None
    tool_calls: list[str]
    model_calls: list[ModelCall]
    input_tokens: int
    cost_usd: float
    seconds: float
    messages: list[Any] = field(repr=False)
    # The text cut off each answer the guard trimmed, not resent (models.trimmed_answer).
    trims: list[str] = field(default_factory=list)
    # Answers given as text and asked for again as a tool call (ANSWER_AS_TEXT).
    answers_in_text: int = 0
    # The tools called after the run was first asked for its answer; None if it
    # never was (hooks.GuardHooks.tool_calls_after_ask).
    tool_calls_after_ask: list[str] | None = None
    # The runbook the answer cited and why it was cleared, if it was (check_citation).
    citation_dropped: dict[str, str] | None = None
    # The patch the answer proposed and why it was cleared, if it was (check_remediation).
    remediation_dropped: dict[str, str] | None = None

    def summary(self) -> str:
        tools = ", ".join(f"{name} x{n}" for name, n in Counter(self.tool_calls).items()) or "none"
        return (
            f"tool calls: {tools} | model calls: {len(self.model_calls)} | "
            f"input tokens: {self.input_tokens:,} | cost: ${self.cost_usd:.4f} | {self.seconds:.0f} s"
        )


async def investigate(
    prompt: str,
    *,
    model: Any,
    tools: list[Callable[..., str]] | list[Callable[..., Awaitable[str]]],
    price: ModelPrice,
    max_output_tokens: int,
    limits: Limits = Limits(),
    clock: Callable[[], float] = time.monotonic,
    fired_at: datetime | None = None,
    observers: Sequence[Any] = (),
) -> RunReport:
    """Run the agent on one alarm's prompt and return what it found, and what it spent.

    A limit never raises: it ends the run with a halted Hypothesis. Anything
    else that stops the run, a refusal included, is reported as a failure
    with no Hypothesis. A cited runbook stays only if a search in this run
    showed it in full (check_citation), and a patch only if it passes its
    checks against a file this run read in full (check_remediation). The
    answer's running_version is the release running at fired_at, as the run's
    own deploy-history reads show it (running_release); without fired_at it's
    None, and no answer can open a PR. observers are more hook providers, which
    only read the run's events (the live trace's, run_trace.Trace.hooks).
    """
    from strands import Agent, ModelRetryStrategy, tool
    from strands.agent.conversation_manager import NullConversationManager
    from strands.middleware import InvokeModelStage
    from strands.tools.executors import SequentialToolExecutor
    from strands.types.exceptions import StructuredOutputException

    guard = RunGuard(limits, clock=clock)
    hooks = GuardHooks(guard, price, max_output_tokens)
    sources = Sources()
    agent = Agent(
        model=model,
        # Counted first, then run and labeled as a source, then given the budget line.
        tools=[tool(guarded_tool(guard, cited(sources, fn))) for fn in tools],
        system_prompt=system_prompt(),
        callback_handler=None,
        # Nothing trims, summarizes, or offloads context behind the guard's back:
        # every token the model is sent comes from this loop.
        conversation_manager=NullConversationManager(),
        context_manager=False,
        tool_executor=SequentialToolExecutor(),
        # Only throttling is retried; the guard sees each retry as a new call.
        retry_strategy=ModelRetryStrategy(max_attempts=3, initial_delay=2, max_delay=8),
        hooks=[hooks, *observers],
    )
    # Each call's cost is reserved, or the call refused, on the request about to go out.
    agent.add_middleware(InvokeModelStage.Input, hooks.before_send)

    answers_in_text = 0

    async def work() -> Hypothesis:
        nonlocal answers_in_text
        ask = prompt
        while True:
            try:
                result = await agent.invoke_async(ask, structured_output_model=Hypothesis)
            except StructuredOutputException:
                # Asked for its answer, the model replied in text that is no Hypothesis.
                pass
            else:
                if not isinstance(result.structured_output, Hypothesis):
                    raise RuntimeError("The run ended without a Hypothesis.")
                if calls_the_answer_tool(result.message):
                    return result.structured_output
                # Text that is a whole Hypothesis, which Strands takes: not from the
                # strict tool, and never trimmed, so it's asked for again like any other.
            answers_in_text += 1
            logger.info(json.dumps({"event": "answer_in_text", "count": answers_in_text}))
            # Counted with the answers that failed validation: past the cap, check() ends the run.
            guard.record_rejected_answer()
            guard.check()
            ask = ANSWER_AS_TEXT

    hypothesis: Hypothesis | None = None
    failure: str | None = None
    try:
        hypothesis = await run_guarded(guard, work)
    except Exception as e:
        failure = describe_failure(e)
        logger.warning(json.dumps({"event": "run_failed", "failure": failure}))

    dropped = patch_dropped = None
    if hypothesis is not None:
        hypothesis, dropped = check_citation(hypothesis, sources)
        hypothesis, patch_dropped = check_remediation(hypothesis, sources)
        # Always written over: the model's schema leaves it out, and only the run's reads say it.
        hypothesis = hypothesis.model_copy(update={"running_version": running_release(sources, fired_at)})

    return RunReport(
        hypothesis=hypothesis,
        failure=failure,
        tool_calls=list(guard.tool_calls),
        model_calls=list(hooks.calls),
        input_tokens=guard.input_tokens,
        cost_usd=guard.cost_usd,
        seconds=guard.elapsed_s,
        messages=agent.messages,
        trims=list(guard.trims),
        answers_in_text=answers_in_text,
        tool_calls_after_ask=hooks.tool_calls_after_ask,
        citation_dropped=dropped,
        remediation_dropped=patch_dropped,
    )


def check_citation(hypothesis: Hypothesis, sources: Sources) -> tuple[Hypothesis, dict[str, str] | None]:
    """The answer with runbook_cited kept only if a search in this run showed that runbook in full.

    The model can name any file; only the run's own results say what it read.
    A runbook only listed by name, or cut short by the result's cap, wasn't
    read whole. A citation that fails is cleared and logged, never sent back.
    """
    in_full: set[str] = set()
    cut_short: set[str] = set()
    listed: set[str] = set()
    for tool, text in sources.results:
        page = parse_results(text) if tool == "search_runbooks" else None
        if page is not None:
            in_full.update(page.in_full)
            cut_short.update(set(page.shown) - set(page.in_full))
            listed.update(page.listed)
    checked = hypothesis.with_citation_checked(in_full)
    if checked is hypothesis:
        return hypothesis, None
    cited = hypothesis.runbook_cited or ""
    reason = "cut short" if cited in cut_short else "only listed" if cited in listed else "never shown"
    dropped = {"cited": cited[:200], "reason": reason}
    logger.warning(json.dumps({"event": "citation_dropped", **dropped}))
    return checked, dropped


def check_remediation(hypothesis: Hypothesis, sources: Sources) -> tuple[Hypothesis, dict[str, str] | None]:
    """The answer with its patch kept only if it passes every check against a file this run read in full.

    The model can write any edits; only the run's own results say what it read.
    A patch that fails is cleared and logged with the reason, never sent back
    (Phase 4 design, decision 7), so a bad patch can't cost the diagnosis a
    resend. One that passes gets the fields only code sets: the release the read
    showed, the file's hash, and the diff. Nothing in it is imported or run.
    """
    remediation = hypothesis.remediation
    if remediation is None:
        return hypothesis, None
    try:
        read = _file_read(sources, remediation.source)
        if remediation.file_path != read.path:
            raise patches.Refused(f"file_path isn't the file {remediation.source} shows")
        if not remediation.rationale.strip():
            raise patches.Refused("no rationale")
        checked = patches.apply_edits(read.path, read.text, [(e.old, e.new) for e in remediation.edits])
    except patches.Refused as refused:
        dropped = {"source": remediation.source[:20], "file_path": remediation.file_path[:200], "reason": str(refused)}
        logger.warning(json.dumps({"event": "remediation_dropped", **dropped}))
        return hypothesis.model_copy(update={"remediation": None}), dropped
    kept = remediation.model_copy(
        update={"version": read.version, "file_sha256": code.digest(read.text), "diff": checked.diff}
    )
    return hypothesis.model_copy(update={"remediation": kept}), None


def running_release(sources: Sources, fired_at: datetime | None) -> str | None:
    """The release running when the alarm fired, from this run's deploy-history reads, or None.

    Every record carries the version running after it, a config change's too,
    so it's the version on the newest record at or before fired_at. A live read
    runs past fired_at, and what it shows after (a rollback, say) is left out. A
    page that ends before fired_at could miss a later record, so it doesn't
    count; one whose records all came after fired_at shows nothing that does.
    """
    if fired_at is None:
        return None
    newest: tuple[datetime, str] | None = None
    for tool, text in sources.results:
        page = parse_history(text) if tool == "get_deploy_history" else None
        up_to = _utc(page.up_to) if page is not None else None
        if page is None or up_to is None or up_to < fired_at:
            continue
        for record in page.records:
            ts, version = _utc(record.get("ts")), record.get("version")
            if ts is None or ts > fired_at or not isinstance(version, str) or not version:
                continue
            if newest is None or ts > newest[0]:
                newest = (ts, version)
    return newest[1] if newest is not None else None


def _utc(value: Any) -> datetime | None:
    """An ISO 8601 time with its zone, as the deploy tool writes them, or None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _file_read(sources: Sources, label: str) -> code.FileRead:
    """The whole file the run's result `label` showed, or Refused saying what that label is instead."""
    for found, tool, text in sources.labeled():
        if found != label:
            continue
        if tool != code.READ_TOOL:
            raise patches.Refused(f"{label} isn't a read_repo_file result")
        read = code.parse_file(text)
        if read is None:
            raise patches.Refused(f"{label} isn't a read of one whole file")
        return read
    raise patches.Refused(f"no result {label[:20]} in this run")


def calls_the_answer_tool(message: Any) -> bool:
    """True if the message, the run's last from the model, calls the answer tool."""
    return any(block.get("toolUse", {}).get("name") == Hypothesis.__name__ for block in message.get("content", []))


def describe_failure(error: BaseException) -> str:
    """What stopped the run, from the first cause in the chain that says something."""
    seen = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ModelRefused):
            return str(current)
        current = current.__cause__ or current.__context__
    text = f"{type(error).__name__}: {error}"
    return text if len(text) <= MAX_FAILURE_CHARS else text[:MAX_FAILURE_CHARS] + " ...[cut]"
