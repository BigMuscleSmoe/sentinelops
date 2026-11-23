"""One investigation: the Strands agent loop, under its guard."""

import json
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from guards import Limits, RunGuard, guarded_tool, run_guarded
from hooks import GuardHooks, ModelCall, ModelRefused
from models import Hypothesis
from pricing import ModelPrice
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
) -> RunReport:
    """Run the agent on one alarm's prompt and return what it found, and what it spent.

    A limit never raises: it ends the run with a halted Hypothesis. Anything
    else that stops the run, a refusal included, is reported as a failure
    with no Hypothesis. A cited runbook stays only if a search in this run
    showed it in full (check_citation).
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
        hooks=[hooks],
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

    dropped = None
    if hypothesis is not None:
        hypothesis, dropped = check_citation(hypothesis, sources)

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
