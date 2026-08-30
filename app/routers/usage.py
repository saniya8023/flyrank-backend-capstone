"""
POST /v1/usage — the single billable-event ingestion endpoint.

Status code contract (never a bare 500 for expected failures):
  201 Created            -> usage recorded (fresh write).
  200 OK                 -> usage recorded previously; idempotent replay.
  402 Payment Required   -> tenant/subscription not in a usable state.
  429 Too Many Requests  -> would exceed the plan's period limit;
                             Retry-After header set.
  404 Not Found          -> unknown tenant_id.
  422 Unprocessable       -> handled automatically by FastAPI/Pydantic
                             for malformed bodies.
"""
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.session import get_db
from app.models.models import EventType, Tenant
from app.schemas.schemas import ErrorDetail, ErrorResponse, UsageEventCreate
from app.services.metering import MeteringEngine
from app.services.pricing import TokenUsage
from app.services.quota import PaymentRequiredError, QuotaExceededError

router = APIRouter(prefix="/v1/usage", tags=["usage"])


@router.post(
    "",
    status_code=201,
    responses={
        402: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)
async def record_usage(
    payload: UsageEventCreate,
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=255),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    tenant = await db.get(Tenant, payload.tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=404,
            detail=ErrorDetail(
                code="NOT_FOUND", message=f"Tenant {payload.tenant_id} not found."
            ).model_dump(),
        )

    engine = MeteringEngine(db=db, settings=settings)
    token_usage = TokenUsage(
        input_tokens=payload.input_tokens,
        cached_input_tokens=payload.cached_input_tokens,
        output_tokens=payload.output_tokens,
        reasoning_tokens=payload.reasoning_tokens,
    )

    try:
        result = await engine.record_usage(
            tenant=tenant,
            idempotency_key=idempotency_key,
            event_type=EventType(payload.event_type),
            token_usage=token_usage,
        )
    except QuotaExceededError as exc:
        response.headers["Retry-After"] = str(exc.retry_after_seconds)
        raise HTTPException(
            status_code=429,
            detail=ErrorDetail(
                code="QUOTA_EXCEEDED",
                message=str(exc),
                limit=exc.limit,
                used=exc.used,
                requested=exc.requested,
                retry_after_seconds=exc.retry_after_seconds,
            ).model_dump(),
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    except PaymentRequiredError as exc:
        raise HTTPException(
            status_code=402,
            detail=ErrorDetail(code="PAYMENT_REQUIRED", message=str(exc)).model_dump(),
        ) from exc

    # Idempotent replays of an original 201 are returned as 200, since no
    # new resource was created on *this* request — the original creation
    # already happened. Fresh writes and same-key retries of a non-201
    # original response are returned verbatim.
    status_code = result.status_code
    if result.replayed and result.status_code == 201:
        status_code = 200

    response.status_code = status_code
    return result.body
