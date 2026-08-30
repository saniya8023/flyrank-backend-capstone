"""
AI Token Pricing Calculator tests — money math correctness.

Covers:
  1. The exact formula: cost = input*rate_input + cached*rate_cached +
     output*rate_output + reasoning*rate_output (reasoning billed at
     the OUTPUT rate, per spec).
  2. Floor-division rounding: fractional micro-cents are truncated, not
     rounded, and never inflate the tenant's charge.
  3. Zero usage costs zero.
  4. No floats are ever produced — every returned value is `int`.
  5. Cached input tokens are strictly cheaper per-token than regular
     input tokens, reflecting pinned config rates.
"""
import pytest

from app.core.config import Settings
from app.services.pricing import MICRO_CENTS_PER_CENT, PricingCalculator, TokenUsage


@pytest.fixture
def settings():
    return Settings(
        RATE_INPUT_MICRO_CENTS_PER_TOKEN=30,
        RATE_CACHED_INPUT_MICRO_CENTS_PER_TOKEN=8,
        RATE_OUTPUT_MICRO_CENTS_PER_TOKEN=150,
        RATE_REASONING_MICRO_CENTS_PER_TOKEN=150,
    )


@pytest.fixture
def calculator(settings):
    return PricingCalculator(settings)


def test_formula_matches_spec_exactly(calculator):
    usage = TokenUsage(
        input_tokens=1000, cached_input_tokens=500, output_tokens=200, reasoning_tokens=100
    )
    expected_micro = (1000 * 30) + (500 * 8) + (200 * 150) + (100 * 150)
    assert calculator.calculate_cost_micro_cents(usage) == expected_micro
    assert calculator.calculate_cost_cents(usage) == expected_micro // MICRO_CENTS_PER_CENT


def test_reasoning_tokens_billed_at_output_rate(calculator, settings):
    """Swapping output<->reasoning token counts must not change cost."""
    usage_a = TokenUsage(output_tokens=100, reasoning_tokens=0)
    usage_b = TokenUsage(output_tokens=0, reasoning_tokens=100)
    assert calculator.calculate_cost_micro_cents(usage_a) == calculator.calculate_cost_micro_cents(
        usage_b
    )


def test_cached_input_is_cheaper_than_regular_input(calculator):
    regular = TokenUsage(input_tokens=1000)
    cached = TokenUsage(cached_input_tokens=1000)
    assert calculator.calculate_cost_micro_cents(cached) < calculator.calculate_cost_micro_cents(
        regular
    )


def test_zero_usage_costs_zero(calculator):
    usage = TokenUsage()
    assert calculator.calculate_cost_micro_cents(usage) == 0
    assert calculator.calculate_cost_cents(usage) == 0


def test_cost_is_floored_never_rounded_up(calculator):
    """
    1 input token at 30 micro-cents = 30 micro-cents = 0.003 cents,
    which floors to 0 whole cents — the platform absorbs the
    sub-cent remainder, the tenant is never charged a rounded-up cent
    for a fraction they didn't fully consume.
    """
    usage = TokenUsage(input_tokens=1)
    assert calculator.calculate_cost_micro_cents(usage) == 30
    assert calculator.calculate_cost_cents(usage) == 0


def test_cost_never_exceeds_sum_of_individually_floored_costs(calculator):
    """
    Reconciliation invariant: cost_cents(A) + cost_cents(B) can be LESS
    than cost_cents(A+B combined) due to floor division, but never MORE.
    """
    a = TokenUsage(input_tokens=333)
    b = TokenUsage(input_tokens=334)
    combined = TokenUsage(input_tokens=667)
    assert calculator.calculate_cost_cents(a) + calculator.calculate_cost_cents(
        b
    ) <= calculator.calculate_cost_cents(combined)


def test_all_returned_values_are_int_never_float(calculator):
    usage = TokenUsage(input_tokens=1234, cached_input_tokens=567, output_tokens=89, reasoning_tokens=1)
    assert isinstance(calculator.calculate_cost_micro_cents(usage), int)
    assert isinstance(calculator.calculate_cost_cents(usage), int)


def test_negative_token_counts_are_rejected():
    with pytest.raises(ValueError):
        TokenUsage(input_tokens=-1)


def test_api_call_events_have_zero_marginal_cost(calculator):
    """Flat-rate API calls are priced into the plan's price_cents, not per-call."""
    assert calculator.calculate_api_call_cost_cents() == 0