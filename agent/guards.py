"""Hard limits on a single investigation.

Enforced here, in code, and deliberately absent from the system prompt.

The agent loop reports into a RunGuard: tools wrapped with guarded_tool()
count themselves and append the remaining budget to their result,
record_input_tokens() runs after each model call, check() before each model
call. A tripped limit raises a GuardLimitExceeded subclass to unwind the loop;
run_guarded() catches it, or the wall-clock deadline, and returns a halted
Hypothesis instead. Callers of run_guarded never see a limit as an exception.
"""

import asyncio
import functools
import inspect
import json
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from pydantic import BaseModel

from models import Hypothesis, StopReason

logger = logging.getLogger("sentinelops.guards")


@dataclass(frozen=True)
class Limits:
    max_tool_calls: int = 15
    max_input_tokens: int = 200_000
    max_wall_clock_s: float = 300.0


class GuardLimitExceeded(Exception):
    reason: StopReason


class StepLimitExceeded(GuardLimitExceeded):
    reason = StopReason.STEP_LIMIT


class TokenLimitExceeded(GuardLimitExceeded):
    reason = StopReason.TOKEN_LIMIT


class WallClockExceeded(GuardLimitExceeded):
    reason = StopReason.WALL_CLOCK


_EXCEPTIONS: dict[StopReason, type[GuardLimitExceeded]] = {
    StopReason.STEP_LIMIT: StepLimitExceeded,
    StopReason.TOKEN_LIMIT: TokenLimitExceeded,
    StopReason.WALL_CLOCK: WallClockExceeded,
}


class Remaining(BaseModel):
    steps: int
    input_tokens: int
    seconds: float


class RunGuard:
    def __init__(
        self,
        limits: Limits = Limits(),
        clock: Callable[[], float] = time.monotonic,
    ):
        self.limits = limits
        self._clock = clock
        self._started_at = clock()
        self.tool_calls: list[str] = []
        self.input_tokens = 0
        self.tripped: StopReason | None = None

    @property
    def elapsed_s(self) -> float:
        return self._clock() - self._started_at

    @property
    def seconds_left(self) -> float:
        return max(0.0, self.limits.max_wall_clock_s - self.elapsed_s)

    def remaining(self) -> Remaining:
        return Remaining(
            steps=max(0, self.limits.max_tool_calls - len(self.tool_calls)),
            input_tokens=max(0, self.limits.max_input_tokens - self.input_tokens),
            seconds=round(self.seconds_left, 1),
        )

    def budget_line(self) -> str:
        # Lands in context on every tool result, so it stays this short.
        r = self.remaining()
        return f"[budget: {r.steps} steps, {r.input_tokens // 1000}k tokens, {int(r.seconds)}s remaining]"

    def check(self) -> None:
        """Raise if the time or token budget is spent. Call before each model call."""
        if self.tripped is not None:
            self._trip(self.tripped)
        if self.elapsed_s >= self.limits.max_wall_clock_s:
            self._trip(StopReason.WALL_CLOCK)
        if self.input_tokens >= self.limits.max_input_tokens:
            self._trip(StopReason.TOKEN_LIMIT)

    def record_tool_call(self, name: str) -> None:
        """Count a tool call before it runs. Raises instead of counting past the cap."""
        self.check()
        if len(self.tool_calls) >= self.limits.max_tool_calls:
            self._trip(StopReason.STEP_LIMIT)
        self.tool_calls.append(name)
        self._log("tool_call", tool=name)

    def record_input_tokens(self, count: int) -> None:
        """Add one model call's input tokens to the running total.

        Doesn't raise even past the cap: the response is already paid for and
        may be the final answer. The next check() or tool call stops the run.
        """
        if count < 0:
            raise ValueError(f"input token count must be non-negative, got {count}")
        self.input_tokens += count

    def halted_hypothesis(self) -> Hypothesis:
        assert self.tripped is not None, "halted_hypothesis() before any limit tripped"
        return Hypothesis(
            root_cause=f"Not determined: the investigation hit its {self._describe(self.tripped)} before reaching a diagnosis.",
            confidence=0.0,
            evidence=[
                f"Halted by the {self._describe(self.tripped)}. Used {len(self.tool_calls)} of "
                f"{self.limits.max_tool_calls} tool calls, {self.input_tokens:,} of "
                f"{self.limits.max_input_tokens:,} input tokens, {self.elapsed_s:.0f}s of "
                f"{self.limits.max_wall_clock_s:.0f}s.",
                self._summarize_tool_calls(),
            ],
            affected_component="unknown",
            halted_by=self.tripped,
        )

    def _mark_tripped(self, reason: StopReason) -> None:
        # The first limit hit is the one reported, however many trip after it.
        if self.tripped is None:
            self.tripped = reason
            self._log("guard_tripped", reason=reason.value)

    def _trip(self, reason: StopReason) -> None:
        self._mark_tripped(reason)
        raise _EXCEPTIONS[self.tripped](self._describe(self.tripped))

    def _describe(self, reason: StopReason) -> str:
        match reason:
            case StopReason.STEP_LIMIT:
                return f"step limit ({self.limits.max_tool_calls} tool calls)"
            case StopReason.TOKEN_LIMIT:
                return f"input-token limit ({self.limits.max_input_tokens:,} tokens)"
            case StopReason.WALL_CLOCK:
                return f"wall-clock limit ({self.limits.max_wall_clock_s:.0f}s)"

    def _summarize_tool_calls(self) -> str:
        if not self.tool_calls:
            return "No tool calls were made before halting."
        counts = Counter(self.tool_calls)  # keeps first-call order
        return "Tool calls made before halting: " + ", ".join(
            f"{name} x{n}" for name, n in counts.items()
        )

    def _log(self, event: str, **fields: object) -> None:
        logger.info(
            json.dumps(
                {
                    "event": event,
                    **fields,
                    "steps_used": len(self.tool_calls),
                    "input_tokens": self.input_tokens,
                    "elapsed_s": round(self.elapsed_s, 1),
                    "remaining": self.remaining().model_dump(),
                }
            )
        )


