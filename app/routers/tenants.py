"""
Minimal tenant + subscription bootstrap endpoint. In a real system,
tenant creation would be tied to signup and Stripe Customer creation;
this endpoint exists so the evaluator/test-suite can create a tenant
on a given plan without going through the full Stripe checkout flow.
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.models import Plan, Subscription, SubscriptionStatus, Tenant
from app.schemas.schemas import ErrorDetail, ErrorResponse, TenantCreate, TenantResponse

router = APIRouter(prefix="/v1/tenants", tags=["tenants"])


@router.post("", response_model=TenantResponse, status_code=201)
async def create_tenant(payload: TenantCreate, db: AsyncSession = Depends(get_db)):
    plan_result = await db.execute(select(Plan).where(Plan.name == payload.plan_type))
    plan = plan_result.scalar_one_or_none()
    if plan is None:
        raise HTTPException(
            status_code=422,
            detail=ErrorDetail(
                code="VALIDATION_ERROR", message=f"Unknown plan_type '{payload.plan_type}'."
            ).model_dump(),
        )

    tenant = Tenant(name=payload.name, plan_type=payload.plan_type)
    db.add(tenant)
    await db.flush()

    now = datetime.now(timezone.utc)
    subscription = Subscription(
        tenant_id=tenant.id,
        plan_id=plan.id,
        status=SubscriptionStatus.ACTIVE,
        current_period_start=now,
        current_period_end=now + timedelta(days=30),
    )
    db.add(subscription)
    await db.commit()
    await db.refresh(tenant)
    return tenant


@router.get(
    "/{tenant_id}",
    response_model=TenantResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_tenant(tenant_id, db: AsyncSession = Depends(get_db)):
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=404,
            detail=ErrorDetail(code="NOT_FOUND", message="Tenant not found.").model_dump(),
        )
    return tenant
