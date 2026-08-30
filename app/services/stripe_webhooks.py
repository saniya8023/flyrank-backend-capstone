"""
StripeWebhookHandler — verifies, deduplicates, and applies Stripe Test
Mode webhook events that affect billing state.

Security: signature verification via `stripe.Webhook.construct_event`
happens in the ROUTER (it needs the raw request body bytes, which
FastAPI's body-parsing can mangle if done twice) — this service
receives the already-verified `stripe.Event` object and only handles
business logic + deduplication.

Deduplication: Stripe delivers events at-least-once, so the same
`evt_...` id can arrive multiple times (retries, multiple endpoints).
`processed_webhooks.event_id` is the PRIMARY KEY; we INSERT it before
applying side effects and treat a UNIQUE VIOLATION as "already handled,
no-op, return 200" — never re-apply a plan change twice.

NOTE (fixed in Phase 2): `event["data"]["object"]` is a
`stripe.StripeObject`, not a plain dict. Newer versions of the `stripe`
SDK make `StripeObject.get(...)` raise `AttributeError` instead of
behaving like `dict.get` — it forces callers to convert via
`.to_dict()` first. Every handler below does that conversion
immediately after unwrapping the event object, before any `.get()`
calls, to avoid that trap.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import (
    Plan,
    ProcessedWebhook,
    Subscription,
    SubscriptionStatus,
    Tenant,
)

# Stripe subscription statuses we treat as "usable" vs. terminal.
_ACTIVE_STRIPE_STATUSES = {"active", "trialing"}
_TERMINAL_STRIPE_STATUSES = {"canceled", "unpaid", "incomplete_expired"}


class StripeWebhookHandler:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def _mark_processed_or_skip(self, event_id: str, event_type: str) -> bool:
        """
        Returns True if this is the first time we're seeing event_id
        (caller should proceed to apply side effects). Returns False if
        it was already processed (caller should no-op).
        """
        try:
            self._db.add(ProcessedWebhook(event_id=event_id, event_type=event_type))
            await self._db.flush()
            return True
        except IntegrityError:
            await self._db.rollback()
            return False

    async def _get_tenant_by_customer_id(self, stripe_customer_id: str) -> Tenant | None:
        result = await self._db.execute(
            select(Tenant).where(Tenant.stripe_customer_id == stripe_customer_id)
        )
        return result.scalar_one_or_none()

    async def _get_plan_by_stripe_price(self, price_id: str) -> Plan | None:
        # In this schema, plan<->price mapping is by convention (pro plan
        # <-> STRIPE_PRICE_ID_PRO); a real system would store price_id on
        # Plan. Kept simple here and documented as a known simplification.
        result = await self._db.execute(select(Plan).where(Plan.name == "pro"))
        return result.scalar_one_or_none()

    async def handle_event(self, event: dict) -> dict:
        """
        `event` is `stripe.Event` (dict-like) already verified by the
        router via `stripe.Webhook.construct_event`. Dispatches on
        `event["type"]`.
        """
        event_id = event["id"]
        event_type = event["type"]

        is_new = await self._mark_processed_or_skip(event_id, event_type)
        if not is_new:
            return {"status": "duplicate_ignored", "event_id": event_id}

        handler = self._DISPATCH.get(event_type)
        if handler is None:
            await self._db.commit()
            return {"status": "ignored_unhandled_type", "event_type": event_type}

        result = await handler(self, event)
        await self._db.commit()
        return result

    async def _handle_checkout_completed(self, event: dict) -> dict:
        obj = event["data"]["object"]
        obj = obj.to_dict() if hasattr(obj, "to_dict") else dict(obj)
        stripe_customer_id = obj.get("customer")
        stripe_subscription_id = obj.get("subscription")

        tenant = await self._get_tenant_by_customer_id(stripe_customer_id)
        if tenant is None:
            return {"status": "no_matching_tenant", "stripe_customer_id": stripe_customer_id}

        plan = await self._get_plan_by_stripe_price(
            obj.get("line_items", {}).get("data", [{}])[0].get("price", {}).get("id", "")
        )
        if plan is None:
            return {"status": "no_matching_plan"}

        now = datetime.now(timezone.utc)
        subscription = Subscription(
            tenant_id=tenant.id,
            stripe_subscription_id=stripe_subscription_id,
            plan_id=plan.id,
            status=SubscriptionStatus.ACTIVE,
            current_period_start=now,
            current_period_end=now + timedelta(days=30),
        )
        self._db.add(subscription)
        tenant.plan_type = plan.name  # type: ignore[assignment]
        return {"status": "subscription_created", "tenant_id": str(tenant.id)}

    async def _handle_subscription_updated(self, event: dict) -> dict:
        obj = event["data"]["object"]
        obj = obj.to_dict() if hasattr(obj, "to_dict") else dict(obj)
        stripe_subscription_id = obj["id"]

        result = await self._db.execute(
            select(Subscription).where(
                Subscription.stripe_subscription_id == stripe_subscription_id
            )
        )
        subscription = result.scalar_one_or_none()
        if subscription is None:
            return {"status": "no_matching_subscription", "stripe_subscription_id": stripe_subscription_id}

        stripe_status = obj.get("status", "")
        if stripe_status in _ACTIVE_STRIPE_STATUSES:
            subscription.status = SubscriptionStatus.ACTIVE
        elif stripe_status == "past_due":
            subscription.status = SubscriptionStatus.PAST_DUE
        elif stripe_status in _TERMINAL_STRIPE_STATUSES:
            subscription.status = SubscriptionStatus.CANCELED

        period_start = obj.get("current_period_start")
        period_end = obj.get("current_period_end")
        if period_start is not None:
            subscription.current_period_start = datetime.fromtimestamp(
                period_start, tz=timezone.utc
            )
        if period_end is not None:
            subscription.current_period_end = datetime.fromtimestamp(
                period_end, tz=timezone.utc
            )

        return {"status": "subscription_updated", "subscription_id": subscription.id}

    async def _handle_subscription_deleted(self, event: dict) -> dict:
        obj = event["data"]["object"]
        obj = obj.to_dict() if hasattr(obj, "to_dict") else dict(obj)
        stripe_subscription_id = obj["id"]

        result = await self._db.execute(
            select(Subscription).where(
                Subscription.stripe_subscription_id == stripe_subscription_id
            )
        )
        subscription = result.scalar_one_or_none()
        if subscription is None:
            return {"status": "no_matching_subscription", "stripe_subscription_id": stripe_subscription_id}

        subscription.status = SubscriptionStatus.CANCELED

        tenant = await self._db.get(Tenant, subscription.tenant_id)
        if tenant is not None:
            tenant.plan_type = "free"  # type: ignore[assignment]

        return {"status": "subscription_canceled", "subscription_id": subscription.id}

    _DISPATCH = {
        "checkout.session.completed": _handle_checkout_completed,
        "customer.subscription.updated": _handle_subscription_updated,
        "customer.subscription.deleted": _handle_subscription_deleted,
    }