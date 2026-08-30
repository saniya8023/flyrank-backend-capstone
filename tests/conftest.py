"""
Shared pytest fixtures.

The test suite runs against a file-backed SQLite database (via
aiosqlite) rather than Postgres. This is a deliberate trade-off
documented in EVIDENCE.md / README.md: the ORM models use a
dialect-agnostic UUID type and JSON/JSONB variant specifically so the
same model definitions work against both engines. A real file (not
`:memory:`) is used so each AsyncSession gets its own connection,
which matters for the concurrency test in test_idempotency.py — see
the `engine` fixture docstring below for why. `SELECT ... FOR UPDATE`
is accepted (though not truly row-locking) by SQLite, so the
concurrency test exercises the *application-level* race handling path
(the IntegrityError-catch-and-replay logic), which is the part of the
guarantee that is engine-independent. True lock-contention behavior
under Postgres is covered by the manual verification run described in
EVIDENCE.md, run against the docker-compose stack.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.session import Base, _enable_sqlite_immediate_transactions, get_db
from app.models.models import Plan, Subscription, SubscriptionStatus, Tenant


@pytest_asyncio.fixture
async def engine(tmp_path):
    """
    Fresh file-backed SQLite engine per test, stored under pytest's
    `tmp_path`. A real file (not `:memory:`) is required for the
    concurrency test in test_idempotency.py: SQLite `:memory:` databases
    only exist within a single DBAPI connection, so forcing all sessions
    onto one shared connection (the only way to make `:memory:` usable
    across sessions) means concurrent async tasks interleave their
    BEGIN/COMMIT/ROLLBACK on that ONE connection and corrupt each
    other's transaction state — the opposite of what the race test needs
    to exercise. A file-backed DB lets each AsyncSession open its own
    real connection, so SQLite's normal file-level write-serialization
    (not connection-sharing) is what arbitrates the race, which is a
    faithful-enough stand-in for Postgres row-locking for this test's
    purpose: proving the IntegrityError-catch-and-replay path, not
    benchmarking lock contention itself.
    """
    db_path = tmp_path / "test.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    _enable_sqlite_immediate_transactions(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture
async def db_session(session_factory):
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def seeded_plans(session_factory):
    """Creates 'free' and 'pro' plans matching the production seed data."""
    async with session_factory() as session:
        free = Plan(name="free", api_call_limit=5, token_limit=10_000, price_cents=0)
        pro = Plan(name="pro", api_call_limit=100_000, token_limit=10_000_000, price_cents=4_900)
        session.add_all([free, pro])
        await session.commit()
        await session.refresh(free)
        await session.refresh(pro)
        return {"free": free.id, "pro": pro.id}


@pytest_asyncio.fixture
async def tenant_free(session_factory, seeded_plans):
    """A tenant on the free plan with an ACTIVE subscription, id returned."""
    async with session_factory() as session:
        tenant = Tenant(name="Acme Free", plan_type="free")
        session.add(tenant)
        await session.flush()
        now = datetime.now(timezone.utc)
        sub = Subscription(
            tenant_id=tenant.id,
            plan_id=seeded_plans["free"],
            status=SubscriptionStatus.ACTIVE,
            current_period_start=now,
            current_period_end=now + timedelta(days=30),
        )
        session.add(sub)
        await session.commit()
        return tenant.id


@pytest_asyncio.fixture
async def tenant_pro(session_factory, seeded_plans):
    async with session_factory() as session:
        tenant = Tenant(name="Acme Pro", plan_type="pro")
        session.add(tenant)
        await session.flush()
        now = datetime.now(timezone.utc)
        sub = Subscription(
            tenant_id=tenant.id,
            plan_id=seeded_plans["pro"],
            status=SubscriptionStatus.ACTIVE,
            current_period_start=now,
            current_period_end=now + timedelta(days=30),
        )
        session.add(sub)
        await session.commit()
        return tenant.id


@pytest_asyncio.fixture
async def client(session_factory):
    """
    httpx.AsyncClient wired directly into the FastAPI app via ASGITransport,
    with `get_db` overridden to hand out sessions from our SQLite
    `session_factory` instead of the real Postgres engine.
    """
    import app.main as main_module

    async def override_get_db():
        async with session_factory() as session:
            yield session

    main_module.app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    main_module.app.dependency_overrides.clear()