"""
SQLAlchemy 2.0 (async) ORM models.

Design notes
------------
- All primary keys that are externally referenced (tenants, usage_events)
  use UUID (server_default=gen_random_uuid()) so IDs are safe to expose
  and are generated without a round trip when using server defaults.
- `idempotency_key` on usage_events is UNIQUE + indexed — this is the
  DB-level guarantee behind the exactly-once metering engine. The
  application-level idempotency_records table stores the *response*
  so retries can be answered without recomputation, while the unique
  constraint on usage_events is the last line of defense against a
  race between two concurrent requests bearing the same key.
- All monetary values are `Integer` cents. Never `Float`/`Numeric` with
  implied fractional cents — see PricingCalculator for the rounding rule.
- `subscriptions.tenant_id` and `usage_events.tenant_id` are indexed
  because the hot path query is "sum usage for tenant X in period Y".
"""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import CHAR, TypeDecorator

from app.db.session import Base

# JSONB on Postgres (production), plain JSON elsewhere (SQLite test DB).
JSONType = JSON().with_variant(JSONB, "postgresql")


class UUID(TypeDecorator):
    """
    Platform-independent UUID type.

    Uses PostgreSQL's native UUID type in production. Falls back to a
    CHAR(36) string representation for SQLite, which is what the async
    test suite uses (aiosqlite) — this keeps the same model definitions
    usable for both `docker-compose` Postgres and fast in-memory tests.
    """
    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if dialect.name == "postgresql":
            return str(value)
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(value)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PlanType(str, enum.Enum):
    FREE = "free"
    PRO = "pro"


class TenantStatus(str, enum.Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CANCELED = "canceled"


class SubscriptionStatus(str, enum.Enum):
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    INCOMPLETE = "incomplete"


class EventType(str, enum.Enum):
    API_CALL = "api_call"
    AI_TOKEN = "ai_token"


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    plan_type: Mapped[PlanType] = mapped_column(
        Enum(PlanType, name="plan_type_enum"), nullable=False, default=PlanType.FREE
    )
    stripe_customer_id: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True, index=True
    )
    status: Mapped[TenantStatus] = mapped_column(
        Enum(TenantStatus, name="tenant_status_enum"),
        nullable=False,
        default=TenantStatus.ACTIVE,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    subscriptions: Mapped[list["Subscription"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
    usage_events: Mapped[list["UsageEvent"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Tenant id={self.id} name={self.name!r} plan={self.plan_type}>"


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    api_call_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    token_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        CheckConstraint("api_call_limit >= 0", name="ck_plans_api_call_limit_nonneg"),
        CheckConstraint("token_limit >= 0", name="ck_plans_token_limit_nonneg"),
        CheckConstraint("price_cents >= 0", name="ck_plans_price_nonneg"),
    )

    def __repr__(self) -> str:
        return f"<Plan {self.name} calls={self.api_call_limit} tokens={self.token_limit}>"


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stripe_subscription_id: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True, index=True
    )
    plan_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus, name="subscription_status_enum"),
        nullable=False,
        default=SubscriptionStatus.ACTIVE,
    )
    current_period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    current_period_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="subscriptions")
    plan: Mapped["Plan"] = relationship()

    __table_args__ = (
        Index("ix_subscriptions_tenant_status", "tenant_id", "status"),
    )

    def __repr__(self) -> str:
        return f"<Subscription id={self.id} tenant={self.tenant_id} status={self.status}>"


class UsageEvent(Base):
    __tablename__ = "usage_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[EventType] = mapped_column(
        Enum(EventType, name="event_type_enum"), nullable=False
    )
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # total_quantity is the metering unit consumed against the plan's
    # relevant limit: for API_CALL events this is always 1 (one call);
    # for AI_TOKEN events this is input+cached+output+reasoning tokens.
    total_quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Cost computed at write-time by PricingCalculator, stored as integer
    # cents so historical invoices are stable even if rates change later.
    cost_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="usage_events")

    __table_args__ = (
        # Enforces exactly-once semantics at the database level: two
        # concurrent inserts with the same key will have one succeed
        # and one raise IntegrityError, which the service layer catches
        # and resolves by reading back the winning row / stored response.
        UniqueConstraint("idempotency_key", name="uq_usage_events_idempotency_key"),
        Index("ix_usage_events_tenant_timestamp", "tenant_id", "timestamp"),
        CheckConstraint("total_quantity >= 0", name="ck_usage_events_qty_nonneg"),
    )

    def __repr__(self) -> str:
        return f"<UsageEvent id={self.id} tenant={self.tenant_id} qty={self.total_quantity}>"


class IdempotencyRecord(Base):
    """
    Stores the exact HTTP response previously returned for a given
    Idempotency-Key, so a retried request (same key) gets back the
    identical status_code + body without re-running any business logic
    or creating a second usage event.
    """
    __tablename__ = "idempotency_records"

    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    def __repr__(self) -> str:
        return f"<IdempotencyRecord key={self.idempotency_key} status={self.status_code}>"


class ProcessedWebhook(Base):
    """
    Deduplication ledger for Stripe webhook events. Stripe may deliver
    the same event more than once (at-least-once delivery); event_id
    (Stripe's `evt_...` id) as PK makes re-processing a no-op.
    """
    __tablename__ = "processed_webhooks"

    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    def __repr__(self) -> str:
        return f"<ProcessedWebhook id={self.event_id} type={self.event_type}>"
