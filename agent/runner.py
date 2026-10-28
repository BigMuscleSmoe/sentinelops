"""One investigation: the Strands agent loop, under its guard."""

import json
import logging
import time
from collections import Counter
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from guards import Limits, RunGuard, guarded_tool, run_guarded
from hooks import GuardHooks, ModelCall, ModelRefused
from models import Hypothesis
from pricing import ModelPrice

logger = logging.getLogger("sentinelops.runner")

PROMPT_FILE = Path(__file__).parent / "prompts" / "system.md"
MAX_FAILURE_CHARS = 500


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
    tools: list[Callable[..., str]],
    price: ModelPrice,
    max_output_tokens: int,
    limits: Limits = Limits(),
    retrieved_runbooks: Collection[str] = (),
    clock: Callable[[], float] = time.monotonic,
) -> RunReport:
    """Run the agent on one alarm's prompt and return what it found, and what it spent.

    A limit never raises: it ends the run with a halted Hypothesis. Anything
    else that stops the run, a refusal included, is reported as a failure
    with no Hypothesis.
    """
    from strands import Agent, ModelRetryStrategy, tool
    from strands.agent.conversation_manager import NullConversationManager
    from strands.middleware import InvokeModelStage
    from strands.tools.executors import SequentialToolExecutor

    guard = RunGuard(limits, clock=clock)
    hooks = GuardHooks(guard, price, max_output_tokens)
    agent = Agent(
        model=model,
        tools=[tool(guarded_tool(guard, fn)) for fn in tools],
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

    async def work() -> Hypothesis:
        result = await agent.invoke_async(prompt, structured_output_model=Hypothesis)
        if not isinstance(result.structured_output, Hypothesis):
            raise RuntimeError("The run ended without a Hypothesis.")
        return result.structured_output

    hypothesis: Hypothesis | None = None
    failure: str | None = None
    try:
        hypothesis = await run_guarded(guard, work)
    except Exception as e:
        failure = describe_failure(e)
        logger.warning(json.dumps({"event": "run_failed", "failure": failure}))

    if hypothesis is not None:
        checked = hypothesis.with_citation_checked(retrieved_runbooks)
        if checked is not hypothesis:
            logger.warning(
                json.dumps(
                    {
                        "event": "citation_dropped",
                        "cited": (hypothesis.runbook_cited or "")[:200],
                        "reason": "no runbook search in this run returned it",
                    }
                )
            )
        hypothesis = checked

    return RunReport(
        hypothesis=hypothesis,
        failure=failure,
        tool_calls=list(guard.tool_calls),
        model_calls=list(hooks.calls),
        input_tokens=guard.input_tokens,
        cost_usd=guard.cost_usd,
        seconds=guard.elapsed_s,
        messages=agent.messages,
    )


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
