"""Strands hooks that put every model call through the RunGuard.

Before a call, the most it can cost is worked out and the guard refuses the
call if that could take spending past the cap. After it, the call's real usage
is recorded and priced from the reply's own token counters.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any

from strands.hooks import AfterModelCallEvent, BeforeModelCallEvent, HookProvider, HookRegistry
from strands.types.exceptions import ContextWindowOverflowException, ModelThrottledException

from guards import GuardLimitExceeded, RunGuard
from pricing import ModelPrice, Usage

logger = logging.getLogger("sentinelops.model")

# What a request carries beyond the text of its messages, system prompt, and
# tool specs: role and tool-use framing, and the API's own tool-use prompt.
PROMPT_OVERHEAD_TOKENS = 2_000


class ModelRefused(Exception):
    """The model stopped with a refusal. The run ends without a Hypothesis."""


# Refused before it was sent, rejected before any work, or already recorded.
_NOT_CHARGED = (GuardLimitExceeded, ModelThrottledException, ContextWindowOverflowException, ModelRefused)


@dataclass(frozen=True)
class ModelCall:
    prompt_tokens: int
    output_tokens: int
    cost_usd: float
    bound_tokens: int
    reserved_usd: float
    stop_reason: str


class GuardHooks(HookProvider):
    def __init__(self, guard: RunGuard, price: ModelPrice, max_output_tokens: int) -> None:
        self.guard = guard
        self.price = price
        self.max_output_tokens = max_output_tokens
        self.calls: list[ModelCall] = []
        self._bound = 0
        self._projected: int | None = None
        self._reserved = 0.0

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeModelCallEvent, self.before_model_call)
        registry.add_callback(AfterModelCallEvent, self.after_model_call)

    def before_model_call(self, event: BeforeModelCallEvent) -> None:
        self._bound = prompt_bound(event.agent)
        self._projected = event.projected_input_tokens
        self._reserved = self.price.most_a_call_costs(self._bound, self.max_output_tokens)
        self.guard.check(reserve_usd=self._reserved)

    def after_model_call(self, event: AfterModelCallEvent) -> None:
        if event.exception is not None:
            if isinstance(event.exception, _NOT_CHARGED):
                return
            # It may have failed after the model started work: charge the most it could cost.
            self.guard.record_cost(self._reserved)
            self._log(
                "model_call_failed",
                error=type(event.exception).__name__,
                charged_usd=round(self._reserved, 6),
            )
            return
        response = event.stop_response
        if response is None:
            return
        usage = usage_of(response.message)
        cost = self.price.cost(usage)
        self.guard.record_input_tokens(usage.prompt_tokens)
        self.guard.record_cost(cost)
        self.calls.append(
            ModelCall(
                prompt_tokens=usage.prompt_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=cost,
                bound_tokens=self._bound,
                reserved_usd=self._reserved,
                stop_reason=str(response.stop_reason),
            )
        )
        self._log(
            "model_call",
            stop_reason=str(response.stop_reason),
            input_tokens=usage.input_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            output_tokens=usage.output_tokens,
            call_cost_usd=round(cost, 6),
            bound_tokens=self._bound,
            projected_tokens=self._projected,
            reserved_usd=round(self._reserved, 6),
        )
        if response.stop_reason == "refusal":
            raise ModelRefused("The model refused to go on with the investigation.")

    def _log(self, event: str, **fields: object) -> None:
        logger.info(
            json.dumps(
                {
                    "event": event,
                    **fields,
                    "spent_usd": round(self.guard.cost_usd, 6),
                    "remaining": self.guard.remaining().model_dump(),
                }
            )
        )


def prompt_bound(agent: Any) -> int:
    """An upper bound on the next request's prompt, in tokens.

    Everything up to the last reply is known exactly: that reply's prompt and
    output tokens. What's been added since, mostly tool results, counts as its
    UTF-8 bytes, because a token never stands for less than a byte of text.
    """
    messages = agent.messages
    for i in range(len(messages) - 1, -1, -1):
        message = messages[i]
        usage = message.get("metadata", {}).get("usage") if message.get("role") == "assistant" else None
        if usage:
            known = (
                usage.get("inputTokens", 0)
                + usage.get("cacheReadInputTokens", 0)
                + usage.get("cacheWriteInputTokens", 0)
                + usage.get("outputTokens", 0)
            )
            return known + _bytes(messages[i + 1 :]) + PROMPT_OVERHEAD_TOKENS
    return (
        _bytes(agent.system_prompt or "")
        + _bytes(agent.tool_registry.get_all_tool_specs())
        + _bytes(messages)
        + PROMPT_OVERHEAD_TOKENS
    )


def usage_of(message: Any) -> Usage:
    usage = message.get("metadata", {}).get("usage", {})
    return Usage(
        input_tokens=usage.get("inputTokens", 0),
        output_tokens=usage.get("outputTokens", 0),
        cache_read_tokens=usage.get("cacheReadInputTokens", 0),
        cache_write_tokens=usage.get("cacheWriteInputTokens", 0),
    )


def _bytes(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return len(text.encode("utf-8"))
