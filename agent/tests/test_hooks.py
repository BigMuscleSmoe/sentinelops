"""The hooks between Strands and the guard: the prompt bound, and what each call is charged."""

import json
from types import SimpleNamespace

import pytest
from strands.types.exceptions import ModelThrottledException

from guards import CostLimitExceeded, Limits, RunGuard
from hooks import PROMPT_OVERHEAD_TOKENS, GuardHooks, ModelRefused, prompt_bound, usage_of
from pricing import Usage, price_for

HAIKU = price_for("anthropic", "claude-haiku-5-5")
SYSTEM = "You investigate alarms."
SPECS = [{"name": "get_metrics", "description": "Metrics.", "inputSchema": {"json": {"type": "object"}}}]
ALARM = {"role": "user", "content": [{"text": "An alarm fired."}]}


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


def hooks(limits: Limits = Limits()) -> tuple[RunGuard, GuardHooks]:
    guard = RunGuard(limits)
    return guard, GuardHooks(guard, HAIKU, max_output_tokens=16_000)


def before(h: GuardHooks, *messages: dict) -> None:
    h.before_model_call(SimpleNamespace(agent=agent(*messages), projected_input_tokens=1_000))


def after(h: GuardHooks, *, message: dict | None = None, exception: Exception | None = None, stop: str = "tool_use") -> None:
    response = SimpleNamespace(message=message, stop_reason=stop) if message else None
    h.after_model_call(SimpleNamespace(stop_response=response, exception=exception))


def test_a_call_is_refused_when_its_worst_case_would_pass_the_cap() -> None:
    guard, h = hooks(Limits(max_cost_usd=0.008))  # 16k output tokens alone could cost $0.008

    with pytest.raises(CostLimitExceeded):
        before(h, ALARM)
    assert guard.cost_usd == 0.0


def test_a_call_is_charged_what_its_usage_costs() -> None:
    guard, h = hooks()
    before(h, ALARM)
    after(h, message=reply(10_000, 1_000))

    assert guard.cost_usd == pytest.approx(HAIKU.cost(Usage(10_000, 1_000)))
    assert guard.input_tokens == 10_000
    assert h.calls[0].reserved_usd >= h.calls[0].cost_usd


def test_a_throttled_call_costs_nothing_and_a_failed_one_its_reservation() -> None:
    guard, h = hooks()
    before(h, ALARM)
    after(h, exception=ModelThrottledException("slow down"))
    assert guard.cost_usd == 0.0

    before(h, ALARM)
    after(h, exception=TimeoutError("read timed out"))
    assert guard.cost_usd == pytest.approx(h._reserved)
    assert h.calls == []


def test_a_refusal_is_paid_for_then_ends_the_run() -> None:
    guard, h = hooks()
    before(h, ALARM)

    with pytest.raises(ModelRefused):
        after(h, message=reply(5_000, 50), stop="refusal")
    assert guard.cost_usd > 0
    assert [c.stop_reason for c in h.calls] == ["refusal"]
