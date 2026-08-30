# EVIDENCE.md

All transcripts below are real output captured by actually running
this codebase — nothing here is hand-written or hypothetical. See
`BUILDLOG.md` for the two bugs these runs caught and how they were
fixed.

---

## 1. Automated test suite (`pytest tests/ -v`)

```
============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-9.1.1, pluggy-1.6.0
plugins: anyio-4.14.2, asyncio-1.4.0
asyncio: mode=Mode.AUTO, debug=False
collecting ... collected 28 items

tests/test_idempotency.py::test_fresh_request_creates_usage_event PASSED [  3%]
tests/test_idempotency.py::test_sequential_retry_is_idempotent PASSED    [  7%]
tests/test_idempotency.py::test_concurrent_requests_same_key_create_exactly_one_event PASSED [ 10%]
tests/test_idempotency.py::test_different_keys_create_distinct_events PASSED [ 14%]
tests/test_idempotency.py::test_missing_idempotency_key_header_is_rejected PASSED [ 17%]
tests/test_pricing.py::test_formula_matches_spec_exactly PASSED          [ 21%]
tests/test_pricing.py::test_reasoning_tokens_billed_at_output_rate PASSED [ 25%]
tests/test_pricing.py::test_cached_input_is_cheaper_than_regular_input PASSED [ 28%]
tests/test_pricing.py::test_zero_usage_costs_zero PASSED                 [ 32%]
tests/test_pricing.py::test_cost_is_floored_never_rounded_up PASSED      [ 35%]
tests/test_pricing.py::test_cost_never_exceeds_sum_of_individually_floored_costs PASSED [ 39%]
tests/test_pricing.py::test_all_returned_values_are_int_never_float PASSED [ 42%]
tests/test_pricing.py::test_negative_token_counts_are_rejected PASSED    [ 46%]
tests/test_pricing.py::test_api_call_events_have_zero_marginal_cost PASSED [ 50%]
tests/test_quota.py::test_usage_under_limit_succeeds PASSED              [ 53%]
tests/test_quota.py::test_usage_exactly_at_limit_succeeds PASSED         [ 57%]
tests/test_quota.py::test_usage_one_over_limit_returns_429_with_retry_after PASSED [ 60%]
tests/test_quota.py::test_token_quota_independent_from_api_call_quota PASSED [ 64%]
tests/test_quota.py::test_token_quota_exceeded_returns_429 PASSED        [ 67%]
tests/test_quota.py::test_no_active_subscription_returns_402 PASSED      [ 71%]
tests/test_quota.py::test_suspended_tenant_returns_402_even_with_quota_headroom PASSED [ 75%]
tests/test_quota.py::test_unknown_tenant_returns_404 PASSED              [ 78%]
tests/test_quota.py::test_quota_status_endpoint_reflects_usage PASSED    [ 82%]
tests/test_webhooks.py::test_invalid_signature_returns_400 PASSED        [ 85%]
tests/test_webhooks.py::test_valid_signature_subscription_updated_syncs_status PASSED [ 89%]
tests/test_webhooks.py::test_duplicate_event_id_is_ignored_second_time PASSED [ 92%]
tests/test_webhooks.py::test_subscription_deleted_downgrades_tenant_to_free PASSED [ 96%]
tests/test_webhooks.py::test_unhandled_event_type_is_acknowledged_but_ignored PASSED [100%]

======================== 28 passed, 1 warning in 1.31s =========================
```

---

## 2. Idempotency — sequential retry (live HTTP run, not mocked)

Same `Idempotency-Key` sent twice, sequentially:

```
=== Idempotency: fresh write then exact-key retry ===
First request  -> 201 {'id': 'a347f660-3118-41a3-8c36-bd285f8616e7', 'tenant_id': '0069d505-ee81-4f89-9831-8d6956d82b8f', 'event_type': 'api_call', 'total_quantity': 1, 'cost_cents': 0, 'plan_name': 'free', 'idempotency_key': 'evidence-key-1'}
Retry (same key)-> 200 {'id': 'a347f660-3118-41a3-8c36-bd285f8616e7', 'tenant_id': '0069d505-ee81-4f89-9831-8d6956d82b8f', 'event_type': 'api_call', 'total_quantity': 1, 'cost_cents': 0, 'plan_name': 'free', 'idempotency_key': 'evidence-key-1'}
```

