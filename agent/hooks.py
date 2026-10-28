"""Strands hooks that put every model call through the RunGuard.

Before a call is sent, the most it can cost is worked out and the guard
refuses the call if that could take spending past the cap. The prompt's size
comes from the API's count_tokens, run on the request about to go out and
trusted to within a margin. The byte bound below stays the ceiling, and it's
the fallback whenever a count fails or once one has proved low. After the
call, its real usage is recorded and priced from the reply's own counters.
"""

import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any

from strands.event_loop.streaming import _normalize_messages
from strands.hooks import AfterModelCallEvent, BeforeModelCallEvent, HookProvider, HookRegistry
from strands.middleware import InvokeModelContext
from strands.models.anthropic import AnthropicModel, _mechanism_tool_choice, _with_native_output
from strands.types.content import split_system_prompt
from strands.types.exceptions import ContextWindowOverflowException, ModelThrottledException

from guards import GuardLimitExceeded, RunGuard
from pricing import ModelPrice, Usage

logger = logging.getLogger("sentinelops.model")

# What a request carries beyond the text of its messages, system prompt, and
# tool specs: role and tool-use framing, and the API's own tool-use prompt.
PROMPT_OVERHEAD_TOKENS = 2_000

# The API calls its count an estimate that "might differ by a small amount",
# with no figure, so a count is trusted to within the larger of these two.
COUNT_MARGIN_TOKENS = 1_000
COUNT_MARGIN_SHARE = 0.10
# A failed count only means the looser bound, so it gets no retries and little time.
COUNT_TIMEOUT_S = 5.0
# The request fields count_tokens takes. Of the others, max_tokens doesn't change
# the prompt and extra_headers go as headers; any other field means no count.
COUNTED_FIELDS = frozenset({"model", "messages", "system", "tools", "tool_choice", "thinking", "output_config"})
UNCOUNTED_FIELDS = frozenset({"max_tokens", "extra_headers"})


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
    counted_tokens: int | None
    limit_tokens: int
    reserved_usd: float
    stop_reason: str


