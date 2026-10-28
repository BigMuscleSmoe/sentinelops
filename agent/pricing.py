"""What a model call costs, in dollars.

Prices are dollars per million tokens, copied from Anthropic's pricing page
(PRICING_SOURCE, checked on PRICES_CHECKED). Only models listed in PRICES can
run: a model whose price isn't known can't be held to the per-run cost cap.
"""

from dataclasses import dataclass

PRICING_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
PRICES_CHECKED = "2026-10-28"


@dataclass(frozen=True)
class Usage:
    """Token counts for one model call, as the API reports them.

    input_tokens excludes the tokens read from or written to the prompt cache,
    so the prompt is the sum of the three input counters.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


@dataclass(frozen=True)
class Rates:
    """Dollars per million tokens."""

    input: float
    cache_write: float  # the 1-hour write price, the higher of the two
    cache_read: float
    output: float


@dataclass(frozen=True)
class ModelPrice:
    standard: Rates
    # Haiku 5.5 bills a prompt over long_prompt_threshold tokens at higher rates.
    # The whole request pays them, cache reads included.
    long_prompt: Rates | None = None
    long_prompt_threshold: int = 100_000

    def rates_for(self, prompt_tokens: int) -> Rates:
        if self.long_prompt is not None and prompt_tokens > self.long_prompt_threshold:
            return self.long_prompt
        return self.standard

    def cost(self, usage: Usage) -> float:
        rates = self.rates_for(usage.prompt_tokens)
        return (
            usage.input_tokens * rates.input
            + usage.cache_write_tokens * rates.cache_write
            + usage.cache_read_tokens * rates.cache_read
            + usage.output_tokens * rates.output
        ) / 1_000_000

    def most_a_call_costs(self, prompt_tokens: int, max_output_tokens: int) -> float:
        """The worst case for a call: every prompt token at the highest input rate, and full output."""
        rates = self.rates_for(prompt_tokens)
        dearest_input = max(rates.input, rates.cache_write, rates.cache_read)
        return (prompt_tokens * dearest_input + max_output_tokens * rates.output) / 1_000_000


# Keyed by (provider, model ID). Bedrock is left out until its quota is granted
# and its prices are checked.
PRICES: dict[tuple[str, str], ModelPrice] = {
    ("anthropic", "claude-haiku-5-5"): ModelPrice(
        standard=Rates(input=0.10, cache_write=0.20, cache_read=0.01, output=0.50),
        long_prompt=Rates(input=0.50, cache_write=1.00, cache_read=0.05, output=2.50),
    ),
}


class UnknownModelPrice(Exception):
    pass


def price_for(provider: str, model_id: str) -> ModelPrice:
    try:
        return PRICES[(provider, model_id)]
    except KeyError:
        raise UnknownModelPrice(
            f"No price for {provider} model {model_id}. Add it to pricing.PRICES from "
            f"{PRICING_SOURCE} before running it, so the cost cap can hold."
        ) from None
