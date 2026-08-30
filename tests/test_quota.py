"""
Quota Enforcement Logic tests — boundary conditions for 429/402.

Covers:
  1. Requests strictly under the limit succeed.
  2. A request that lands EXACTLY on the limit succeeds (used +
     requested == limit is allowed; only > limit is rejected).
  3. A request that would exceed the limit by 1 unit is rejected with
     429, a structured QUOTA_EXCEEDED payload, and a Retry-After header.
  4. Token-based quota (ai_token) is enforced independently from
     api_call quota — exhausting one does not block the other.
  5. A tenant with no active subscription gets 402, not 429/500.
  6. A suspended tenant gets 402 even with quota headroom remaining.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.models import Tenant, TenantStatus

pytestmark = pytest.mark.asyncio


async def test_usage_under_limit_succeeds(client, tenant_free):
    for i in range(4):
        resp = await client.post(
            "/v1/usage",
            json={"tenant_id": str(tenant_free), "event_type": "api_call"},
            headers={"Idempotency-Key": f"under-{i}"},
        )
        assert resp.status_code == 201


async def test_usage_exactly_at_limit_succeeds(client, tenant_free):
    for i in range(5):
        resp = await client.post(
            "/v1/usage",
            json={"tenant_id": str(tenant_free), "event_type": "api_call"},
            headers={"Idempotency-Key": f"exact-{i}"},
        )
        assert resp.status_code == 201, f"call {i} should succeed at/under the limit"


async def test_usage_one_over_limit_returns_429_with_retry_after(client, tenant_free):
    for i in range(5):
        resp = await client.post(
            "/v1/usage",
            json={"tenant_id": str(tenant_free), "event_type": "api_call"},
            headers={"Idempotency-Key": f"fill-{i}"},
        )
        assert resp.status_code == 201

    overflow = await client.post(
        "/v1/usage",
        json={"tenant_id": str(tenant_free), "event_type": "api_call"},
        headers={"Idempotency-Key": "overflow-1"},
    )
    assert overflow.status_code == 429
    assert "Retry-After" in overflow.headers
    body = overflow.json()
    assert body["detail"]["code"] == "QUOTA_EXCEEDED"
    assert body["detail"]["limit"] == 5
    assert body["detail"]["used"] == 5
    assert body["detail"]["requested"] == 1


async def test_token_quota_independent_from_api_call_quota(client, tenant_free):
    for i in range(5):
        resp = await client.post(
            "/v1/usage",
            json={"tenant_id": str(tenant_free), "event_type": "api_call"},
            headers={"Idempotency-Key": f"apicall-{i}"},
        )
        assert resp.status_code == 201

    token_resp = await client.post(
        "/v1/usage",
        json={
            "tenant_id": str(tenant_free),
            "event_type": "ai_token",
            "input_tokens": 10,
            "output_tokens": 5,
        },
        headers={"Idempotency-Key": "token-still-ok"},
    )
    assert token_resp.status_code == 201


async def test_token_quota_exceeded_returns_429(client, tenant_free):
    resp = await client.post(
        "/v1/usage",
        json={
            "tenant_id": str(tenant_free),
            "event_type": "ai_token",
            "input_tokens": 9_000,
            "output_tokens": 500,
        },
        headers={"Idempotency-Key": "big-token-1"},
    )
    assert resp.status_code == 201

    overflow = await client.post(
        "/v1/usage",
        json={
            "tenant_id": str(tenant_free),
            "event_type": "ai_token",
            "input_tokens": 600,
        },
        headers={"Idempotency-Key": "big-token-2"},
    )
    assert overflow.status_code == 429
    assert overflow.json()["detail"]["code"] == "QUOTA_EXCEEDED"


async def test_no_active_subscription_returns_402(client, session_factory):
    async with session_factory() as session:
        tenant = Tenant(name="No Sub Co", plan_type="free")
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        tenant_id = tenant.id

    resp = await client.post(
        "/v1/usage",
        json={"tenant_id": str(tenant_id), "event_type": "api_call"},
        headers={"Idempotency-Key": "no-sub-1"},
    )
    assert resp.status_code == 402
    assert resp.json()["detail"]["code"] == "PAYMENT_REQUIRED"


async def test_suspended_tenant_returns_402_even_with_quota_headroom(
    client, tenant_free, session_factory
):
    async with session_factory() as session:
        tenant = await session.get(Tenant, tenant_free)
        tenant.status = TenantStatus.SUSPENDED
        await session.commit()

    resp = await client.post(
        "/v1/usage",
        json={"tenant_id": str(tenant_free), "event_type": "api_call"},
        headers={"Idempotency-Key": "suspended-1"},
    )
    assert resp.status_code == 402
    assert resp.json()["detail"]["code"] == "PAYMENT_REQUIRED"


async def test_unknown_tenant_returns_404(client):
    resp = await client.post(
        "/v1/usage",
        json={"tenant_id": str(uuid.uuid4()), "event_type": "api_call"},
        headers={"Idempotency-Key": "unknown-tenant-1"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "NOT_FOUND"


async def test_quota_status_endpoint_reflects_usage(client, tenant_free):
    await client.post(
        "/v1/usage",
        json={"tenant_id": str(tenant_free), "event_type": "api_call"},
        headers={"Idempotency-Key": "status-check-1"},
    )
    resp = await client.get(f"/v1/tenants/{tenant_free}/quota")
    assert resp.status_code == 200
    body = resp.json()
    assert body["api_calls_used"] == 1
    assert body["api_call_limit"] == 5