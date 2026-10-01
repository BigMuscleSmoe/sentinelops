import asyncio
import time

import pytest

from guards import Guards, GuardTripped, Limits, run_guarded
from models import PartialReport, StopReason


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
def guards(clock: FakeClock) -> Guards:
    return Guards(clock=clock)


def test_defaults_match_the_documented_limits() -> None:
    assert Limits() == Limits(max_tool_calls=15, max_input_tokens=200_000, max_wall_clock_s=300.0)


# --- step cap ----------------------------------------------------------------


def test_allows_exactly_15_tool_calls(guards: Guards) -> None:
    for i in range(15):
        guards.before_tool_call(f"tool_{i}")

    with pytest.raises(GuardTripped) as exc:
        guards.before_tool_call("tool_15")

    assert exc.value.reason is StopReason.STEP_LIMIT
    assert len(guards.tool_calls) == 15, "the refused call must not be recorded"


def test_stays_tripped_after_step_limit(guards: Guards) -> None:
    for i in range(15):
        guards.before_tool_call(f"tool_{i}")
    with pytest.raises(GuardTripped):
        guards.before_tool_call("one_more")

    with pytest.raises(GuardTripped):
        guards.before_model_call()


# --- token cap ---------------------------------------------------------------


def test_allows_model_calls_under_the_token_cap(guards: Guards) -> None:
    guards.after_model_call(199_999)
    guards.before_model_call()
    guards.before_tool_call("get_metrics")


def test_trips_once_cumulative_tokens_reach_the_cap(guards: Guards) -> None:
    guards.after_model_call(150_000)
    guards.after_model_call(50_000)

    with pytest.raises(GuardTripped) as exc:
        guards.before_model_call()
    assert exc.value.reason is StopReason.TOKEN_LIMIT


def test_token_cap_also_blocks_tool_calls(guards: Guards) -> None:
    guards.after_model_call(250_000)

    with pytest.raises(GuardTripped) as exc:
        guards.before_tool_call("get_logs")
    assert exc.value.reason is StopReason.TOKEN_LIMIT


def test_recording_tokens_over_the_cap_does_not_raise(guards: Guards) -> None:
    # The response that crossed the cap may be the final answer; keep it.
    guards.after_model_call(500_000)
    assert guards.input_tokens == 500_000


def test_rejects_negative_token_counts(guards: Guards) -> None:
    with pytest.raises(ValueError):
        guards.after_model_call(-1)


# --- wall clock --------------------------------------------------------------


def test_trips_at_five_minutes(guards: Guards, clock: FakeClock) -> None:
    clock.advance(299.9)
    guards.before_tool_call("get_logs")

    clock.advance(0.1)
    with pytest.raises(GuardTripped) as exc:
        guards.before_tool_call("get_metrics")
    assert exc.value.reason is StopReason.TIME_LIMIT


def test_first_limit_hit_is_the_one_reported(guards: Guards, clock: FakeClock) -> None:
    clock.advance(301)
    with pytest.raises(GuardTripped):
        guards.before_model_call()

    guards.after_model_call(1_000_000)
    with pytest.raises(GuardTripped) as exc:
        guards.before_model_call()
    assert exc.value.reason is StopReason.TIME_LIMIT


def test_remaining_seconds_never_goes_negative(guards: Guards, clock: FakeClock) -> None:
    clock.advance(1000)
    assert guards.remaining_s == 0.0


# --- run_guarded -------------------------------------------------------------


def test_returns_the_result_when_no_limit_trips() -> None:
    guards = Guards()

    async def work() -> str:
        guards.before_tool_call("get_logs")
        return "diagnosis"

    assert asyncio.run(run_guarded(guards, work)) == "diagnosis"


def test_wall_clock_returns_partial_report_instead_of_raising() -> None:
    guards = Guards(Limits(max_wall_clock_s=0.05))

    async def work() -> str:
        guards.before_tool_call("get_logs")
        await asyncio.sleep(10)  # a hung model or tool call
        return "never"

    started = time.monotonic()
    result = asyncio.run(run_guarded(guards, work))

    assert time.monotonic() - started < 2, "deadline must cut the hung call short"
    assert isinstance(result, PartialReport)
    assert result.stop_reason is StopReason.TIME_LIMIT
    assert result.tool_calls == ["get_logs"]


def test_step_limit_returns_partial_report() -> None:
    guards = Guards(Limits(max_tool_calls=3))

    async def work() -> str:
        while True:  # an agent stuck in a retry loop
            guards.before_tool_call("get_logs")

    result = asyncio.run(run_guarded(guards, work))

    assert isinstance(result, PartialReport)
    assert result.stop_reason is StopReason.STEP_LIMIT
    assert result.tool_calls == ["get_logs"] * 3


def test_token_limit_returns_partial_report() -> None:
    guards = Guards()

    async def work() -> str:
        while True:
            guards.before_model_call()
            guards.after_model_call(60_000)

    result = asyncio.run(run_guarded(guards, work))

    assert isinstance(result, PartialReport)
    assert result.stop_reason is StopReason.TOKEN_LIMIT
    assert result.input_tokens == 240_000


def test_partial_report_survives_framework_wrapping_the_exception() -> None:
    guards = Guards(Limits(max_tool_calls=1))

    async def work() -> str:
        guards.before_tool_call("get_logs")
        try:
            guards.before_tool_call("get_metrics")
        except GuardTripped as e:
            raise RuntimeError("hook failed") from e
        return "never"

    result = asyncio.run(run_guarded(guards, work))

    assert isinstance(result, PartialReport)
    assert result.stop_reason is StopReason.STEP_LIMIT


def test_unrelated_errors_still_propagate() -> None:
    guards = Guards()

    async def work() -> str:
        raise KeyError("bug in a tool")

    with pytest.raises(KeyError):
        asyncio.run(run_guarded(guards, work))


def test_a_timeout_inside_work_is_not_mistaken_for_the_deadline() -> None:
    guards = Guards()

    async def work() -> str:
        raise TimeoutError("CloudWatch query timed out")

    with pytest.raises(TimeoutError, match="CloudWatch"):
        asyncio.run(run_guarded(guards, work))
    assert guards.stop_reason is None
