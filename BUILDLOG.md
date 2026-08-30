# BUILDLOG.md — AI Transparency Log

This log tracks the prompts, decisions, bugs found, and fixes made
while building this capstone with Claude. Kept honest and specific,
not a marketing summary.

---

## Phase 1 — Core codebase (models, services, routers)

**Prompt (paraphrased):** Act as a Principal Backend Engineer & System
Architect; build a production-ready Usage Metering & Billing Engine
capstone with the tech stack, schema, and business rules specified
(idempotency, quota 429/402, integer-cents pricing, Stripe webhook
sync).

**What was built:**
- `app/models/models.py` — SQLAlchemy 2.0 async models for all 6
  tables, with explicit indexes, FKs, CHECK/UNIQUE constraints.
- `app/services/pricing.py` — `PricingCalculator` / `TokenUsage`,
  integer-micro-cents math, floor-division rounding.
- `app/services/quota.py` — `QuotaEnforcer`, `SELECT ... FOR UPDATE`
  row-locking, `QuotaExceededError` (429) / `PaymentRequiredError` (402).
- `app/services/metering.py` — `MeteringEngine`, the two-layer
  idempotency guarantee (fast-path cache + UNIQUE-constraint backstop).
- `app/services/stripe_webhooks.py` — `StripeWebhookHandler`, event
  dedup via `processed_webhooks`, plan sync on the three named events.
- `app/routers/*.py`, `app/main.py` — HTTP layer, structured error
  envelopes, global exception handlers so no bare 500s leak.
- Alembic migration `0001_initial_schema.py`, `Dockerfile`,
  `docker-compose.yml`, `.env.example`.

**Design decisions made during Phase 1:**
- Chose a dialect-agnostic `UUID` TypeDecorator (native `UUID` on
  Postgres, `CHAR(36)` on SQLite) and a `JSON().with_variant(JSONB,
  "postgresql")` column type, specifically so the exact same model
  definitions could later be exercised by a fast SQLite test suite
  without any Postgres-specific mocking. This was a forward-looking
  decision made before Phase 2's tests existed, and it paid off.
- Reasoning tokens are billed at the `RATE_OUTPUT_MICRO_CENTS_PER_TOKEN`
  rate per the spec; kept `RATE_REASONING_MICRO_CENTS_PER_TOKEN` as a
  separate config constant (currently mirroring the output rate) rather
  than hardcoding the reuse, so pricing can diverge later without a
  code change — just a config change.
- `MeteringEngine.record_usage` computes cost and checks quota under
  the SAME transaction/lock scope as the `UsageEvent` insert (rather
  than as separate round-trips), and flushes before building the
  response body so a `UNIQUE` violation on `idempotency_key` surfaces
  as a catchable `IntegrityError` before any response is constructed.

**Verification performed before declaring Phase 1 done:**
- Built the SQLAlchemy models against an in-memory SQLite engine and
  confirmed `Base.metadata.create_all` succeeds on all 6 tables.
- Ran a manual async script exercising `MeteringEngine.record_usage`
  directly (no HTTP layer) for: a fresh API-call event, an idempotent
  replay of the same key, filling a 5-call quota and confirming the
  6th raises `QuotaExceededError`, and an AI-token cost calculation
  cross-checked by hand (1000 input + 500 cached + 200 output + 100
  reasoning tokens → 7 cents, matched exactly).
- Ran the same flow through the actual FastAPI app via `TestClient`
  and confirmed the HTTP-level 201 → 200 (replay) → 429 sequence.

No bugs were found in Phase 1 that survived to the delivered files —
the timezone-comparison bug described below was caught and fixed
*during* Phase 1 verification, before the code was ever packaged.

**Bug found & fixed during Phase 1 verification:**
- `QuotaEnforcer.check_and_reserve` compared
  `datetime.now(timezone.utc)` against
  `subscription.current_period_end` directly. Postgres round-trips
  `DateTime(timezone=True)` values as timezone-aware, but SQLite (used
  in the verification script) returns them naive, causing
  `TypeError: can't compare offset-naive and offset-aware datetimes`.
  Fixed by adding `_as_aware_utc()`, which tags a naive value as UTC
  (all datetimes in this system are written as UTC) rather than
  leaving the comparison to assume the driver's tz-awareness.