class GuardHooks(HookProvider):
    """The guard's hooks, and before_send, the middleware that reserves each call.

    Register before_send as an InvokeModelStage Input middleware. It runs after
    the before-call hooks, on the request about to go out, and it's the only
    place that sees the request's tool_choice: forcing the Hypothesis tool adds
    120 tokens on Haiku 5.5.
    """

    def __init__(self, guard: RunGuard, price: ModelPrice, max_output_tokens: int) -> None:
        self.guard = guard
        self.price = price
        self.max_output_tokens = max_output_tokens
        self.calls: list[ModelCall] = []
        self._bound = 0
        self._counted: int | None = None
        self._count_ms: int | None = None
        self._limit = 0
        self._projected: int | None = None
        self._reserved = 0.0
        self._trust_counts = True

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeModelCallEvent, self.before_model_call)
        registry.add_callback(AfterModelCallEvent, self.after_model_call)

    def before_model_call(self, event: BeforeModelCallEvent) -> None:
        # Nothing is reserved until before_send has the request: a call that
        # fails before then was never sent, so it's charged nothing.
        self._bound = 0
        self._counted = None
        self._count_ms = None
        self._limit = 0
        self._projected = event.projected_input_tokens
        self._reserved = 0.0

    async def before_send(self, context: InvokeModelContext) -> InvokeModelContext:
        # No count for a call that another limit already stops.
        self.guard.check()
        self._bound = prompt_bound(context.agent)
        if self._trust_counts:
            self._counted, self._count_ms = await self._count(context)
        self._limit = prompt_limit(self._bound, self._counted)
        self._reserved = self.price.most_a_call_costs(self._limit, self.max_output_tokens)
        self.guard.check(reserve_usd=self._reserved)
        return context

    async def _count(self, context: InvokeModelContext) -> tuple[int | None, int | None]:
        """The API's count of the prompt, and how long it took in ms; Nones if there's none."""
        if not isinstance(context.model, AnthropicModel):
            self._log("token_count_skipped", level=logging.WARNING, model=type(context.model).__name__)
            return None, None
        try:
            request = count_request(context)
            unknown = sorted(set(request) - COUNTED_FIELDS - UNCOUNTED_FIELDS)
            if unknown:
                self._log("token_count_skipped", level=logging.WARNING, fields=unknown)
                return None, None
            body = {key: value for key, value in request.items() if key in COUNTED_FIELDS}
            if request.get("extra_headers"):
                body["extra_headers"] = request["extra_headers"]
            client = context.model.client.with_options(max_retries=0, timeout=COUNT_TIMEOUT_S)
            started = time.monotonic()
            result = await client.messages.count_tokens(**body)
        except Exception as e:
            # The type and status only: an error's message can quote the request.
            self._log(
                "token_count_failed",
                level=logging.WARNING,
                error=type(e).__name__,
                status=getattr(e, "status_code", None),
            )
            return None, None
        return result.input_tokens, round((time.monotonic() - started) * 1000)

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
                counted_tokens=self._counted,
                limit_tokens=self._limit,
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
            counted_tokens=self._counted,
            limit_tokens=self._limit,
            count_ms=self._count_ms,
            projected_tokens=self._projected,
            reserved_usd=round(self._reserved, 6),
        )
        if self._counted is not None and usage.prompt_tokens > self._counted + count_margin(self._counted):
            # The count was low by more than its margin: the byte bound alone from here on.
            self._trust_counts = False
            self._log(
                "token_count_miss",
                level=logging.WARNING,
                counted_tokens=self._counted,
                limit_tokens=self._limit,
                prompt_tokens=usage.prompt_tokens,
            )
        if response.stop_reason == "refusal":
            raise ModelRefused("The model refused to go on with the investigation.")

    def _log(self, event: str, level: int = logging.INFO, **fields: object) -> None:
        logger.log(
            level,
            json.dumps(
                {
                    "event": event,
                    **fields,
                    "spent_usd": round(self.guard.cost_usd, 6),
                    "remaining": self.guard.remaining().model_dump(),
                }
            ),
        )


def count_request(context: InvokeModelContext) -> dict[str, Any]:
    """The request AnthropicModel.stream() is about to send, built by the same steps.

    stream_messages() normalizes the messages and passes on only their role and
    content. AnthropicModel enforces structured output itself, so it gets every
    tool spec and the output model, and stream() fits tool_choice and
    output_config to the mechanism it picks. Strands 1.62.0 is pinned, and a
    test holds the count's body to the sent one. Only a retry inside stream()
    sends something else: stale thinking dropped, which is smaller, or a forced
    tool the model rejects, sent again as native JSON, which can be larger. The
    check after the call catches a count that was low by more than its margin.
    """
    model = context.model
    messages = [{"role": m["role"], "content": m["content"]} for m in _normalize_messages(context.messages)]
    system_prompt, system_prompt_content = split_system_prompt(context.system_prompt)
    output_model = context.structured_output_model
    mechanism = model._structured_output_mechanism(output_model, context.tool_choice)
    request = model.format_request(
        messages,
        context.tool_specs or None,
        system_prompt,
        system_prompt_content=system_prompt_content,
        tool_choice=_mechanism_tool_choice(mechanism, context.tool_choice),
        dynamic_trailing_blocks=context.dynamic_trailing_blocks,
    )
    return _with_native_output(request, mechanism, output_model)


def count_margin(counted: int) -> int:
    return max(COUNT_MARGIN_TOKENS, math.ceil(counted * COUNT_MARGIN_SHARE))


def prompt_limit(bound: int, counted: int | None) -> int:
    """The prompt size a call is reserved for: its count and margin, never more than the byte bound."""
    if counted is None:
        return bound
    return min(bound, counted + count_margin(counted))


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
