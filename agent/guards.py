"""Hard limits on a single investigation.

Enforced here, in code. The system prompt tells the agent its budget so it can
plan, but nothing depends on the model respecting it.

The agent loop calls the before_*/after_* hooks; run_guarded wraps the whole
loop. A tripped limit unwinds the loop with GuardTripped, and run_guarded turns
that, or the wall-clock deadline, into a PartialReport. Callers of run_guarded
never see a limit as an exception.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from models import PartialReport, StopReason


@dataclass(frozen=True)
class Limits:
    max_tool_calls: int = 15
    max_input_tokens: int = 200_000
    max_wall_clock_s: float = 300.0


class GuardTripped(Exception):
    """Unwinds the agent loop when a limit is hit. Never escapes run_guarded."""

    def __init__(self, reason: StopReason):
        super().__init__(reason.value)
        self.reason = reason


class Guards:
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
        self.stop_reason: StopReason | None = None

    @property
    def elapsed_s(self) -> float:
        return self._clock() - self._started_at

    @property
    def remaining_s(self) -> float:
        return max(0.0, self.limits.max_wall_clock_s - self.elapsed_s)

    def before_model_call(self) -> None:
        self._check()

    def after_model_call(self, input_tokens: int) -> None:
        if input_tokens < 0:
            raise ValueError(f"input_tokens must be non-negative, got {input_tokens}")
        self.input_tokens += input_tokens
        # No check here. This response is already paid for and may be the
        # final answer; the cap stops the *next* call instead.

    def before_tool_call(self, name: str) -> None:
        self._check()
        if len(self.tool_calls) >= self.limits.max_tool_calls:
            self._trip(StopReason.STEP_LIMIT)
        self.tool_calls.append(name)

    def partial_report(self) -> PartialReport:
        assert self.stop_reason is not None, "partial_report() before any limit tripped"
        return PartialReport(
            stop_reason=self.stop_reason,
            tool_calls=list(self.tool_calls),
            input_tokens=self.input_tokens,
            elapsed_s=round(self.elapsed_s, 1),
        )

    def _check(self) -> None:
        if self.stop_reason is not None:
            raise GuardTripped(self.stop_reason)
        if self.elapsed_s >= self.limits.max_wall_clock_s:
            self._trip(StopReason.TIME_LIMIT)
        if self.input_tokens >= self.limits.max_input_tokens:
            self._trip(StopReason.TOKEN_LIMIT)

    def _trip(self, reason: StopReason) -> None:
        # The first limit hit is the one reported.
        if self.stop_reason is None:
            self.stop_reason = reason
        raise GuardTripped(self.stop_reason)


async def run_guarded[T](
    guards: Guards, work: Callable[[], Awaitable[T]]
) -> T | PartialReport:
    """Run one investigation: its result, or a PartialReport if a limit tripped.

    The deadline cancels `work` at its next await point. Blocking code running
    in a worker thread can't be interrupted and is abandoned, not stopped; the
    AgentCore session lifetime is the backstop for that.
    """
    deadline = asyncio.timeout(guards.remaining_s)
    try:
        async with deadline:
            return await work()
    except TimeoutError:
        if not deadline.expired():
            raise  # a timeout inside work, not ours
        if guards.stop_reason is None:
            guards.stop_reason = StopReason.TIME_LIMIT
        return guards.partial_report()
    except Exception:
        # Agent frameworks may wrap exceptions raised in hooks, so don't rely on
        # catching GuardTripped by type: if a limit tripped, that's the outcome.
        if guards.stop_reason is not None:
            return guards.partial_report()
        raise