---

## Phase 2 — Tests, capstone.yaml, README, BUILDLOG, EVIDENCE

**Prompt (paraphrased):** Continue with Phase 2 — build the
`tests/` suite (idempotency concurrency, quota boundaries, webhook
verification), plus `capstone.yaml`, `README.md`, `BUILDLOG.md`, and
`EVIDENCE.md`. Deliver only the specific files added/modified in this
phase, with exact file paths — no restated folder structure.

**What was built:**
- `tests/conftest.py` — fixtures: file-backed SQLite engine/session
  factory, seeded plans/tenants, an `httpx.AsyncClient` wired directly
  into the FastAPI app via `ASGITransport` with `get_db` overridden.
- `tests/test_idempotency.py` — fresh write, sequential retry, **true
  concurrent race** via `asyncio.gather` on the same
  `Idempotency-Key`, distinct-keys-create-distinct-events, missing
  header validation.
- `tests/test_quota.py` — under-limit, exactly-at-limit (boundary),
  one-over-limit (429 + `Retry-After`), independent api_call/token
  quotas, no-subscription (402), suspended-tenant (402), unknown
  tenant (404), quota-status endpoint correctness.
- `tests/test_pricing.py` — formula correctness, reasoning-billed-as-
  output equivalence, cached-cheaper-than-regular, zero-usage,
  floor-rounding, floor-never-overcharges-vs-aggregate invariant,
  int-never-float, negative-input rejection.
- `tests/test_webhooks.py` — valid signature accepted, invalid
  signature → 400, duplicate `event_id` → no-op second time,
  `subscription.deleted` downgrades tenant to free, unhandled event
  type acknowledged-but-ignored. Signatures are constructed with the
  *real* Stripe HMAC scheme (`t={ts},v1={hmac}`) so
  `stripe.Webhook.construct_event` is exercised for real, not mocked.
- `pytest.ini` — `asyncio_mode = auto`.

**Bugs found and fixed while writing and running these tests (this is
the part of the log that matters most for transparency):**

1. **SQLite `:memory:` + `StaticPool` corrupts concurrent transactions.**
   The first draft of `tests/conftest.py` used
   `create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)`
   on the theory that `:memory:` requires a single shared connection to
   be visible across sessions. Under `asyncio.gather`-driven concurrent
   requests, this shares literally one DBAPI connection across all
   "concurrent" sessions, so their BEGIN/COMMIT/ROLLBACK sequences
   interleaved on that single connection and silently discarded the
   winning write — `test_concurrent_requests_same_key_create_exactly_one_event`
   failed with 0 rows in the table even though the HTTP responses
   showed a 201 winner. **Fix:** switched to a file-backed SQLite
   database under pytest's `tmp_path` fixture, so each `AsyncSession`
   opens its own real connection and SQLite's normal file-level write
   serialization (not artificial connection-sharing) arbitrates the
   race. Documented in `conftest.py` and `README.md` as a known
   fidelity gap vs. real Postgres row-locking.

2. **`stripe.StripeObject.get()` raises `AttributeError`, not a dict
   lookup.** `StripeWebhookHandler`'s handlers originally called
   `obj.get("status", "")` on `event["data"]["object"]`, copying dict
   idiom. The installed `stripe` SDK version's `StripeObject` supports
   `__getitem__` but explicitly raises on `.get`/other dict-method
   names to force `.to_dict()` conversion first. This surfaced as an
   unhandled 500 in `test_valid_signature_subscription_updated_syncs_status`
   and `test_duplicate_event_id_is_ignored_second_time`. **Fix:** each
   handler now converts `obj = obj.to_dict() if hasattr(obj, "to_dict")
   else dict(obj)` immediately after unwrapping `event["data"]["object"]`,
   before any `.get()` calls.

