# Usage Metering & Billing Engine

A production-shaped backend service that answers three questions for a
SaaS platform with 100% precision:

1. **How much has this customer used?** → Metering (`usage_events`, exactly-once)
2. **Have they reached their plan limits?** → Quota enforcement (`429` / `402`)
3. **How much should they pay?** → Integer-cents pricing + Stripe Test Mode

---

## Architecture

```
                              ┌─────────────────────────────┐
                              │        FastAPI App          │
                              │   (Router / Controller)     │
                              │  ─────────────────────────  │
                              │  routers/usage.py            │
                              │  routers/quota.py            │
                              │  routers/tenants.py          │
                              │  routers/webhooks.py         │
                              │  • Pydantic DTO validation    │
                              │  • Clean status codes         │
                              │    (never leaks 500 on        │
                              │     validation/quota fails)   │
                              └──────────────┬───────────────┘
                                             │
                              ┌──────────────▼───────────────┐
                              │        Service Layer          │
                              │  ─────────────────────────    │
                              │  MeteringEngine                │
                              │   ├─ idempotency fast-path      │
                              │   ├─ calls QuotaEnforcer         │
                              │   ├─ calls PricingCalculator      │
                              │   └─ IntegrityError race-replay    │
                              │                                  │
                              │  QuotaEnforcer                    │
                              │   └─ SELECT ... FOR UPDATE lock     │
                              │       on tenant's Subscription       │
                              │                                       │
                              │  PricingCalculator                     │
                              │   └─ integer micro-cents money math     │
                              │                                          │
                              │  StripeWebhookHandler                    │
                              │   ├─ signature verified in router          │
                              │   └─ processed_webhooks dedup ledger        │
                              └──────────────┬───────────────────────────┘
                                             │
                              ┌──────────────▼───────────────┐
                              │      Repository / Data Layer  │
                              │  ─────────────────────────    │
                              │  SQLAlchemy 2.0 (AsyncIO)       │
                              │  models/models.py                │
                              │  • explicit FKs, indexes,          │
                              │    CHECK/UNIQUE constraints          │
                              │  • row-level locking where needed      │
                              └──────────────┬───────────────────────┘
                                             │
                                  ┌──────────▼──────────┐
                                  │   PostgreSQL 16       │
                                  │  (Docker container)    │
                                  └────────────────────────┘

                     ┌─────────────────────────────────────┐
                     │      Stripe Test Mode (external)      │
                     │  checkout.session.completed             │
                     │  customer.subscription.updated            │
                     │  customer.subscription.deleted              │
                     └──────────────┬──────────────────────────┘
                                    │  webhook POST (HMAC-signed)
                                    ▼
                     POST /v1/webhooks/stripe  (routers/webhooks.py)
```

### Tables

| Table                 | Purpose                                                        |
|-----------------------|-----------------------------------------------------------------|
| `tenants`              | Customer accounts; plan type, status, Stripe customer link       |
| `plans`                | Named plans (`free`, `pro`) with numeric limits + flat price       |
| `subscriptions`         | A tenant's current billing period + plan + Stripe subscription link |
| `usage_events`          | One row per billable event; `idempotency_key` is UNIQUE            |
| `idempotency_records`    | Cached HTTP response per `Idempotency-Key`, for fast-path replay      |
| `processed_webhooks`     | Stripe `event_id` dedup ledger (at-least-once delivery)                |

---

## Running the stack

### Option A — Docker Compose (recommended)

```bash
cp .env.example .env
# edit .env with real Stripe test keys if you want to exercise webhooks
# against `stripe listen`; the placeholder values are fine for the
# metering/quota/pricing endpoints, which don't call Stripe.

docker compose up -d --build
curl http://localhost:8000/health
```

This brings up Postgres, runs Alembic migrations (`migrate` service,
which also seeds the `free`/`pro` plans), then starts the API on
`:8000`.

### Option B — Local Python (for running the test suite)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest tests/ -v
```

The test suite runs entirely against an in-memory-equivalent,
file-backed SQLite database (see `tests/conftest.py`) — no Postgres or
Docker required to validate the business logic. The ORM models use a
dialect-agnostic `UUID` type and JSON/JSONB column variant specifically
so the same model definitions are exercised by both engines.

### Try it manually

```bash
# 1. Create a tenant on the free plan
TENANT_ID=$(curl -s -X POST localhost:8000/v1/tenants \
  -H "Content-Type: application/json" \
  -d '{"name": "Acme Inc", "plan_type": "free"}' | jq -r .id)

# 2. Record a usage event (Idempotency-Key required)
curl -s -X POST localhost:8000/v1/usage \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d "{\"tenant_id\": \"$TENANT_ID\", \"event_type\": \"api_call\"}"

# 3. Check quota status
curl -s localhost:8000/v1/tenants/$TENANT_ID/quota
```

---

## Business rules implemented

- **Idempotency**: `Idempotency-Key` header is required on every
  `POST /v1/usage` call. A fast-path cache (`idempotency_records`)
  answers retries without recomputation; a DB-level `UNIQUE` constraint
  on `usage_events.idempotency_key` is the last line of defense against
  a true concurrent race between two requests bearing the same key.
- **Quota enforcement**: `SELECT ... FOR UPDATE` locks the tenant's
  active `Subscription` row before computing the period rollup, closing
  the check-then-act race under concurrent load. Returns `429` with a
  structured payload + `Retry-After` when the numeric limit would be
  exceeded, `402` when the account/subscription itself isn't in a
  usable state (no active subscription, suspended tenant, lapsed
  period).
- **Pricing**: all rates are pinned as integer micro-cents/token in
  config; cost accumulates as an integer and is floor-divided to whole
  cents only at the end, so the platform — never the tenant — absorbs
  any sub-cent remainder.
- **Stripe webhooks**: signature verified against the *raw* request
  body (never a re-serialized model) via `stripe.Webhook.construct_event`;
  `processed_webhooks.event_id` (primary key) makes re-delivered events
  a no-op rather than a re-applied plan change.

---

## Known limitations

- **Plan ↔ Stripe Price mapping** is by convention (`checkout.session.completed`
  always resolves to the `pro` plan) rather than a stored `price_id` column
  on `Plan`. A real system would add that column; this was simplified for
  capstone scope.
- **Test-suite concurrency** is exercised against a file-backed SQLite
  database, not Postgres. This validates the *application-level* race
  handling (the `IntegrityError`-catch-and-replay path) faithfully, but
  true row-lock contention under `SELECT ... FOR UPDATE` can only be
  observed against real Postgres — see `EVIDENCE.md` for a manual
  verification run against the Docker Compose stack.
- **No authentication/authorization layer** — every endpoint is
  unauthenticated. A real deployment would add API-key or JWT auth
  scoped per tenant before this went anywhere near production traffic.
- **Invoice generation** (rolling up `usage_events.cost_cents` into a
  billing-period invoice) is out of scope for this capstone; the schema
  supports it (`cost_cents` is persisted per event) but no endpoint
  produces an invoice document.
- **Webhook retry/backoff** on the Stripe side is Stripe's own
  responsibility; this service only guarantees idempotent *handling* of
  whatever Stripe (re)delivers.