Identical `id` on both, no second `UsageEvent` row created; the retry
was answered by the `idempotency_records` fast path.

## 3. Idempotency — true concurrent race (8 requests, one key, `asyncio.gather`)

```
=== Idempotency: 8 concurrent requests, same key ===
  concurrent #0: 200 id=900d104c-059e-472b-8c41-85678a03820d
  concurrent #1: 200 id=900d104c-059e-472b-8c41-85678a03820d
  concurrent #2: 200 id=900d104c-059e-472b-8c41-85678a03820d
  concurrent #3: 200 id=900d104c-059e-472b-8c41-85678a03820d
  concurrent #4: 200 id=900d104c-059e-472b-8c41-85678a03820d
  concurrent #5: 200 id=900d104c-059e-472b-8c41-85678a03820d
  concurrent #6: 200 id=900d104c-059e-472b-8c41-85678a03820d
  concurrent #7: 201 id=900d104c-059e-472b-8c41-85678a03820d
```

All 8 concurrent requests resolve to the **same** `UsageEvent` id —
exactly one `201` winner, seven `200` replays — despite none of them
being able to see each other's write before racing. This is the
`UNIQUE` constraint + `IntegrityError`-catch-and-replay path, not just
the sequential fast-path cache. A database query immediately after
confirms exactly one row exists with this idempotency key (see
`tests/test_idempotency.py::test_concurrent_requests_same_key_create_exactly_one_event`,
which asserts this programmatically).

---

## 4. Quota enforcement — boundary + overflow (live HTTP run)

Plan limit: `api_call_limit = 5`. One call already recorded above,
then 3 more filled, then the boundary is tested:

```
=== Quota: filling remaining limit then overflow ===
  fill #0: 201
  fill #1: 201
  fill #2: 201
  overflow: 429 {'detail': {'code': 'QUOTA_EXCEEDED', 'message': 'Quota exceeded: used=5 requested=1 limit=5', 'limit': 5, 'used': 5, 'requested': 1, 'retry_after_seconds': 2591999}} Retry-After= 2591999
```

The 5th call (used=4 → requested 1 more = 5, **not** > 5) succeeds —
confirmed separately in
`tests/test_quota.py::test_usage_exactly_at_limit_succeeds`. The 6th
call is rejected with a structured `429` payload and a `Retry-After`
header carrying the exact seconds remaining until
`current_period_end`.

```
=== Quota status endpoint ===
  200 {'tenant_id': '0069d505-ee81-4f89-9831-8d6956d82b8f', 'plan_name': 'free', 'api_calls_used': 5, 'api_call_limit': 5, 'tokens_used': 0, 'token_limit': 10000, 'period_start': '2026-08-29T15:21:14.759643', 'period_end': '2026-09-28T15:21:14.759643'}
```

`GET /v1/tenants/{id}/quota` independently confirms the same rollup
the enforcement logic used (`api_calls_used: 5` matches the limit
exactly), proving the read path and the write-path enforcement agree.

`402` (payment-required) paths — no active subscription, suspended
tenant, lapsed billing period — are covered by
`tests/test_quota.py::test_no_active_subscription_returns_402` and
`::test_suspended_tenant_returns_402_even_with_quota_headroom`, both
passing (see Section 1).

---

## 5. AI token pricing — money math (live calculation + hand check)

Input: 1000 input tokens, 500 cached-input tokens, 200 output tokens,
100 reasoning tokens. Rates: 30 / 8 / 150 / 150 micro-cents per token
respectively (reasoning billed at the output rate).

```
Hand-computed:  1000*30 + 500*8 + 200*150 + 100*150 = 79,000 micro-cents
Floor to cents: 79,000 // 10,000 = 7 cents

Engine output (from Phase 1 verification):
Token usage result: {'id': '5e8ba31a-...', 'event_type': 'ai_token',
  'total_quantity': 1800, 'cost_cents': 7, 'plan_name': 'pro',
  'idempotency_key': 'token-key-1'}
Expected cost cents: 7 Got: 7
```

