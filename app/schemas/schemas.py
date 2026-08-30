"""
Pydantic v2 schemas (DTOs). These are the ONLY shapes that cross the
HTTP boundary — routers never accept or return ORM models directly.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# --------------------------------------------------------------------------
# Usage ingestion
# --------------------------------------------------------------------------

class UsageEventCreate(BaseModel):
    """
    Payload for POST /v1/usage. `Idempotency-Key` itself travels as a
    request HEADER (see routers/usage.py), not in this body, per
    standard idempotent-API convention — but it is echoed here for
    services that need it in a single object.
    """

    tenant_id: uuid.UUID
    event_type: Literal["api_call", "ai_token"]
    input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_token_fields(self) -> "UsageEventCreate":
        if self.event_type == "api_call":
            if any(
                v != 0
                for v in (
                    self.input_tokens,
                    self.cached_input_tokens,
                    self.output_tokens,
                    self.reasoning_tokens,
                )
            ):
                raise ValueError(
                    "api_call events must not carry token fields; use ai_token instead"
                )
        return self


class UsageEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    event_type: str
    total_quantity: int
    cost_cents: int
    idempotency_key: str
    timestamp: datetime


# --------------------------------------------------------------------------
# Quota
# --------------------------------------------------------------------------

class QuotaStatus(BaseModel):
    tenant_id: uuid.UUID
    plan_name: str
    api_calls_used: int
    api_call_limit: int
    tokens_used: int
    token_limit: int
    period_start: datetime
    period_end: datetime


# --------------------------------------------------------------------------
# Error payloads (structured, machine-parseable)
# --------------------------------------------------------------------------

class ErrorDetail(BaseModel):
    code: Literal[
        "QUOTA_EXCEEDED",
        "PAYMENT_REQUIRED",
        "VALIDATION_ERROR",
        "IDEMPOTENCY_CONFLICT",
        "NOT_FOUND",
        "WEBHOOK_SIGNATURE_INVALID",
    ]
    message: str
    limit: int | None = None
    used: int | None = None
    requested: int | None = None
    retry_after_seconds: int | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


# --------------------------------------------------------------------------
# Tenants / Plans (minimal, for seeding & inspection endpoints)
# --------------------------------------------------------------------------

class TenantCreate(BaseModel):
    name: str
    plan_type: Literal["free", "pro"] = "free"


class TenantResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    plan_type: str
    status: str
    stripe_customer_id: str | None = None
    created_at: datetime


class PlanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    api_call_limit: int
    token_limit: int
    price_cents: int
