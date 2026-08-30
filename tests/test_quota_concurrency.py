"""
Quota concurrency correctness — a race distinct from idempotency.

test_idempotency.py's concurrency test races N requests carrying the
SAME Idempotency-Key, which proves the idempotency guarantee (exactly
one UsageEvent survives) but does NOT exercise QuotaEnforcer's
`SELECT ... FOR UPDATE` lock at all: since every request after the
first is short-circuited by the idempotency fast path or the UNIQUE
constraint, only ONE request ever actually reaches the quota-check
code path.

This file races N requests with N *different* Idempotency-Keys against
a tight quota limit — this is the only way to actually exercise
concurrent, independent check-then-act sequences through
QuotaEnforcer, and therefore the only test that can catch quota
overselling under concurrency.

This test caught a real bug during development (see BUILDLOG.md /
EVIDENCE.md, Phase 3): before `app/db/session.py` was changed to open
SQLite transactions with `BEGIN IMMEDIATE`, 5 concurrent requests with
5 different keys against a plan with `api_call_limit=1` all returned
201 — SQLite silently ignores `SELECT ... FOR UPDATE`, so nothing
serialized the racing reads. The fix (`_enable_sqlite_immediate_transactions`
in app/db/session.py) closes this at the SQLite-connection level; on
PostgreSQL, `SELECT ... FOR UPDATE` already does this correctly at
the row level, so the fix is a no-op there.
"""
import asyncio

import pytest
from sqlalchemy import func, select

from app.models.models import UsageEvent

pytestmark = pytest.mark.asyncio


async def test_concurrent_different_keys_never_oversell_quota(
    client, tenant_free, session_factory
):
    """
    tenant_free's plan has api_call_limit=5 and already has zero usage.
    Fire 8 concurrent requests, each with a UNIQUE Idempotency-Key, so
    all 8 independently reach QuotaEnforcer.check_and_reserve(). At
    most 5 may succeed — the plan's exact limit — no matter how the
    requests interleave.
    """
    payload = {"tenant_id": str(tenant_free), "event_type": "api_call"}

    responses = await asyncio.gather(
        *[
            client.post(
                "/v1/usage", json=payload, headers={"Idempotency-Key": f"unique-race-{i}"}
            )
            for i in range(8)
        ]
    )

    successes = [r for r in responses if r.status_code == 201]
    rejections = [r for r in responses if r.status_code == 429]

    assert len(successes) == 5, (
        f"expected exactly 5 successful inserts (the plan limit), got "
        f"{len(successes)} — quota was oversold under concurrency"
    )
    assert len(rejections) == 3
    for r in rejections:
        assert r.json()["detail"]["code"] == "QUOTA_EXCEEDED"

    async with session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(UsageEvent))
        assert count == 5, "no more than the plan limit may ever be persisted"


async def test_concurrent_different_keys_at_tight_limit_of_one(
    client, session_factory, seeded_plans
):
    """
    Tightest possible case: api_call_limit=1, 5 concurrent requests with
    5 different keys. Exactly one may succeed. This is the scenario
    that failed 5-for-5 before the BEGIN IMMEDIATE fix (see BUILDLOG.md).
    """
    from datetime import datetime, timedelta, timezone

    from app.models.models import Plan, Subscription, SubscriptionStatus, Tenant

    async with session_factory() as session:
        tight_plan = Plan(name="tight", api_call_limit=1, token_limit=1000, price_cents=0)
        session.add(tight_plan)
        await session.flush()
        tenant = Tenant(name="Tight Co", plan_type="free")
        session.add(tenant)
        await session.flush()
        now = datetime.now(timezone.utc)
        sub = Subscription(
            tenant_id=tenant.id,
            plan_id=tight_plan.id,
            status=SubscriptionStatus.ACTIVE,
            current_period_start=now,
            current_period_end=now + timedelta(days=30),
        )
        session.add(sub)
        await session.commit()
        tenant_id = tenant.id

    payload = {"tenant_id": str(tenant_id), "event_type": "api_call"}
    responses = await asyncio.gather(
        *[
            client.post(
                "/v1/usage", json=payload, headers={"Idempotency-Key": f"tight-race-{i}"}
            )
            for i in range(5)
        ]
    )

    successes = [r for r in responses if r.status_code == 201]
    assert len(successes) == 1, (
        f"expected exactly 1 successful insert against api_call_limit=1, "
        f"got {len(successes)}"
    )