**Verification performed:**
- Full suite run: `pytest tests/ -v` → **28 passed, 0 failed** (see
  `EVIDENCE.md` for the full transcript).
- Re-ran the concurrency test in isolation multiple times to confirm
  the fix wasn't a one-off pass — consistently exactly one
  `UsageEvent` row survives 8 concurrent requests on the same key.

---

## Phase 3 — Quota concurrency correctness (a real bug this time)

**Prompt (paraphrased):** Move to Phase 3 if there is any remaining
work.

**Why this phase exists:** Phase 2's concurrency test
(`test_concurrent_requests_same_key_create_exactly_one_event`) races
N requests sharing the SAME `Idempotency-Key`. That's the right test
for the idempotency guarantee, but it does NOT exercise
`QuotaEnforcer`'s `SELECT ... FOR UPDATE` lock at all — every request
after the first is short-circuited by the idempotency fast path or the
`UNIQUE` constraint before it ever reaches the quota-check code path.
No test in the suite had actually raced multiple *independent*
(differently-keyed) requests against a tight quota limit. This phase
closes that gap.

**Bug found — quota overselling under concurrency:**

A manual probe script fired 5 concurrent requests, each with a
*different* `Idempotency-Key`, against a tenant on a plan with
`api_call_limit=1`. All 5 returned `201`:

```
request #0: 201 ...
request #1: 201 ...
request #2: 201 ...
request #3: 201 ...
request #4: 201 ...
Total successful (201) inserts: 5 (limit was 1)
```

**Root cause:** `SELECT ... FOR UPDATE` is a genuine row lock on
PostgreSQL, but SQLite silently accepts and ignores the `FOR UPDATE`
clause — it has no per-row locking. Worse, pysqlite/aiosqlite open
transactions in "deferred" mode by default, meaning no lock of any
kind is acquired until the first *write* statement. So all 5 racing
transactions could run their `SELECT`-based rollup (reading `used=0`)
before any of them reached the `INSERT`, each concluding there was
quota headroom, and all 5 committed.

**Fix — `app/db/session.py`:** Added
`_enable_sqlite_immediate_transactions()`, which — strictly for the
`sqlite` dialect, a no-op on PostgreSQL — disables pysqlite's implicit
transaction handling and issues `BEGIN IMMEDIATE` explicitly at the
start of every transaction. `BEGIN IMMEDIATE` grabs SQLite's RESERVED
lock immediately (before any `SELECT`), so a second concurrent
transaction attempting the same is blocked until the first commits or
rolls back. This closes the race at whole-database granularity on
SQLite (coarser than Postgres's per-row `FOR UPDATE`, but sufficient
to make the test suite meaningfully prove the guarantee without
requiring a live Postgres instance for every CI run).

**Verification after the fix:** the identical 5-concurrent-request
probe against `api_call_limit=1` now returns exactly one `201` and
four `429`s. Two permanent regression tests were added in
`tests/test_quota_concurrency.py`:
- 8 concurrent requests, 8 different keys, `api_call_limit=5` → exactly
  5 succeed, 3 rejected with `429 QUOTA_EXCEEDED`, and a direct DB
  count confirms exactly 5 `UsageEvent` rows exist.
- 5 concurrent requests, 5 different keys, `api_call_limit=1` (the
  exact scenario that failed 5-for-5 before the fix) → exactly 1
  succeeds.

Full suite after this phase: **30 passed, 0 failed** (2 new tests
added; all Phase 1/2 tests still green — the SQLite transaction-mode
change did not regress the idempotency-replay or webhook-dedup tests,
which rely on the same session machinery).

**What this does and doesn't prove:** This fix and its tests prove
the check-then-act sequence is race-safe *as implemented* — the
locking strategy correctly prevents overselling once given a backend
that honors it. It does not by itself prove PostgreSQL's `FOR UPDATE`
will behave identically in production; that remains the one
manual-verification item called out in `README.md`'s "Known
limitations" section, since this sandbox has no Docker/Postgres
available to confirm directly. The mechanism (row-level lock before
read, held until commit) is the same correctness property in both
engines, just enforced at different granularity.