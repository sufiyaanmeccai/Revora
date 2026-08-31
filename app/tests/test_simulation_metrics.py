"""
app/tests/test_simulation_metrics.py
--------------------------------------
Async pytest tests for the simulation batch runner and metrics API.

Test matrix:
  Simulation endpoint:
    1. POST /api/v1/simulation/run?count=10 returns 200 with 10 event IDs.
    2. Exactly 10 PaymentEvent rows are inserted into the database.
    3. All inserted events have status=AT_RISK.
    4. Count parameter is respected (count=5 → 5 events).

  Metrics endpoint (with pre-seeded data):
    5. GET /api/v1/metrics returns 200 with valid RecoverySummaryStats schema.
    6. total_at_risk reflects the correct count of AT_RISK events.
    7. total_in_recovery reflects IN_RECOVERY events.
    8. total_amount_at_risk sums the correct AT_RISK amounts.
    9. recovery_rate_pct is 0 when no events are RECOVERED yet.
   10. action_breakdowns is a list (may be empty when no workflows exist).

Strategy:
  • Override get_db with isolated in-memory SQLite for every test.
  • Mock process_payment_event to a no-op AsyncMock — the decision engine's
    correctness is already verified in test_decision_engine.py.
  • Seed the DB directly for metrics tests so values are deterministic.
"""

import uuid
from typing import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.database import get_db
from app.main import app
from app.models.orm import Base, PaymentEvent, PaymentStatus

# ---------------------------------------------------------------------------
# Isolated in-memory test DB (one engine per test via factory)
# ---------------------------------------------------------------------------

def _make_engine_and_factory() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
    )
    factory = async_sessionmaker(
        bind=eng,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )
    return eng, factory


async def _create_tables(eng: AsyncEngine) -> None:
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def db_resources() -> AsyncGenerator[
    tuple[AsyncEngine, async_sessionmaker[AsyncSession], AsyncSession], None
]:
    """Yield (engine, factory, session) for a fresh in-memory DB."""
    eng, factory = _make_engine_and_factory()
    await _create_tables(eng)
    async with factory() as session:
        yield eng, factory, session


@pytest.fixture
def override_db(db_resources):
    """Override FastAPI get_db dependency with the in-memory session."""
    _, _, session = db_resources

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override
    yield session
    app.dependency_overrides.pop(get_db, None)


SIMULATION_URL = "/api/v1/simulation/run"
METRICS_URL    = "/api/v1/metrics"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_event(
    status: PaymentStatus = PaymentStatus.AT_RISK,
    amount: float = 999.0,
) -> PaymentEvent:
    return PaymentEvent(
        id=str(uuid.uuid4()),
        customer_id=f"cust_{uuid.uuid4().hex[:6]}",
        customer_name="Test User",
        customer_email="test@revora.ai",
        customer_contact="+910000000000",
        amount=amount,
        currency="INR",
        error_code="BAD_REQUEST_ERROR",
        error_reason="insufficient_funds",
        status=status,
    )


# ---------------------------------------------------------------------------
# Simulation endpoint tests
# ---------------------------------------------------------------------------