def guarded_tool[F: Callable[..., Any]](guard: RunGuard, fn: F) -> F:
    """Wrap a tool so each call is counted first and its result ends with the budget line.

    functools.wraps keeps the signature and docstring, which agent frameworks
    read to build the tool's schema.
    """
    name = fn.__name__

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> str:
            guard.record_tool_call(name)
            return f"{await fn(*args, **kwargs)}\n{guard.budget_line()}"

        return cast(F, async_wrapper)

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        guard.record_tool_call(name)
        return f"{fn(*args, **kwargs)}\n{guard.budget_line()}"

    return cast(F, wrapper)


async def run_guarded(
    guard: RunGuard, work: Callable[[], Awaitable[Hypothesis]]
) -> Hypothesis:
    """Run one investigation. Returns its Hypothesis, or a halted one if a limit tripped.

    The deadline cancels `work` at its next await point. Blocking code running
    in a worker thread can't be interrupted and is abandoned, not stopped; the
    AgentCore session lifetime is the backstop for that.
    """
    deadline = asyncio.timeout(guard.seconds_left)
    try:
        async with deadline:
            result = await work()
    except TimeoutError:
        if not deadline.expired():
            raise  # a timeout inside work, not ours
        guard._mark_tripped(StopReason.WALL_CLOCK)
        return guard.halted_hypothesis()
    except Exception:
        # Agent frameworks may wrap exceptions raised in hooks, so don't rely on
        # catching GuardLimitExceeded by type: if a limit tripped, that's the outcome.
        if guard.tripped is not None:
            return guard.halted_hypothesis()
        raise

    # Frameworks commonly catch tool exceptions and hand them to the model as
    # error results, so a tripped limit can be swallowed and the model can still
    # return a confident answer. The guard's record, not the loop's exit, decides.
    if guard.tripped is not None:
        return guard.halted_hypothesis()
    return result
