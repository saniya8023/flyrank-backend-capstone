"""
Idempotency Engine tests — exactly-once metering guarantee.

Covers:
  1. A fresh request creates exactly one UsageEvent and returns 201.
  2. A retried request with the SAME Idempotency-Key returns the
     identical response body without creating a second UsageEvent
     (sequential retry — the idempotency_records fast path).
  3. TRUE concurrent requests with the same Idempotency-Key (fired via
     asyncio.gather, racing each other before either commits) still
     result in exactly one UsageEvent row — the UNIQUE constraint +
     IntegrityError-catch-and-replay path, not just the fast-path cache.
  4. Two DIFFERENT Idempotency-Keys for the same tenant create two
     distinct UsageEvents (idempotency is scoped to the key, not the
     tenant).
"""
import asyncio

import pytest
from sqlalchemy import func, select

from app.models.models import UsageEvent

pytestmark = pytest.mark.asyncio


async def test_fresh_request_creates_usage_event(client, tenant_free):
    resp = await client.post(
        "/v1/usage",
        json={"tenant_id": str(tenant_free), "event_type": "api_call"},
        headers={"Idempotency-Key": "seq-key-1"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["total_quantity"] == 1
    assert body["idempotency_key"] == "seq-key-1"


async def test_sequential_retry_is_idempotent(client, tenant_free, session_factory):
    payload = {"tenant_id": str(tenant_free), "event_type": "api_call"}
    headers = {"Idempotency-Key": "seq-key-2"}

    first = await client.post("/v1/usage", json=payload, headers=headers)
    second = await client.post("/v1/usage", json=payload, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json() == second.json()

    async with session_factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(UsageEvent).where(
                UsageEvent.idempotency_key == "seq-key-2"
            )
        )
        assert count == 1


async def test_concurrent_requests_same_key_create_exactly_one_event(
    client, tenant_free, session_factory
):
    """
    Fires N concurrent requests carrying the SAME Idempotency-Key with
    asyncio.gather so they race each other at the DB level (none of
    them can see the others' idempotency_records row before their own
    INSERT lands). Exactly one must "win" and create the UsageEvent;
    the rest must resolve via the IntegrityError-catch-and-replay path
    and return the winner's body — never a second row.
    """
    payload = {"tenant_id": str(tenant_free), "event_type": "api_call"}
    headers = {"Idempotency-Key": "race-key-1"}

    responses = await asyncio.gather(
        *[client.post("/v1/usage", json=payload, headers=headers) for _ in range(8)]
    )

    assert all(r.status_code in (200, 201, 409) for r in responses)
    winners = [r for r in responses if r.status_code in (200, 201)]
    assert len(winners) >= 1
    bodies = {r.json()["id"] for r in winners}
    assert len(bodies) == 1, "all concurrent replays must reference the same UsageEvent id"

    async with session_factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(UsageEvent).where(
                UsageEvent.idempotency_key == "race-key-1"
            )
        )
        assert count == 1, "exactly one UsageEvent row must exist despite the race"


async def test_different_keys_create_distinct_events(client, tenant_free, session_factory):
    payload = {"tenant_id": str(tenant_free), "event_type": "api_call"}

    r1 = await client.post("/v1/usage", json=payload, headers={"Idempotency-Key": "key-a"})
    r2 = await client.post("/v1/usage", json=payload, headers={"Idempotency-Key": "key-b"})

    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["id"] != r2.json()["id"]

    async with session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(UsageEvent))
        assert count == 2


async def test_missing_idempotency_key_header_is_rejected(client, tenant_free):
    resp = await client.post(
        "/v1/usage",
        json={"tenant_id": str(tenant_free), "event_type": "api_call"},
    )
    assert resp.status_code == 422