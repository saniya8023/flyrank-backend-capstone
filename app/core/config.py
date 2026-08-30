"""
Centralized application configuration.

All monetary rates are pinned here as integer MICRO-CENTS per token
(1 cent = 10_000 micro-cents) so that per-token pricing can be
represented precisely without floating point error, even for
fractional-cent-per-token rates. All *stored* costs (usage_events,
invoices) are rolled up and persisted as integer CENTS.

Design rule: floats are NEVER used for money anywhere in this
codebase. See PricingCalculator for the rollup math.
"""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- App ---
    APP_NAME: str = "Usage Metering & Billing Engine"
    ENVIRONMENT: str = "development"
    DEBUG: bool = True

    # --- Database ---
    DATABASE_URL: str = (
        "postgresql+asyncpg://billing_user:billing_pass@localhost:5432/billing_engine"
    )
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20

    # --- Stripe ---
    STRIPE_API_KEY: str = "sk_test_placeholder"
    STRIPE_WEBHOOK_SECRET: str = "whsec_placeholder"
    STRIPE_PRICE_ID_PRO: str = "price_placeholder_pro"

    # --- Quota / Metering ---
    # Grace window (seconds) tolerated for clock skew on period rollovers.
    QUOTA_CLOCK_SKEW_SECONDS: int = 5
    # Default Retry-After (seconds) returned on 429 when the limit resets
    # at a known period boundary; used as a fallback if period end is
    # unavailable.
    DEFAULT_RETRY_AFTER_SECONDS: int = 3600

    # --- AI Token Pricing (integer MICRO-CENTS per token) ---
    # 1 cent = 10_000 micro-cents. Example: $0.03 / 1K input tokens
    # => 3 cents / 1000 tokens => 0.003 cents/token => 30 micro-cents/token.
    RATE_INPUT_MICRO_CENTS_PER_TOKEN: int = 30
    RATE_CACHED_INPUT_MICRO_CENTS_PER_TOKEN: int = 8
    RATE_OUTPUT_MICRO_CENTS_PER_TOKEN: int = 150
    # Reasoning tokens are billed at the OUTPUT rate per business rules;
    # this constant exists for explicitness / future divergence and
    # currently mirrors RATE_OUTPUT_MICRO_CENTS_PER_TOKEN.
    RATE_REASONING_MICRO_CENTS_PER_TOKEN: int = 150

    # --- Plan defaults (seed values; source of truth is the `plans` table) ---
    FREE_PLAN_API_CALL_LIMIT: int = 1_000
    FREE_PLAN_TOKEN_LIMIT: int = 100_000
    PRO_PLAN_API_CALL_LIMIT: int = 100_000
    PRO_PLAN_TOKEN_LIMIT: int = 10_000_000
    PRO_PLAN_PRICE_CENTS: int = 4_900


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton — safe for FastAPI Depends()."""
    return Settings()
