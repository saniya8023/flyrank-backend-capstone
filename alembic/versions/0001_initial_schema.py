"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-08-29

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')

    plan_type_enum = postgresql.ENUM("free", "pro", name="plan_type_enum")
    tenant_status_enum = postgresql.ENUM("active", "suspended", "canceled", name="tenant_status_enum")
    subscription_status_enum = postgresql.ENUM(
        "active", "past_due", "canceled", "incomplete", name="subscription_status_enum"
    )
    event_type_enum = postgresql.ENUM("api_call", "ai_token", name="event_type_enum")

    bind = op.get_bind()
    plan_type_enum.create(bind, checkfirst=True)
    tenant_status_enum.create(bind, checkfirst=True)
    subscription_status_enum.create(bind, checkfirst=True)
    event_type_enum.create(bind, checkfirst=True)

    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("plan_type", plan_type_enum, nullable=False, server_default="free"),
        sa.Column("stripe_customer_id", sa.String(255), nullable=True),
        sa.Column("status", tenant_status_enum, nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_tenants_stripe_customer_id", "tenants", ["stripe_customer_id"], unique=True)

    op.create_table(
        "plans",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(50), nullable=False, unique=True),
        sa.Column("api_call_limit", sa.Integer(), nullable=False),
        sa.Column("token_limit", sa.Integer(), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("api_call_limit >= 0", name="ck_plans_api_call_limit_nonneg"),
        sa.CheckConstraint("token_limit >= 0", name="ck_plans_token_limit_nonneg"),
        sa.CheckConstraint("price_cents >= 0", name="ck_plans_price_nonneg"),
    )

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stripe_subscription_id", sa.String(255), nullable=True),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", subscription_status_enum, nullable=False, server_default="active"),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_subscriptions_tenant_id", "subscriptions", ["tenant_id"])
    op.create_index("ix_subscriptions_stripe_subscription_id", "subscriptions", ["stripe_subscription_id"], unique=True)
    op.create_index("ix_subscriptions_tenant_status", "subscriptions", ["tenant_id", "status"])

    op.create_table(
        "usage_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", event_type_enum, nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cached_input_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reasoning_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("total_quantity", sa.BigInteger(), nullable=False),
        sa.Column("cost_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("total_quantity >= 0", name="ck_usage_events_qty_nonneg"),
        sa.UniqueConstraint("idempotency_key", name="uq_usage_events_idempotency_key"),
    )
    op.create_index("ix_usage_events_tenant_id", "usage_events", ["tenant_id"])
    op.create_index("ix_usage_events_timestamp", "usage_events", ["timestamp"])
    op.create_index("ix_usage_events_tenant_timestamp", "usage_events", ["tenant_id", "timestamp"])

    op.create_table(
        "idempotency_records",
        sa.Column("idempotency_key", sa.String(255), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("response_body", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_idempotency_records_tenant_id", "idempotency_records", ["tenant_id"])

    op.create_table(
        "processed_webhooks",
        sa.Column("event_id", sa.String(255), primary_key=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.bulk_insert(
        sa.table(
            "plans",
            sa.column("name", sa.String),
            sa.column("api_call_limit", sa.Integer),
            sa.column("token_limit", sa.Integer),
            sa.column("price_cents", sa.Integer),
        ),
        [
            {"name": "free", "api_call_limit": 1_000, "token_limit": 100_000, "price_cents": 0},
            {"name": "pro", "api_call_limit": 100_000, "token_limit": 10_000_000, "price_cents": 4_900},
        ],
    )


def downgrade() -> None:
    op.drop_table("processed_webhooks")
    op.drop_table("idempotency_records")
    op.drop_table("usage_events")
    op.drop_table("subscriptions")
    op.drop_table("plans")
    op.drop_table("tenants")

    bind = op.get_bind()
    postgresql.ENUM(name="event_type_enum").drop(bind, checkfirst=True)
    postgresql.ENUM(name="subscription_status_enum").drop(bind, checkfirst=True)
    postgresql.ENUM(name="tenant_status_enum").drop(bind, checkfirst=True)
    postgresql.ENUM(name="plan_type_enum").drop(bind, checkfirst=True)
