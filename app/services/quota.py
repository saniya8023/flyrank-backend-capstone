"""
QuotaEnforcer — decides whether a requested unit of usage may proceed.

Concurrency safety
------------------
The check-then-act sequence (read current usage -> compare to limit ->
allow the caller to insert a usage row) is a classic TOCTOU race under
concurrent requests. We close that race with `SELECT ... FOR UPDATE`
on the tenant's active `Subscription` row: every quota check for a
given tenant takes a row lock on that tenant's subscription before
computing the rollup, so two concurrent requests for the same tenant
are serialized at the database level and cannot both "see" room for
a request that only one of them can actually satisfy.

This is called from within the same transaction that will insert the
UsageEvent (see MeteringEngine), so the lock is held for the full
check-and-insert, not just the read.

Error semantics
----------------
- 429 Too Many Requests: the tenant is on a valid, current subscription
  but the requested usage would exceed the plan's numeric limit for
  this billing period. Retryable later (after period reset) -> we
  return `Retry-After` seconds until `current_period_end`.
- 402 Payment Required: the tenant's subscription/account is not in a
  state that permits usage at all regardless of quota numbers — e.g.
  status is PAST_DUE/CANCELED/SUSPENDED, or no active subscription
  exists. This signals "fix billing", not "wait and retry".
"""
from dataclasses import dataclass
from datetime import datetime, timezone


def _as_aware_utc(value: datetime) -> datetime:
    """
    Normalizes a datetime to timezone-aware UTC.

    SQLite (used by the test suite via aiosqlite) has no native
    timezone-aware column type, so `DateTime(timezone=True)` values
    round-trip as naive datetimes there, while PostgreSQL (production)
    correctly preserves tzinfo. All datetimes in this system are written
    as UTC, so a naive value is assumed to already be UTC and is simply
    tagged rather than converted.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import (
    EventType,
    Plan,
    Subscription,
    SubscriptionStatus,
    Tenant,
    TenantStatus,
    UsageEvent,
)


class QuotaExceededError(Exception):
    """Raised -> router maps to HTTP 429 with Retry-After."""

    def __init__(self, limit: int, used: int, requested: int, retry_after_seconds: int):
        self.limit = limit
        self.used = used
        self.requested = requested
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            f"Quota exceeded: used={used} requested={requested} limit={limit}"
        )


class PaymentRequiredError(Exception):
    """Raised -> router maps to HTTP 402."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class QuotaCheckResult:
    plan: Plan
    subscription: Subscription
    api_calls_used: int
    tokens_used: int


class QuotaEnforcer:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def _get_locked_active_subscription(self, tenant_id) -> Subscription | None:
        """
        Row-locks the tenant's current subscription (FOR UPDATE) so
        concurrent quota checks for the same tenant serialize. Returns
        None if there is no ACTIVE subscription — caller must then
        decide free-tier-by-default vs. hard 402, per business rules.
        """
        stmt = (
            select(Subscription)
            .where(
                Subscription.tenant_id == tenant_id,
                Subscription.status == SubscriptionStatus.ACTIVE,
            )
            .order_by(Subscription.current_period_end.desc())
            .limit(1)
            .with_for_update()
        )
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    async def _sum_period_usage(
        self, tenant_id, period_start: datetime, period_end: datetime
    ) -> tuple[int, int]:
        """Returns (api_calls_used, tokens_used) for the current period."""
        stmt = select(
            func.coalesce(
                func.sum(UsageEvent.total_quantity).filter(
                    UsageEvent.event_type == EventType.API_CALL
                ),
                0,
            ),
            func.coalesce(
                func.sum(UsageEvent.total_quantity).filter(
                    UsageEvent.event_type == EventType.AI_TOKEN
                ),
                0,
            ),
        ).where(
            UsageEvent.tenant_id == tenant_id,
            UsageEvent.timestamp >= period_start,
            UsageEvent.timestamp < period_end,
        )
        result = await self._db.execute(stmt)
        api_calls_used, tokens_used = result.one()
        return int(api_calls_used), int(tokens_used)

    async def check_and_reserve(
        self,
        tenant: Tenant,
        event_type: EventType,
        requested_quantity: int,
    ) -> QuotaCheckResult:
        """
        Validates that `requested_quantity` more units of `event_type`
        may be consumed right now. Raises PaymentRequiredError (402) or
        QuotaExceededError (429) if not. Returns the locked
        subscription + plan + current rollup on success so the caller
        (MeteringEngine) can insert the UsageEvent within the same
        transaction/lock scope without re-querying.

        NOTE: this does not itself write the UsageEvent — it only
        validates and holds the row lock. The lock is released when the
        enclosing transaction commits/rolls back.
        """
        if tenant.status != TenantStatus.ACTIVE:
            raise PaymentRequiredError(
                f"Tenant account status is '{tenant.status.value}'; usage is blocked "
                "until the account is reactivated."
            )

        subscription = await self._get_locked_active_subscription(tenant.id)
        if subscription is None:
            raise PaymentRequiredError(
                "No active subscription found for tenant. A valid subscription "
                "(including the free plan) is required before usage can be recorded."
            )

        now = datetime.now(timezone.utc)
        period_end = _as_aware_utc(subscription.current_period_end)
        if now >= period_end:
            # Defensive: a subscription whose period has lapsed without
            # being rolled forward (e.g. Stripe webhook lag) is treated
            # as requiring payment-side attention rather than silently
            # metering into a stale period.
            raise PaymentRequiredError(
                "Subscription billing period has ended and has not yet been "
                "renewed. Please retry shortly or contact billing support."
            )

        plan = await self._db.get(Plan, subscription.plan_id)
        if plan is None:
            raise PaymentRequiredError("Subscription references an unknown plan.")

        api_calls_used, tokens_used = await self._sum_period_usage(
            tenant.id, subscription.current_period_start, subscription.current_period_end
        )

        if event_type == EventType.API_CALL:
            limit = plan.api_call_limit
            used = api_calls_used
        else:
            limit = plan.token_limit
            used = tokens_used

        if used + requested_quantity > limit:
            retry_after = max(1, int((period_end - now).total_seconds()))
            raise QuotaExceededError(
                limit=limit,
                used=used,
                requested=requested_quantity,
                retry_after_seconds=retry_after,
            )

        return QuotaCheckResult(
            plan=plan,
            subscription=subscription,
            api_calls_used=api_calls_used,
            tokens_used=tokens_used,
        )