Engine output matches the hand calculation exactly. Full formula
correctness, the reasoning-billed-as-output equivalence, the
cached-cheaper-than-regular-input property, floor-vs-round-up
behavior, and the never-produces-a-float invariant are each asserted
individually and pass in `tests/test_pricing.py` (Section 1).

---

## 6. Stripe webhook handling — signature verification, dedup, plan sync

All requests below are signed with the **real** Stripe HMAC scheme
(`t={timestamp},v1={hmac_sha256}`) and verified by the actual
`stripe.Webhook.construct_event` call — not mocked out.

```
=== Invalid signature ===
  400 {'detail': {'code': 'WEBHOOK_SIGNATURE_INVALID', 'message': 'Webhook signature verification failed: No signatures found matching the expected signature for payload', ...}}

=== Valid signature: customer.subscription.updated (past_due) ===
  200 {'status': 'subscription_updated', 'subscription_id': 1}

=== Same event_id delivered again (Stripe at-least-once) ===
  200 {'status': 'duplicate_ignored', 'event_id': 'evt_evidence_1'}

=== customer.subscription.deleted -> tenant downgraded to free ===
  200 {'status': 'subscription_canceled', 'subscription_id': 1}
  tenant.plan_type after deletion: PlanType.FREE
  subscription.status after deletion: SubscriptionStatus.CANCELED
```

This demonstrates, in order:
1. A tampered/invalid signature is rejected with `400` and never
   reaches business logic.
2. A validly-signed `customer.subscription.updated` event correctly
   syncs the subscription's `status` to `PAST_DUE`.
3. Re-delivering the **identical** `event_id` is a genuine no-op
   (`duplicate_ignored`) — the second delivery does not re-apply the
   status change or touch `processed_at` again.
4. `customer.subscription.deleted` correctly cascades: the
   subscription is marked `CANCELED` **and** the tenant's `plan_type`
   is downgraded to `free`, read back from the database (not just
   inferred from the HTTP response) to confirm the write actually
   persisted.

---

## 7. Reconciling this evidence with the automated suite

Every behavior demonstrated in the live transcripts above (Sections
2–6) has a corresponding assertion in `tests/`, so this evidence is
not a one-off manual run that could regress silently — see the
`PASSED` list in Section 1 for the permanent, CI-runnable version of
each of these checks.

---

## 8. Phase 3 — Quota overselling under concurrency: bug, fix, proof

**Before the fix** (`app/db/session.py` without `BEGIN IMMEDIATE`):
5 concurrent requests, 5 different `Idempotency-Key`s, tenant on a
plan with `api_call_limit=1`:

```
request #0: 201 {'id': '4b3b8cf7-...', ...}
request #1: 201 {'id': 'af865a54-...', ...}
request #2: 201 {'id': 'b376f6dc-...', ...}
request #3: 201 {'id': '33aa0c67-...', ...}
request #4: 201 {'id': '601c6a22-...', ...}

Total successful (201) inserts: 5 (limit was 1)
```

All 5 succeeded against a limit of 1 — a genuine overselling bug.
`SELECT ... FOR UPDATE` in `QuotaEnforcer` is a no-op under SQLite's
default deferred-transaction mode, so nothing serialized the racing
reads (see `BUILDLOG.md` Phase 3 for the root-cause explanation).

**After the fix** (`_enable_sqlite_immediate_transactions` added to
`app/db/session.py`), the identical probe:

```
request #0: 201 {'id': '2f82794d-...', ...}
request #1: 429 {'detail': {'code': 'QUOTA_EXCEEDED', 'message': 'Quota exceeded: used=1 requested=1 limit=1', ...}}
request #2: 429 {'detail': {'code': 'QUOTA_EXCEEDED', ...}}
request #3: 429 {'detail': {'code': 'QUOTA_EXCEEDED', ...}}
request #4: 429 {'detail': {'code': 'QUOTA_EXCEEDED', ...}}

Total successful (201) inserts: 1 (limit was 1)
```

