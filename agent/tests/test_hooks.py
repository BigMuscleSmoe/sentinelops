"""The hooks between Strands and the guard: the prompt's bound and count, and what each call is charged."""

import asyncio
import json
import logging
from types import SimpleNamespace

import httpx
import pytest
from strands.types.exceptions import ModelThrottledException

from fake_anthropic import FakeAnthropic
from guards import CostLimitExceeded, Limits, RunGuard, WallClockExceeded
from hooks import (
    COUNT_MARGIN_TOKENS,
    COUNT_TIMEOUT_S,
    PROMPT_OVERHEAD_TOKENS,
    GuardHooks,
    ModelRefused,
    count_margin,
    prompt_bound,
    prompt_limit,
    usage_of,
)
from pricing import Usage, price_for
from providers import THINKING, Settings, build_model

HAIKU = price_for("anthropic", "claude-haiku-5-5")
SYSTEM = "You investigate alarms."
SPECS = [{"name": "get_metrics", "description": "Metrics.", "inputSchema": {"json": {"type": "object"}}}]
ALARM = {"role": "user", "content": [{"text": "An alarm fired."}]}
SPEND_LIMIT = "You have reached your specified API usage limits."


def size(value: object) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return len(text.encode("utf-8"))


def agent(*messages: dict) -> SimpleNamespace:
    registry = SimpleNamespace(get_all_tool_specs=lambda: SPECS)
    return SimpleNamespace(messages=list(messages), system_prompt=SYSTEM, tool_registry=registry)


