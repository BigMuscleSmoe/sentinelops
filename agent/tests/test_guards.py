import asyncio
import inspect
import json
import logging
import time

import pytest

from guards import (
    Limits,
    Remaining,
    RunGuard,
    StepLimitExceeded,
    StopReason,
    TokenLimitExceeded,
    WallClockExceeded,
    guarded_tool,
    run_guarded,
)
from models import Hypothesis

DIAGNOSIS = Hypothesis(
    root_cause="Upstream payments-api p99 rose to 4.8s; checkout-api times out waiting on it.",
    confidence=0.85,
    evidence=["payments-api p99 4.8s at 14:03"],
    affected_component="payments-api",
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def guard(clock: FakeClock) -> RunGuard:
    return RunGuard(clock=clock)


def tool_call_logs(caplog: pytest.LogCaptureFixture) -> list[dict]:
    lines = [json.loads(r.getMessage()) for r in caplog.records if r.name == "sentinelops.guards"]
    return [line for line in lines if line["event"] == "tool_call"]


def test_defaults_match_the_documented_limits() -> None:
    assert Limits() == Limits(max_tool_calls=15, max_input_tokens=200_000, max_wall_clock_s=300.0)


# --- normal completion ---------------------------------------------------------


def test_run_under_all_three_limits_returns_the_agents_hypothesis(clock: FakeClock) -> None:
    guard = RunGuard(clock=clock)

    async def work() -> Hypothesis:
        for i in range(15):  # right at the step cap, not past it
            guard.check()
            guard.record_input_tokens(13_000)  # 195k total
            guard.record_tool_call(f"tool_{i}")
            clock.advance(19)  # 285s total
        return DIAGNOSIS

    result = asyncio.run(run_guarded(guard, work))

    assert result is DIAGNOSIS
    assert result.halted_by is None
    assert guard.tripped is None
    assert guard.remaining() == Remaining(steps=0, input_tokens=5_000, seconds=15.0)


def test_completed_run_at_zero_confidence_is_not_halted() -> None:
    unsure = DIAGNOSIS.model_copy(update={"confidence": 0.0})

    async def work() -> Hypothesis:
        return unsure

    result = asyncio.run(run_guarded(RunGuard(), work))

    assert result.confidence == 0.0
    assert result.halted_by is None


# --- step cap -------------------------------------------------------------------


def test_sixteenth_tool_call_raises_step_limit(guard: RunGuard) -> None:
    for i in range(15):
        guard.record_tool_call(f"tool_{i}")

    with pytest.raises(StepLimitExceeded):
        guard.record_tool_call("tool_15")

    assert len(guard.tool_calls) == 15, "the refused call must not be counted"
    assert guard.tripped is StopReason.STEP_LIMIT


def test_step_limit_returns_halted_hypothesis_instead_of_raising() -> None:
    guard = RunGuard()

    async def work() -> Hypothesis:
        while True:  # an agent stuck in a retry loop
            guard.record_tool_call("get_recent_logs")

    result = asyncio.run(run_guarded(guard, work))

    assert result.halted_by is StopReason.STEP_LIMIT
    assert result.confidence == 0.0
    assert result.affected_component == "unknown"
    assert "step limit" in result.evidence[0]
    assert "15 of 15 tool calls" in result.evidence[0]
    assert "get_recent_logs ×15" in result.evidence[1]


# --- token cap ------------------------------------------------------------------


def test_tokens_accumulate_across_calls(guard: RunGuard) -> None:
    guard.record_input_tokens(120_000)
    guard.record_input_tokens(79_999)
    guard.check()  # 199,999: still under
    assert guard.remaining().input_tokens == 1


def test_reaching_the_token_cap_stops_the_next_call(guard: RunGuard) -> None:
    guard.record_input_tokens(150_000)
    guard.record_input_tokens(50_000)  # recording never raises

    with pytest.raises(TokenLimitExceeded):
        guard.check()
    with pytest.raises(TokenLimitExceeded):
        guard.record_tool_call("get_metrics")


def test_token_limit_returns_halted_hypothesis_instead_of_raising() -> None:
    guard = RunGuard()

    async def work() -> Hypothesis:
        guard.record_tool_call("get_metrics")
        while True:  # context growing every turn
            guard.check()
            guard.record_input_tokens(60_000)

    result = asyncio.run(run_guarded(guard, work))

    assert result.halted_by is StopReason.TOKEN_LIMIT
    assert result.confidence == 0.0
    assert "input-token limit" in result.evidence[0]
    assert "240,000 of 200,000 input tokens" in result.evidence[0]
    assert guard.tripped is StopReason.TOKEN_LIMIT


def test_rejects_negative_token_counts(guard: RunGuard) -> None:
    with pytest.raises(ValueError):
        guard.record_input_tokens(-1)


# --- wall clock -----------------------------------------------------------------


def test_wall_clock_trips_at_five_minutes(guard: RunGuard, clock: FakeClock) -> None:
    clock.advance(299.9)
    guard.check()

    clock.advance(0.1)
    with pytest.raises(WallClockExceeded):
        guard.check()
    assert guard.tripped is StopReason.WALL_CLOCK


def test_wall_clock_returns_halted_hypothesis_instead_of_raising(clock: FakeClock) -> None:
    guard = RunGuard(clock=clock)

    async def work() -> Hypothesis:
        guard.record_tool_call("get_recent_logs")
        clock.advance(301)
        guard.check()
        return DIAGNOSIS

    result = asyncio.run(run_guarded(guard, work))

    assert result.halted_by is StopReason.WALL_CLOCK
    assert result.confidence == 0.0
    assert "wall-clock limit" in result.evidence[0]


def test_wall_clock_cuts_off_a_hung_call() -> None:
    # Real time: nothing inside work() checks the guard, so only the deadline can stop it.
    guard = RunGuard(Limits(max_wall_clock_s=0.05))

    async def work() -> Hypothesis:
        guard.record_tool_call("get_recent_logs")
        await asyncio.sleep(10)
        return DIAGNOSIS

    started = time.monotonic()
    result = asyncio.run(run_guarded(guard, work))

    assert time.monotonic() - started < 2
    assert result.halted_by is StopReason.WALL_CLOCK
    assert result.confidence == 0.0
    assert "get_recent_logs ×1" in result.evidence[1]


# --- budget and logging ---------------------------------------------------------


def test_remaining_reports_each_budget(guard: RunGuard, clock: FakeClock) -> None:
    assert guard.remaining() == Remaining(steps=15, input_tokens=200_000, seconds=300.0)

    guard.record_tool_call("get_metrics")
    guard.record_input_tokens(12_345)
    clock.advance(42)

    assert guard.remaining() == Remaining(steps=14, input_tokens=187_655, seconds=258.0)


def test_remaining_never_goes_negative(guard: RunGuard, clock: FakeClock) -> None:
    guard.record_input_tokens(999_999)
    clock.advance(1000)
    assert guard.remaining() == Remaining(steps=15, input_tokens=0, seconds=0.0)


def test_budget_line_is_compact(guard: RunGuard, clock: FakeClock) -> None:
    for _ in range(6):
        guard.record_tool_call("get_metrics")
    guard.record_input_tokens(57_900)
    clock.advance(101.6)

    assert guard.budget_line() == "[budget: 9 steps, 142k tokens, 198s remaining]"


def test_guarded_tool_counts_the_call_and_appends_the_budget(guard: RunGuard) -> None:
    def get_metrics(metric: str, window_minutes: int = 30) -> str:
        """Fetch metric statistics."""
        return f"{metric} p99=4.8s over {window_minutes}m"

    tool = guarded_tool(guard, get_metrics)

    assert tool("Latency", window_minutes=10) == (
        "Latency p99=4.8s over 10m\n[budget: 14 steps, 200k tokens, 300s remaining]"
    )
    assert guard.tool_calls == ["get_metrics"]
    # Frameworks build the tool schema from these.
    assert tool.__name__ == "get_metrics"
    assert tool.__doc__ == "Fetch metric statistics."
    assert list(inspect.signature(tool).parameters) == ["metric", "window_minutes"]


def test_guarded_tool_wraps_async_tools(guard: RunGuard) -> None:
    async def get_recent_logs() -> str:
        return "3 lines"

    result = asyncio.run(guarded_tool(guard, get_recent_logs)())

    assert result.startswith("3 lines\n[budget: 14 steps")


def test_guarded_tool_refuses_before_running_the_tool(guard: RunGuard) -> None:
    runs = 0

    def get_recent_logs() -> str:
        nonlocal runs
        runs += 1
        return "ok"

    tool = guarded_tool(guard, get_recent_logs)
    for _ in range(15):
        tool()
    with pytest.raises(StepLimitExceeded):
        tool()

    assert runs == 15


def test_logs_a_structured_line_per_tool_call(
    guard: RunGuard, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="sentinelops.guards")

    guard.record_input_tokens(10_000)
    guard.record_tool_call("get_recent_logs")
    clock.advance(5)
    guard.record_input_tokens(15_000)
    guard.record_tool_call("get_metrics")

    lines = tool_call_logs(caplog)
    assert [line["tool"] for line in lines] == ["get_recent_logs", "get_metrics"]
    assert lines[1]["steps_used"] == 2
    assert lines[1]["input_tokens"] == 25_000
    assert lines[1]["elapsed_s"] == 5.0
    assert lines[1]["remaining"] == {"steps": 13, "input_tokens": 175_000, "seconds": 295.0}


def test_refused_call_is_logged_as_a_trip_not_a_tool_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="sentinelops.guards")
    guard = RunGuard(Limits(max_tool_calls=1))

    guard.record_tool_call("get_metrics")
    with pytest.raises(StepLimitExceeded):
        guard.record_tool_call("get_metrics")

    events = [json.loads(r.getMessage())["event"] for r in caplog.records]
    assert events == ["tool_call", "guard_tripped"]