Exactly one success, four correctly-rejected `429`s. This exact
scenario is now a permanent regression test:
`tests/test_quota_concurrency.py::test_concurrent_different_keys_at_tight_limit_of_one`.

**Full suite after the fix** — 2 new tests added, all 28 prior tests
still green:

```
collected 30 items

tests/test_idempotency.py::test_fresh_request_creates_usage_event PASSED [  3%]
tests/test_idempotency.py::test_sequential_retry_is_idempotent PASSED    [  6%]
tests/test_idempotency.py::test_concurrent_requests_same_key_create_exactly_one_event PASSED [ 10%]
tests/test_idempotency.py::test_different_keys_create_distinct_events PASSED [ 13%]
tests/test_idempotency.py::test_missing_idempotency_key_header_is_rejected PASSED [ 16%]
tests/test_pricing.py::test_formula_matches_spec_exactly PASSED          [ 20%]
tests/test_pricing.py::test_reasoning_tokens_billed_at_output_rate PASSED [ 23%]
tests/test_pricing.py::test_cached_input_is_cheaper_than_regular_input PASSED [ 26%]
tests/test_pricing.py::test_zero_usage_costs_zero PASSED                 [ 30%]
tests/test_pricing.py::test_cost_is_floored_never_rounded_up PASSED      [ 33%]
tests/test_pricing.py::test_cost_never_exceeds_sum_of_individually_floored_costs PASSED [ 36%]
tests/test_pricing.py::test_all_returned_values_are_int_never_float PASSED [ 40%]
tests/test_pricing.py::test_negative_token_counts_are_rejected PASSED    [ 43%]
tests/test_pricing.py::test_api_call_events_have_zero_marginal_cost PASSED [ 46%]
tests/test_quota.py::test_usage_under_limit_succeeds PASSED              [ 50%]
tests/test_quota.py::test_usage_exactly_at_limit_succeeds PASSED         [ 53%]
tests/test_quota.py::test_usage_one_over_limit_returns_429_with_retry_after PASSED [ 56%]
tests/test_quota.py::test_token_quota_independent_from_api_call_quota PASSED [ 60%]
tests/test_quota.py::test_token_quota_exceeded_returns_429 PASSED        [ 63%]
tests/test_quota.py::test_no_active_subscription_returns_402 PASSED      [ 66%]
tests/test_quota.py::test_suspended_tenant_returns_402_even_with_quota_headroom PASSED [ 70%]
tests/test_quota.py::test_unknown_tenant_returns_404 PASSED              [ 73%]
tests/test_quota.py::test_quota_status_endpoint_reflects_usage PASSED    [ 76%]
tests/test_quota_concurrency.py::test_concurrent_different_keys_never_oversell_quota PASSED [ 80%]
tests/test_quota_concurrency.py::test_concurrent_different_keys_at_tight_limit_of_one PASSED [ 83%]
tests/test_webhooks.py::test_invalid_signature_returns_400 PASSED        [ 86%]
tests/test_webhooks.py::test_valid_signature_subscription_updated_syncs_status PASSED [ 90%]
tests/test_webhooks.py::test_duplicate_event_id_is_ignored_second_time PASSED [ 93%]
tests/test_webhooks.py::test_subscription_deleted_downgrades_tenant_to_free PASSED [ 96%]
tests/test_webhooks.py::test_unhandled_event_type_is_acknowledged_but_ignored PASSED [100%]

======================== 30 passed, 1 warning in 1.71s =========================
```

**Honest caveat, stated plainly:** this fix and its tests prove the
locking *strategy* is correct given a backend that honors it. This
sandbox has no Docker/Postgres available, so the identical race has
not been re-run against a live PostgreSQL instance — that remains the
one item in `README.md`'s "Known limitations" requiring manual
verification via `docker compose up` before this is trusted in
production. The correctness property (acquire the lock before the
read, hold it until commit) is the same in both engines; only the
lock granularity differs (whole-database on SQLite via `BEGIN
IMMEDIATE`, per-row on Postgres via `SELECT ... FOR UPDATE`).