async def test_simulation_run_returns_200_with_event_ids(
    override_db: AsyncSession,
) -> None:
    """POST /simulation/run?count=10 must return 200 and exactly 10 event IDs."""
    with patch(
        "app.api.v1.endpoints.simulation.process_payment_event",
        new_callable=AsyncMock,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(f"{SIMULATION_URL}?count=10")

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "batch_started"
    assert data["count"]  == 10
    assert len(data["event_ids"]) == 10


async def test_simulation_inserts_correct_number_of_events(
    override_db: AsyncSession,
) -> None:
    """Exactly count events must be persisted in the database."""
    with patch(
        "app.api.v1.endpoints.simulation.process_payment_event",
        new_callable=AsyncMock,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post(f"{SIMULATION_URL}?count=10")

    result = await override_db.execute(select(PaymentEvent))
    events = result.scalars().all()
    assert len(events) == 10


async def test_simulation_events_are_at_risk(
    override_db: AsyncSession,
) -> None:
    """All generated events must start with status AT_RISK."""
    with patch(
        "app.api.v1.endpoints.simulation.process_payment_event",
        new_callable=AsyncMock,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post(f"{SIMULATION_URL}?count=5")

    result = await override_db.execute(select(PaymentEvent))
    events = result.scalars().all()
    assert all(ev.status == PaymentStatus.AT_RISK for ev in events)


async def test_simulation_respects_count_parameter(
    override_db: AsyncSession,
) -> None:
    """Different count values must produce the correct number of events."""
    with patch(
        "app.api.v1.endpoints.simulation.process_payment_event",
        new_callable=AsyncMock,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            r3  = await client.post(f"{SIMULATION_URL}?count=3")
            r7  = await client.post(f"{SIMULATION_URL}?count=7")

    assert len(r3.json()["event_ids"])  == 3
    assert len(r7.json()["event_ids"])  == 7

    result = await override_db.execute(select(PaymentEvent))
    total  = result.scalars().all()
    assert len(total) == 10   # 3 + 7


async def test_simulation_queues_background_task_per_event(
    override_db: AsyncSession,
) -> None:
    """process_payment_event must be scheduled once for each generated event."""
    with patch(
        "app.api.v1.endpoints.simulation.process_payment_event",
        new_callable=AsyncMock,
    ) as mock_engine:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(f"{SIMULATION_URL}?count=4")

    assert response.json()["count"] == 4
    assert mock_engine.call_count == 4


# ---------------------------------------------------------------------------
# Metrics endpoint tests
# ---------------------------------------------------------------------------

async def test_metrics_returns_200_and_valid_schema(
    override_db: AsyncSession,
) -> None:
    """GET /metrics must return 200 with all RecoverySummaryStats fields."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(METRICS_URL)

    assert response.status_code == 200, response.text
    data = response.json()

    required_fields = {
        "total_at_risk", "total_in_recovery", "total_recovered",
        "total_failed", "total_stopped",
        "recovery_rate_pct", "total_amount_at_risk", "recovered_amount",
        "action_breakdowns",
    }
    assert required_fields.issubset(data.keys())


async def test_metrics_total_at_risk_count(
    override_db: AsyncSession,
) -> None:
    """total_at_risk must reflect the number of AT_RISK events in the DB."""
    override_db.add_all([
        _seed_event(PaymentStatus.AT_RISK,     500.0),
        _seed_event(PaymentStatus.AT_RISK,     750.0),
        _seed_event(PaymentStatus.IN_RECOVERY, 300.0),
    ])
    await override_db.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(METRICS_URL)

    data = response.json()
    assert data["total_at_risk"]     == 2
    assert data["total_in_recovery"] == 1


async def test_metrics_total_amount_at_risk(
    override_db: AsyncSession,
) -> None:
    """total_amount_at_risk must sum the amounts of AT_RISK events only."""
    override_db.add_all([
        _seed_event(PaymentStatus.AT_RISK, 1000.0),
        _seed_event(PaymentStatus.AT_RISK, 500.0),
        _seed_event(PaymentStatus.RECOVERED, 800.0),  # must NOT be included
    ])
    await override_db.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(METRICS_URL)

    data = response.json()
    assert data["total_amount_at_risk"] == pytest.approx(1500.0, abs=0.01)
    assert data["recovered_amount"]     == pytest.approx(800.0,  abs=0.01)


async def test_metrics_recovery_rate_zero_when_no_recovered(
    override_db: AsyncSession,
) -> None:
    """recovery_rate_pct must be 0.0 when no events are RECOVERED or FAILED."""
    override_db.add_all([
        _seed_event(PaymentStatus.AT_RISK),
        _seed_event(PaymentStatus.IN_RECOVERY),
    ])
    await override_db.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(METRICS_URL)

    assert response.json()["recovery_rate_pct"] == pytest.approx(0.0)


async def test_metrics_recovery_rate_calculation(
    override_db: AsyncSession,
) -> None:
    """recovery_rate_pct = recovered / (recovered + failed) * 100."""
    override_db.add_all([
        _seed_event(PaymentStatus.RECOVERED,       400.0),
        _seed_event(PaymentStatus.RECOVERED,       400.0),
        _seed_event(PaymentStatus.RECOVERED,       400.0),
        _seed_event(PaymentStatus.FAILED_EXHAUSTED, 200.0),
    ])
    await override_db.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(METRICS_URL)

    data = response.json()
    # 3 recovered, 1 failed → 3/4 * 100 = 75.0
    assert data["recovery_rate_pct"] == pytest.approx(75.0, abs=0.1)


async def test_metrics_action_breakdowns_is_list(
    override_db: AsyncSession,
) -> None:
    """action_breakdowns must always be a list (empty when no audit logs)."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(METRICS_URL)

    assert isinstance(response.json()["action_breakdowns"], list)
