import pytest

from pricing import (
    PRICES,
    PRICES_CHECKED,
    PRICING_SOURCE,
    ModelPrice,
    Rates,
    UnknownModelPrice,
    Usage,
    price_for,
)

HAIKU = price_for("anthropic", "claude-haiku-5-5")


def test_haiku_prices_match_the_pricing_page() -> None:
    # Copied from PRICING_SOURCE on PRICES_CHECKED. Changing a number here means
    # re-reading the page, and recording the new date and source in COSTS.md.
    assert PRICING_SOURCE == "https://platform.claude.com/docs/en/about-claude/pricing"
    assert PRICES_CHECKED == "2026-10-28"
    assert HAIKU == ModelPrice(
        standard=Rates(input=0.10, cache_write=0.20, cache_read=0.01, output=0.50),
        long_prompt=Rates(input=0.50, cache_write=1.00, cache_read=0.05, output=2.50),
        long_prompt_threshold=100_000,
    )


def test_only_checked_models_can_run() -> None:
    assert set(PRICES) == {("anthropic", "claude-haiku-5-5")}


def test_a_typical_call() -> None:
    usage = Usage(input_tokens=20_000, output_tokens=1_500)
    assert HAIKU.cost(usage) == pytest.approx(0.00275)  # $0.002 in, $0.00075 out


def test_every_counter_is_billed_at_its_own_rate() -> None:
    usage = Usage(
        input_tokens=10_000, output_tokens=2_000, cache_read_tokens=30_000, cache_write_tokens=5_000
    )
    # 10k x $0.10 + 5k x $0.20 + 30k x $0.01 + 2k x $0.50, per million
    assert HAIKU.cost(usage) == pytest.approx(0.0033)


def test_a_prompt_over_100k_tokens_pays_the_higher_rates_on_everything() -> None:
    # Cache reads count toward the prompt's length, and the whole request
    # pays the long-prompt prices, output included.
    usage = Usage(input_tokens=90_000, cache_read_tokens=20_000, output_tokens=1_000)

    assert usage.prompt_tokens == 110_000
    # 90k x $0.50 + 20k x $0.05 + 1k x $2.50, per million
    assert HAIKU.cost(usage) == pytest.approx(0.0485)


def test_a_prompt_of_exactly_100k_tokens_is_not_over() -> None:
    assert HAIKU.rates_for(100_000) is HAIKU.standard
    assert HAIKU.rates_for(100_001) is HAIKU.long_prompt


def test_the_worst_case_prices_every_prompt_token_at_the_dearest_input_rate() -> None:
    # The 1-hour cache write is the dearest way to send a prompt token.
    assert HAIKU.most_a_call_costs(50_000, 16_000) == pytest.approx(0.018)  # $0.01 + $0.008


def test_the_worst_case_switches_tier_with_the_prompt() -> None:
    # 120k x $1.00 + 16k x $2.50, per million
    assert HAIKU.most_a_call_costs(120_000, 16_000) == pytest.approx(0.16)


def test_the_worst_case_is_never_below_the_real_cost() -> None:
    for usage in [
        Usage(input_tokens=99_000, output_tokens=16_000),
        Usage(input_tokens=1, cache_write_tokens=99_999, output_tokens=16_000),
        Usage(input_tokens=60_000, cache_read_tokens=60_000, output_tokens=16_000),
    ]:
        assert HAIKU.cost(usage) <= HAIKU.most_a_call_costs(usage.prompt_tokens, 16_000)


def test_an_unknown_model_refuses_to_run() -> None:
    with pytest.raises(UnknownModelPrice, match="pricing.PRICES"):
        price_for("bedrock", "us.anthropic.claude-haiku-5-5")
    with pytest.raises(UnknownModelPrice):
        price_for("anthropic", "claude-sonnet-5-5")
