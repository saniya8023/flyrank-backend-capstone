"""
MeteringEngine — the exactly-once usage recording pipeline.

Flow for a single incoming usage-record request:

  1. Look up `idempotency_records` for this key.
     - HIT  -> return the stored (status_code, response_body) verbatim.
               No quota check, no new UsageEvent, no cost recomputation.
     - MISS -> continue.
  2. Within a single DB transaction:
       a. QuotaEnforcer.check_and_reserve() — takes the FOR UPDATE lock
          on the tenant's subscription and validates headroom.
       b. Compute cost via PricingCalculator.
       c. INSERT the UsageEvent (idempotency_key is UNIQUE).
       d. INSERT the IdempotencyRecord with the response we're about to
          return.
       e. COMMIT.
  3. If step 2c's INSERT hits a UNIQUE VIOLATION on idempotency_key
     (a concurrent duplicate request that raced us and inserted first),
     we roll back our own attempt and re-read the *other* request's
     stored response from `idempotency_records`, returning that instead
     of double-charging or erroring. This is the exactly-once guarantee
     surviving a true concurrent race, not just sequential retries.

Why two idempotency layers (UNIQUE constraint AND idempotency_records)?
The UNIQUE constraint on usage_events.idempotency_key is the ultimate
correctness guarantee (DB enforces it atomically even under a race the
application layer could otherwise lose). idempotency_records is the
*fast path* + response cache: 99% of retries hit it directly at step 1
without touching usage_events or re-running quota/pricing logic at all.
"""
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.models import EventType, IdempotencyRecord, Tenant, UsageEvent
from app.services.pricing import PricingCalculator, TokenUsage
from app.services.quota import PaymentRequiredError, QuotaEnforcer, QuotaExceededError


@dataclass(frozen=True, slots=True)
class MeteringResult:
    status_code: int
    body: dict
    replayed: bool  # True if this was an idempotent replay, not a fresh write


class MeteringEngine:
    def __init__(self, db: AsyncSession, settings: Settings) -> None:
        self._db = db
        self._quota = QuotaEnforcer(db)
        self._pricing = PricingCalculator(settings)

    async def _get_stored_response(self, idempotency_key: str) -> IdempotencyRecord | None:
        return await self._db.get(IdempotencyRecord, idempotency_key)

    async def record_usage(
        self,
        tenant: Tenant,
        idempotency_key: str,
        event_type: EventType,
        token_usage: TokenUsage | None = None,
    ) -> MeteringResult:
        """
        Main entry point. `token_usage` is required for AI_TOKEN events
        and ignored (must be None/zeroed) for API_CALL events.
        """
        # --- Step 1: fast-path idempotent replay ---
        existing = await self._get_stored_response(idempotency_key)
        if existing is not None:
            return MeteringResult(
                status_code=existing.status_code,
                body=existing.response_body,
                replayed=True,
            )

        usage = token_usage or TokenUsage()
        requested_quantity = 1 if event_type == EventType.API_CALL else usage.total_tokens

        try:
            # --- Step 2: quota check under row lock, then write ---
            check = await self._quota.check_and_reserve(
                tenant=tenant, event_type=event_type, requested_quantity=requested_quantity
            )

            cost_cents = (
                self._pricing.calculate_api_call_cost_cents()
                if event_type == EventType.API_CALL
                else self._pricing.calculate_cost_cents(usage)
            )

            usage_event = UsageEvent(
                tenant_id=tenant.id,
                event_type=event_type,
                input_tokens=usage.input_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                output_tokens=usage.output_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                total_quantity=requested_quantity,
                cost_cents=cost_cents,
                idempotency_key=idempotency_key,
            )
            self._db.add(usage_event)
            await self._db.flush()  # surfaces IntegrityError before we build the response

            response_body = {
                "id": str(usage_event.id),
                "tenant_id": str(tenant.id),
                "event_type": event_type.value,
                "total_quantity": requested_quantity,
                "cost_cents": cost_cents,
                "plan_name": check.plan.name,
                "idempotency_key": idempotency_key,
            }
            status_code = 201

            self._db.add(
                IdempotencyRecord(
                    idempotency_key=idempotency_key,
                    tenant_id=tenant.id,
                    status_code=status_code,
                    response_body=response_body,
                )
            )
            await self._db.commit()
            return MeteringResult(status_code=status_code, body=response_body, replayed=False)

        except IntegrityError:
            # A concurrent request with the same idempotency_key won the
            # race and committed first. Roll back our half-done attempt
            # and defer to whichever response actually got persisted.
            await self._db.rollback()
            winner = await self._get_stored_response(idempotency_key)
            if winner is not None:
                return MeteringResult(
                    status_code=winner.status_code,
                    body=winner.response_body,
                    replayed=True,
                )
            # Extremely unlikely: the other transaction's idempotency_records
            # insert hasn't landed yet. Surface a 409 so the client retries.
            return MeteringResult(
                status_code=409,
                body={
                    "error": {
                        "code": "IDEMPOTENCY_CONFLICT",
                        "message": "A concurrent request with this Idempotency-Key is "
                        "still being processed. Please retry.",
                    }
                },
                replayed=False,
            )
        except (QuotaExceededError, PaymentRequiredError):
            await self._db.rollback()
            raise
