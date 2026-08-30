"""
POST /v1/webhooks/stripe

Signature verification MUST happen against the raw request bytes
(`await request.body()`), never against a re-serialized Pydantic model
of the payload — Stripe signs the exact bytes it sent, and JSON
re-serialization can change key order/whitespace and break the HMAC
check. This is why this router uses `Request` directly instead of a
Pydantic body model.
"""
import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.session import get_db
from app.schemas.schemas import ErrorDetail, ErrorResponse
from app.services.stripe_webhooks import StripeWebhookHandler

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


@router.post(
    "/stripe",
    status_code=200,
    responses={400: {"model": ErrorResponse}},
)
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(..., alias="Stripe-Signature"),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    raw_body = await request.body()

    try:
        event = stripe.Webhook.construct_event(
            payload=raw_body,
            sig_header=stripe_signature,
            secret=settings.STRIPE_WEBHOOK_SECRET,
        )
    except (ValueError, stripe.error.SignatureVerificationError) as exc:
        raise HTTPException(
            status_code=400,
            detail=ErrorDetail(
                code="WEBHOOK_SIGNATURE_INVALID",
                message=f"Webhook signature verification failed: {exc}",
            ).model_dump(),
        ) from exc

    handler = StripeWebhookHandler(db=db)
    result = await handler.handle_event(event)
    return result
