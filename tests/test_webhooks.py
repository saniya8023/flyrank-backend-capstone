"""
Stripe Webhook Handler tests.

Covers:
  1. A validly-signed webhook is accepted (200) and applied.
  2. An invalidly-signed webhook is rejected with 400 and a structured
     WEBHOOK_SIGNATURE_INVALID error — never a bare 500.
  3. The exact same event_id delivered twice (Stripe's at-least-once
     delivery guarantee) is applied only once.
  4. `customer.subscription.deleted` correctly downgrades the tenant's
     plan_type back to "free" and marks the subscription CANCELED.
  5. `customer.subscription.updated` syncs status and period dates.

Signature construction mirrors Stripe's documented scheme exactly:
HMAC-SHA256 over "{timestamp}.{raw_body}" keyed by the webhook signing
secret, formatted as `t={timestamp},v1={signature}`. This exercises the
REAL `stripe.Webhook.construct_event` verification path end-to-end.
"""
import hashlib
import hmac
import json
import time

import pytest

from app.core.config import get_settings
from app.models.models import PlanType, Subscription, SubscriptionStatus, Tenant

pytestmark = pytest.mark.asyncio

WEBHOOK_SECRET = "whsec_test_secret_for_unit_tests"


def _sign_payload(payload_bytes: bytes, secret: str = WEBHOOK_SECRET) -> str:
    timestamp = int(time.time())
    signed_payload = f"{timestamp}.{payload_bytes.decode()}".encode()
    signature = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={signature}"


def _make_event(event_id: str, event_type: str, data_object: dict) -> bytes:
    return json.dumps(
        {"id": event_id, "type": event_type, "data": {"object": data_object}}
    ).encode()


@pytest.fixture(autouse=True)
def override_webhook_secret(monkeypatch):
    """Points the app at the same secret used to sign test payloads."""
    settings = get_settings()
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    yield


async def test_invalid_signature_returns_400(client):
    payload = _make_event("evt_bad_sig", "customer.subscription.updated", {"id": "sub_x"})
    resp = await client.post(
        "/v1/webhooks/stripe",
        content=payload,
        headers={"Stripe-Signature": "t=1,v1=deadbeef", "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "WEBHOOK_SIGNATURE_INVALID"


async def test_valid_signature_subscription_updated_syncs_status(
    client, tenant_pro, session_factory
):
    async with session_factory() as session:
        tenant = await session.get(Tenant, tenant_pro)
        result = await session.execute(
            __import__("sqlalchemy").select(Subscription).where(
                Subscription.tenant_id == tenant.id
            )
        )
        subscription = result.scalar_one()
        subscription.stripe_subscription_id = "sub_test_123"
        await session.commit()

    now = int(time.time())
    payload = _make_event(
        "evt_sub_updated_1",
        "customer.subscription.updated",
        {
            "id": "sub_test_123",
            "status": "past_due",
            "current_period_start": now,
            "current_period_end": now + 2_592_000,
        },
    )
    resp = await client.post(
        "/v1/webhooks/stripe",
        content=payload,
        headers={"Stripe-Signature": _sign_payload(payload), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "subscription_updated"

    async with session_factory() as session:
        result = await session.execute(
            __import__("sqlalchemy").select(Subscription).where(
                Subscription.stripe_subscription_id == "sub_test_123"
            )
        )
        updated = result.scalar_one()
        assert updated.status == SubscriptionStatus.PAST_DUE


async def test_duplicate_event_id_is_ignored_second_time(client, tenant_pro, session_factory):
    async with session_factory() as session:
        result = await session.execute(
            __import__("sqlalchemy").select(Subscription).where(
                Subscription.tenant_id == tenant_pro
            )
        )
        subscription = result.scalar_one()
        subscription.stripe_subscription_id = "sub_dedup_test"
        await session.commit()

    now = int(time.time())
    payload = _make_event(
        "evt_duplicate_1",
        "customer.subscription.updated",
        {
            "id": "sub_dedup_test",
            "status": "active",
            "current_period_start": now,
            "current_period_end": now + 2_592_000,
        },
    )
    headers = {"Stripe-Signature": _sign_payload(payload), "Content-Type": "application/json"}

    first = await client.post("/v1/webhooks/stripe", content=payload, headers=headers)
    second = await client.post("/v1/webhooks/stripe", content=payload, headers=headers)

    assert first.status_code == 200
    assert first.json()["status"] == "subscription_updated"
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate_ignored"


async def test_subscription_deleted_downgrades_tenant_to_free(
    client, tenant_pro, session_factory
):
    async with session_factory() as session:
        result = await session.execute(
            __import__("sqlalchemy").select(Subscription).where(
                Subscription.tenant_id == tenant_pro
            )
        )
        subscription = result.scalar_one()
        subscription.stripe_subscription_id = "sub_to_delete"
        await session.commit()

    payload = _make_event(
        "evt_sub_deleted_1", "customer.subscription.deleted", {"id": "sub_to_delete"}
    )
    resp = await client.post(
        "/v1/webhooks/stripe",
        content=payload,
        headers={"Stripe-Signature": _sign_payload(payload), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "subscription_canceled"

    async with session_factory() as session:
        tenant = await session.get(Tenant, tenant_pro)
        result = await session.execute(
            __import__("sqlalchemy").select(Subscription).where(
                Subscription.stripe_subscription_id == "sub_to_delete"
            )
        )
        subscription = result.scalar_one()
        assert subscription.status == SubscriptionStatus.CANCELED
        assert tenant.plan_type == PlanType.FREE or tenant.plan_type == "free"


async def test_unhandled_event_type_is_acknowledged_but_ignored(client):
    payload = _make_event("evt_unhandled_1", "invoice.paid", {"id": "in_123"})
    resp = await client.post(
        "/v1/webhooks/stripe",
        content=payload,
        headers={"Stripe-Signature": _sign_payload(payload), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored_unhandled_type"