def reply(input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> dict:
    usage = {"inputTokens": input_tokens, "outputTokens": output_tokens, "totalTokens": 0}
    if cache_read:
        usage["cacheReadInputTokens"] = cache_read
    if cache_write:
        usage["cacheWriteInputTokens"] = cache_write
    return {
        "role": "assistant",
        "content": [{"toolUse": {"toolUseId": "t1", "name": "get_metrics", "input": {}}}],
        "metadata": {"usage": usage, "metrics": {"latencyMs": 1}},
    }


def tool_result(text: str) -> dict:
    return {"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "status": "success", "content": [{"text": text}]}}]}


# A round of tool output: a byte bound near 25K.
MID = (ALARM, reply(3_000, 300), tool_result("x" * 20_000))
# A burst's log pages: a byte bound past 100K, where a reservation alone passes $0.10.
BURST = (ALARM, reply(3_000, 300), tool_result("x" * 110_000))


def test_the_first_call_counts_every_byte_it_sends() -> None:
    bound = prompt_bound(agent(ALARM))

    assert bound == size(SYSTEM) + size(SPECS) + size([ALARM]) + PROMPT_OVERHEAD_TOKENS


def test_a_later_call_counts_the_last_reply_exactly_and_what_came_after_it_as_bytes() -> None:
    result = {"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "content": [{"text": "\u00e9" * 100}]}}]}

    bound = prompt_bound(agent(ALARM, reply(3_000, 400, cache_read=500, cache_write=100), result))

    assert bound == 3_000 + 500 + 100 + 400 + size([result]) + PROMPT_OVERHEAD_TOKENS
    assert size([result]) > len(json.dumps([result], ensure_ascii=False)), "UTF-8 bytes, not characters"


def test_usage_reads_every_counter() -> None:
    assert usage_of(reply(10, 20, cache_read=30, cache_write=40)) == Usage(
        input_tokens=10, output_tokens=20, cache_read_tokens=30, cache_write_tokens=40
    )


def test_a_count_is_trusted_to_within_its_margin_and_never_past_the_bound() -> None:
    assert count_margin(5_000) == 1_000
    assert count_margin(25_000) == 2_500
    assert prompt_limit(bound=200_000, counted=5_000) == 6_000
    assert prompt_limit(bound=200_000, counted=50_000) == 55_000
    assert prompt_limit(bound=30_000, counted=50_000) == 30_000
    assert prompt_limit(bound=30_000, counted=None) == 30_000


@pytest.mark.parametrize("prompt", [1_000, 9_000, 50_000, 90_000, 90_910, 95_000, 99_999, 100_000, 100_001, 150_000])
def test_any_count_within_its_margin_reserves_what_the_call_can_cost(prompt: int) -> None:
    # Across the 100K line, where the whole request moves to the long-prompt prices.
    start = max(0, min(prompt - COUNT_MARGIN_TOKENS, int(prompt / 1.1)) - 2)
    lowest = next(c for c in range(start, prompt + 1) if c + count_margin(c) >= prompt)
    worst = HAIKU.cost(Usage(input_tokens=prompt, output_tokens=16_000))

    for counted in (lowest, prompt, 2 * prompt):
        limit = prompt_limit(bound=3 * prompt, counted=counted)
        assert limit >= prompt
        assert HAIKU.most_a_call_costs(limit, 16_000) >= worst


def hooks(limits: Limits = Limits()) -> tuple[RunGuard, GuardHooks]:
    guard = RunGuard(limits)
    return guard, GuardHooks(guard, HAIKU, max_output_tokens=16_000)


def counts(*scripted: object) -> FakeAnthropic:
    return FakeAnthropic([], counts=list(scripted))


def send(
    h: GuardHooks, *messages: dict, fake: FakeAnthropic | None = None, model: object = None, **params: object
) -> None:
    """What happens to a call before it goes out: the before-call hook, then the middleware.

    The model is the agent's own unless one is given, with any params added to its request.
    """
    fake = fake if fake is not None else FakeAnthropic([])
    if model is None:
        model = build_model(Settings(), "sk-ant-test-0000", http_client=fake.client())
        model.update_config(params={"thinking": THINKING, **params})
    a = agent(*messages)
    context = SimpleNamespace(
        agent=a,
        messages=a.messages,
        system_prompt=SYSTEM,
        tool_specs=SPECS,
        tool_choice=None,
        structured_output_model=None,
        model=model,
        dynamic_trailing_blocks=0,
    )
    h.before_model_call(SimpleNamespace(projected_input_tokens=1_000))
    asyncio.run(h.before_send(context))


def after(h: GuardHooks, *, message: dict | None = None, exception: Exception | None = None, stop: str = "tool_use") -> None:
    response = SimpleNamespace(message=message, stop_reason=stop) if message else None
    h.after_model_call(SimpleNamespace(stop_response=response, exception=exception))


def warnings(caplog: pytest.LogCaptureFixture) -> list[dict]:
    return [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.name == "sentinelops.model" and r.levelno == logging.WARNING
    ]


def api_error(status: int, kind: str, message: str) -> httpx.Response:
    return httpx.Response(status, json={"type": "error", "error": {"type": kind, "message": message}})


def test_the_count_is_taken_on_the_request_itself_once_and_quickly() -> None:
    guard, h = hooks()
    fake = counts(1_234)

    send(h, *MID, fake=fake)

    [counted] = fake.count_requests
    assert counted["path"] == "/v1/messages/count_tokens"
    assert set(counted["body"]) == {"model", "messages", "system", "tools", "thinking"}
    assert counted["body"]["thinking"] == THINKING
    assert [t["name"] for t in counted["body"]["tools"]] == ["get_metrics"]
    assert counted["timeout"] == dict.fromkeys(("connect", "read", "write", "pool"), COUNT_TIMEOUT_S)
    assert h._counted == 1_234
    assert h._limit == 1_234 + 1_000
    assert h._reserved == HAIKU.most_a_call_costs(2_234, 16_000)


def test_a_burst_sized_prompt_goes_out_on_its_count_where_the_bound_alone_would_stop_it() -> None:
    guard, h = hooks()
    send(h, *BURST, fake=counts(50_000))
    assert h._bound > 100_000 and h._limit == 55_000

    guard, h = hooks()
    with pytest.raises(CostLimitExceeded):
        send(h, *BURST, fake=counts(api_error(500, "api_error", "Internal server error")))
    assert h._counted is None and h._limit == h._bound


@pytest.mark.parametrize(
    ("failure", "error", "status"),
    [
        (api_error(500, "api_error", "Internal server error"), "InternalServerError", 500),
        (api_error(529, "overloaded_error", "Overloaded"), "OverloadedError", 529),
        (api_error(429, "rate_limit_error", "Rate limited"), "RateLimitError", 429),
        (api_error(400, "invalid_request_error", SPEND_LIMIT), "BadRequestError", 400),
        (httpx.ReadTimeout("timed out"), "APITimeoutError", None),
        (httpx.ConnectError("connection refused"), "APIConnectionError", None),
    ],
)
def test_a_failed_count_means_the_bound_for_that_call_after_one_try(
    failure: object, error: str, status: int | None, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="sentinelops.model")
    guard, h = hooks()
    fake = counts(failure, 1_234)

    send(h, ALARM, fake=fake)

    assert len(fake.count_requests) == 1, "no retries"
    assert h._counted is None and h._limit == h._bound
    assert [(w["event"], w["error"], w["status"]) for w in warnings(caplog)] == [("token_count_failed", error, status)]
    assert SPEND_LIMIT not in caplog.text, "an error's message never reaches the log"

    # A failure is about that one call: the next is counted again.
    send(h, ALARM, fake=fake)
    assert h._counted == 1_234


def test_only_the_anthropic_api_is_asked_for_a_count(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="sentinelops.model")
    guard, h = hooks()

    send(h, *MID, model=SimpleNamespace())

    assert h._counted is None and h._limit == h._bound
    assert [(w["event"], w["model"]) for w in warnings(caplog)] == [("token_count_skipped", "SimpleNamespace")]


def test_a_call_is_refused_when_its_worst_case_would_pass_the_cap() -> None:
    guard, h = hooks(Limits(max_cost_usd=0.008))  # 16k output tokens alone could cost $0.008

    with pytest.raises(CostLimitExceeded):
        send(h, ALARM)
    assert guard.cost_usd == 0.0


def test_no_count_is_sent_for_a_call_another_limit_already_stops() -> None:
    guard, h = hooks(Limits(max_wall_clock_s=0.0))
    fake = FakeAnthropic([])

    with pytest.raises(WallClockExceeded):
        send(h, ALARM, fake=fake)
    assert fake.count_requests == []


def test_a_request_field_the_count_cant_take_means_no_count(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="sentinelops.model")
    guard, h = hooks()
    fake = FakeAnthropic([])

    send(h, ALARM, fake=fake, temperature=0.2)

    assert fake.count_requests == []
    assert h._counted is None and h._limit == h._bound
    assert [(w["event"], w["fields"]) for w in warnings(caplog)] == [("token_count_skipped", ["temperature"])]


def test_a_call_is_charged_what_its_usage_costs() -> None:
    guard, h = hooks()
    send(h, *MID, fake=counts(10_000))
    after(h, message=reply(10_000, 1_000))

    assert guard.cost_usd == pytest.approx(HAIKU.cost(Usage(input_tokens=10_000, output_tokens=1_000)))
    assert guard.input_tokens == 10_000
    [call] = h.calls
    assert (call.counted_tokens, call.limit_tokens) == (10_000, 11_000)
    assert call.reserved_usd >= call.cost_usd


def test_a_throttled_call_costs_nothing_and_a_failed_one_its_reservation() -> None:
    guard, h = hooks()
    send(h, ALARM)
    after(h, exception=ModelThrottledException("slow down"))
    assert guard.cost_usd == 0.0

    send(h, ALARM)
    after(h, exception=TimeoutError("read timed out"))
    assert guard.cost_usd == pytest.approx(h._reserved) and h._reserved > 0
    assert h.calls == []


def test_a_call_that_fails_before_it_is_sent_is_charged_nothing() -> None:
    guard, h = hooks()
    send(h, ALARM)
    after(h, message=reply(1_000, 100))
    spent = guard.cost_usd

    h.before_model_call(SimpleNamespace(projected_input_tokens=1_000))
    after(h, exception=RuntimeError("failed before the middleware ran"))

    assert guard.cost_usd == spent


def test_a_count_low_by_more_than_its_margin_is_caught_and_never_trusted_again(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="sentinelops.model")
    guard, h = hooks()

    # Counted at 90,000, billed at 100,001: low by more than its 9,000 margin,
    # and across the 100K line, so this call costs more than it reserved. The
    # design accepts this one exposure, with the monthly limit behind it.
    send(h, *BURST, fake=counts(90_000))
    after(h, message=reply(100_001, 16_000))

    [call] = h.calls
    assert call.limit_tokens == 99_000 and call.bound_tokens > 100_000
    assert call.cost_usd > call.reserved_usd
    [miss] = warnings(caplog)
    assert (miss["event"], miss["counted_tokens"], miss["prompt_tokens"]) == ("token_count_miss", 90_000, 100_001)

    # From then on, the byte bound alone: no count, and here the bound refuses the call.
    fake = counts(90_000)
    with pytest.raises(CostLimitExceeded):
        send(h, *BURST, fake=fake)
    assert fake.count_requests == []
    assert h._counted is None and h._limit == h._bound


def test_a_refusal_is_paid_for_then_ends_the_run() -> None:
    guard, h = hooks()
    send(h, ALARM)

    with pytest.raises(ModelRefused):
        after(h, message=reply(5_000, 50), stop="refusal")
    assert guard.cost_usd > 0
    assert [c.stop_reason for c in h.calls] == ["refusal"]
