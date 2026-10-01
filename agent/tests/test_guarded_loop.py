"""The guard driven through a realistic agent loop, not called directly.

Unit tests prove the guard raises. These prove the halt reaches the caller
whatever the framework does with that exception on the way out: let it
through, wrap it, turn tool errors into messages for the model, or swallow
everything and let the model answer anyway.
"""

import asyncio
from collections.abc import Callable
from enum import Enum, auto

import pytest

from guards import RunGuard, guarded_tool, run_guarded
from models import Hypothesis, StopReason

CONFIDENT_ANSWER = Hypothesis(
    root_cause="Connection pool max size lowered in the latest config change.",
    confidence=0.95,
    evidence=["pool_wait_time_ms p99 at 900ms"],
    affected_component="checkout-api",
)


class ErrorHandling(Enum):
    PROPAGATE = auto()  # exceptions leave the loop untouched
    WRAP = auto()  # re-raised as the framework's own exception type
    TOOL_ERRORS_TO_MODEL = auto()  # tool exceptions become error results; the loop goes on
    SWALLOW_ALL = auto()  # nothing escapes; the model always gets to answer


class FrameworkError(Exception):
    pass


class FakeAgentLoop:
    """Model turn, then the tool call it asked for, until it answers.

    The 'model' follows a script: tool names to call, then a final Hypothesis.
    """

    def __init__(
        self,
        guard: RunGuard,
        tools: dict[str, Callable[[], str]],
        script: list[str | Hypothesis],
        errors: ErrorHandling,
    ):
        self.guard = guard
        self.tools = tools
        self.script = script
        self.errors = errors
        self.transcript: list[str] = []

    async def run(self) -> Hypothesis:
        for step in self.script:
            self._hook(self.guard.check)  # before-model-call hook
            await asyncio.sleep(0)  # the model call
            self.guard.record_input_tokens(4_000)
            if isinstance(step, Hypothesis):
                return step
            self.transcript.append(self._call_tool(step))
        raise AssertionError("script ended without an answer")

    def _hook(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as e:
            if self.errors is ErrorHandling.SWALLOW_ALL:
                return
            if self.errors is ErrorHandling.WRAP:
                raise FrameworkError("hook failed") from e
            raise

    def _call_tool(self, name: str) -> str:
        try:
            return self.tools[name]()
        except Exception as e:
            if self.errors in (ErrorHandling.TOOL_ERRORS_TO_MODEL, ErrorHandling.SWALLOW_ALL):
                return f"Error: {e}"
            if self.errors is ErrorHandling.WRAP:
                raise FrameworkError("tool failed") from e
            raise


def make_tools(guard: RunGuard) -> tuple[dict[str, Callable[[], str]], list[int]]:
    executions = [0]

    def get_recent_logs() -> str:
        executions[0] += 1
        return "TimeoutError: acquiring connection from pool"

    return {"get_recent_logs": guarded_tool(guard, get_recent_logs)}, executions


@pytest.mark.parametrize("errors", list(ErrorHandling), ids=lambda e: e.name.lower())
def test_sixteenth_tool_call_halts_the_run_whatever_the_framework_does(
    errors: ErrorHandling,
) -> None:
    guard = RunGuard()
    tools, executions = make_tools(guard)
    # An agent stuck in a loop, which then claims high confidence anyway.
    loop = FakeAgentLoop(guard, tools, ["get_recent_logs"] * 16 + [CONFIDENT_ANSWER], errors)

    result = asyncio.run(run_guarded(guard, loop.run))

    assert result.halted_by is StopReason.STEP_LIMIT
    assert result.confidence == 0.0
    assert not result.can_open_pr
    assert executions[0] == 15, "the 16th call must be refused before the tool runs"


def test_budget_line_reaches_the_model_on_every_tool_result() -> None:
    guard = RunGuard()
    tools, _ = make_tools(guard)
    loop = FakeAgentLoop(guard, tools, ["get_recent_logs"] * 3 + [CONFIDENT_ANSWER], ErrorHandling.PROPAGATE)

    asyncio.run(run_guarded(guard, loop.run))

    steps_left = [line.splitlines()[-1].split(",")[0] for line in loop.transcript]
    assert steps_left == ["[budget: 14 steps", "[budget: 13 steps", "[budget: 12 steps"]


def test_a_run_that_finishes_in_budget_returns_the_agents_answer() -> None:
    guard = RunGuard()
    tools, executions = make_tools(guard)
    loop = FakeAgentLoop(guard, tools, ["get_recent_logs"] * 7 + [CONFIDENT_ANSWER], ErrorHandling.PROPAGATE)

    result = asyncio.run(run_guarded(guard, loop.run))

    assert result is CONFIDENT_ANSWER
    assert result.halted_by is None
    assert result.can_open_pr
    assert executions[0] == 7
