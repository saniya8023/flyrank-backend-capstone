"""GET /v1/tenants/{tenant_id}/quota — read-only quota status, no writes."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.models import EventType, Plan, Subscription, SubscriptionStatus, Tenant, UsageEvent
from app.schemas.schemas import ErrorDetail, ErrorResponse, QuotaStatus
from sqlalchemy import func

router = APIRouter(prefix="/v1/tenants", tags=["quota"])


@router.get(
    "/{tenant_id}/quota",
    response_model=QuotaStatus,
    responses={404: {"model": ErrorResponse}},
)
async def get_quota_status(tenant_id, db: AsyncSession = Depends(get_db)):
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=404,
            detail=ErrorDetail(code="NOT_FOUND", message="Tenant not found.").model_dump(),
        )

    sub_result = await db.execute(
        select(Subscription)
        .where(Subscription.tenant_id == tenant.id, Subscription.status == SubscriptionStatus.ACTIVE)
        .order_by(Subscription.current_period_end.desc())
        .limit(1)
    )
    subscription = sub_result.scalar_one_or_none()
    if subscription is None:
        raise HTTPException(
            status_code=404,
            detail=ErrorDetail(
                code="NOT_FOUND", message="No active subscription for this tenant."
            ).model_dump(),
        )

    plan = await db.get(Plan, subscription.plan_id)

    usage_result = await db.execute(
        select(
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
            UsageEvent.tenant_id == tenant.id,
            UsageEvent.timestamp >= subscription.current_period_start,
            UsageEvent.timestamp < subscription.current_period_end,
        )
    )
    api_calls_used, tokens_used = usage_result.one()

    return QuotaStatus(
        tenant_id=tenant.id,
        plan_name=plan.name,
        api_calls_used=int(api_calls_used),
        api_call_limit=plan.api_call_limit,
        tokens_used=int(tokens_used),
        token_limit=plan.token_limit,
        period_start=subscription.current_period_start,
        period_end=subscription.current_period_end,
    )