# --- run_guarded edge cases -----------------------------------------------------


def test_first_limit_hit_is_the_one_reported(guard: RunGuard, clock: FakeClock) -> None:
    clock.advance(301)
    with pytest.raises(WallClockExceeded):
        guard.check()

    guard.record_input_tokens(1_000_000)
    with pytest.raises(WallClockExceeded):
        guard.check()


def test_halted_hypothesis_survives_framework_wrapping_the_exception() -> None:
    guard = RunGuard(Limits(max_tool_calls=1))

    async def work() -> Hypothesis:
        guard.record_tool_call("get_recent_logs")
        try:
            guard.record_tool_call("get_metrics")
        except StepLimitExceeded as e:
            raise RuntimeError("hook failed") from e
        return DIAGNOSIS

    result = asyncio.run(run_guarded(guard, work))

    assert result.confidence == 0.0
    assert guard.tripped is StopReason.STEP_LIMIT


def test_unrelated_errors_still_propagate() -> None:
    async def work() -> Hypothesis:
        raise KeyError("bug in a tool")

    with pytest.raises(KeyError):
        asyncio.run(run_guarded(RunGuard(), work))


def test_a_timeout_inside_work_is_not_mistaken_for_the_deadline() -> None:
    guard = RunGuard()

    async def work() -> Hypothesis:
        raise TimeoutError("CloudWatch query timed out")

    with pytest.raises(TimeoutError, match="CloudWatch"):
        asyncio.run(run_guarded(guard, work))
    assert guard.tripped is None


def test_halted_with_no_tool_calls_says_so() -> None:
    guard = RunGuard(Limits(max_input_tokens=100))

    async def work() -> Hypothesis:
        guard.record_input_tokens(500)
        guard.check()
        return DIAGNOSIS

    result = asyncio.run(run_guarded(guard, work))

    assert result.evidence[1] == "No tool calls were made before halting."
