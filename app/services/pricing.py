"""
PricingCalculator — AI token cost math.

HARD RULE: no floats touch money anywhere in this module. Rates are
pinned in `Settings` as integer MICRO-CENTS per token (1 cent =
10,000 micro-cents). We accumulate the cost in micro-cents (still an
integer) and only convert to whole cents at the very end, using
integer floor division — i.e. the tenant is never overcharged by a
fraction of a cent, and any residual fraction is deliberately
absorbed by the platform, not the customer. This rounding policy is
documented in EVIDENCE.md.

    Cost (micro-cents) = input_tokens        * rate_input
                        + cached_input_tokens * rate_cached
                        + output_tokens        * rate_output
                        + reasoning_tokens      * rate_output   # billed as output

    Cost (cents) = Cost (micro-cents) // 10_000
"""
from dataclasses import dataclass

from app.core.config import Settings

MICRO_CENTS_PER_CENT = 10_000


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    def __post_init__(self) -> None:
        for field_name in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_tokens",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must be >= 0")

    @property
    def total_tokens(self) -> int:
        """Metering unit consumed against the plan's token_limit."""
        return (
            self.input_tokens
            + self.cached_input_tokens
            + self.output_tokens
            + self.reasoning_tokens
        )


class PricingCalculator:
    """Stateless money-math service. One instance per Settings object."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def calculate_cost_micro_cents(self, usage: TokenUsage) -> int:
        """Exact cost in micro-cents (integer, no rounding loss yet)."""
        s = self._settings
        return (
            usage.input_tokens * s.RATE_INPUT_MICRO_CENTS_PER_TOKEN
            + usage.cached_input_tokens * s.RATE_CACHED_INPUT_MICRO_CENTS_PER_TOKEN
            + usage.output_tokens * s.RATE_OUTPUT_MICRO_CENTS_PER_TOKEN
            # Reasoning tokens are billed at the OUTPUT rate per spec.
            + usage.reasoning_tokens * s.RATE_REASONING_MICRO_CENTS_PER_TOKEN
        )

    def calculate_cost_cents(self, usage: TokenUsage) -> int:
        """
        Cost rounded DOWN to whole cents (floor division). This is the
        value persisted to `usage_events.cost_cents`. Floor (not
        round-half-up) is the deliberate policy: it guarantees the sum
        of per-event costs never exceeds the cost computed from the
        aggregate token counts, which matters for invoice reconciliation.
        """
        return self.calculate_cost_micro_cents(usage) // MICRO_CENTS_PER_CENT

    def calculate_api_call_cost_cents(self) -> int:
        """
        Flat API calls are metered by count, not tokens, and — per this
        spec — are not separately priced per-call (their cost is baked
        into the plan's flat monthly price_cents). Returns 0 explicitly
        so callers never have to special-case event_type for cost.
        """
        return 0
