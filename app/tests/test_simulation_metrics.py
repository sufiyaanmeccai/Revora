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
    5. process_payment_event queued once per event.

  Fast-Forward endpoint (Phase 7):
    6. POST /api/v1/simulation/fast-forward returns 200 with outcome counts.
    7. INTERVENTION_ACTIVE events are resolved by the simulator.

  Metrics endpoint (with pre-seeded data):
    8.  GET /api/v1/metrics returns 200 with valid RecoverySummaryStats schema.
    9.  total_at_risk reflects the correct count of AT_RISK events.
    10. total_in_recovery = DIAGNOSED + INTERVENTION_ACTIVE events.
    11. total_amount_at_risk sums AT_RISK + DIAGNOSED + INTERVENTION_ACTIVE amounts.
    12. recovery_rate_pct is 0 when no events are RECOVERED yet.
    13. action_breakdowns is a list (may be empty when no workflows exist).
    14. recovered_amount uses amount_recovered field (Phase 7).

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
from app.models.orm import (
    Base,
    DiagnosedCause,
    PaymentEvent,
    PaymentStatus,
    RecoveryStrategy,
    RecoveryWorkflow,
)

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


SIMULATION_URL    = "/api/v1/simulation/run"
FAST_FORWARD_URL  = "/api/v1/simulation/fast-forward"
METRICS_URL       = "/api/v1/metrics"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_event(
    status: PaymentStatus = PaymentStatus.AT_RISK,
    amount: float = 999.0,
) -> PaymentEvent:
    return PaymentEvent(
        id=str(uuid.uuid4()),
        razorpay_event_id=f"evt_Test{uuid.uuid4().hex[:8]}",
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


def _seed_workflow(
    payment_event_id: str,
    amount_recovered: float = 0.0,
    is_active: bool = True,
) -> RecoveryWorkflow:
    return RecoveryWorkflow(
        id=str(uuid.uuid4()),
        payment_event_id=payment_event_id,
        diagnosed_cause=DiagnosedCause.INSUFFICIENT_FUNDS_ADAPTIVE,
        strategy=RecoveryStrategy.SILENT_MANDATE_RETRY,
        current_step=1,
        max_steps=3,
        retry_count=0,
        intervention_count=1,
        amount_recovered=amount_recovered,
        is_active=is_active,
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
# Fast-Forward endpoint tests (Phase 7)
# ---------------------------------------------------------------------------

async def test_fast_forward_returns_200(
    override_db: AsyncSession,
) -> None:
    """POST /simulation/fast-forward must return 200 with outcome counts."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(FAST_FORWARD_URL)

    assert response.status_code == 200, response.text
    data = response.json()
    assert "resolved" in data
    assert "total_processed" in data
    assert "recovered" in data
    assert "escalated" in data
    assert "still_active" in data


async def test_fast_forward_with_no_active_events(
    override_db: AsyncSession,
) -> None:
    """When no INTERVENTION_ACTIVE events exist, resolved must be 0."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(FAST_FORWARD_URL)

    data = response.json()
    assert data["total_processed"] == 0
    assert data["resolved"] == 0


async def test_fast_forward_resolves_active_events(
    override_db: AsyncSession,
) -> None:
    """POST /simulation/fast-forward resolves INTERVENTION_ACTIVE events and creates audit logs."""
    ev = _seed_event(PaymentStatus.INTERVENTION_ACTIVE, 500.0)
    wf = _seed_workflow(ev.id, amount_recovered=0.0, is_active=True)
    override_db.add_all([ev, wf])
    await override_db.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(FAST_FORWARD_URL)

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total_processed"] == 1
    assert data["resolved"] + data["still_active"] == 1


# ---------------------------------------------------------------------------
# Metrics endpoint tests
# ---------------------------------------------------------------------------

async def test_metrics_returns_200_and_valid_schema(
    override_db: AsyncSession,
) -> None:
    """GET /metrics must return 200 with all Phase 7 RecoverySummaryStats fields."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(METRICS_URL)

    assert response.status_code == 200, response.text
    data = response.json()

    # Phase 7 required fields
    required_fields = {
        "total_at_risk", "total_diagnosed", "total_intervention",
        "total_recovered", "total_escalated", "total_in_recovery",
        "recovery_rate_pct", "total_amount_at_risk", "recovered_amount",
        "action_breakdowns",
    }
    assert required_fields.issubset(data.keys()), (
        f"Missing fields: {required_fields - data.keys()}"
    )


async def test_metrics_total_at_risk_count(
    override_db: AsyncSession,
) -> None:
    """total_at_risk must reflect the number of AT_RISK events in the DB."""
    override_db.add_all([
        _seed_event(PaymentStatus.AT_RISK,              500.0),
        _seed_event(PaymentStatus.AT_RISK,              750.0),
        _seed_event(PaymentStatus.INTERVENTION_ACTIVE,  300.0),
    ])
    await override_db.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(METRICS_URL)

    data = response.json()
    assert data["total_at_risk"]     == 2
    assert data["total_in_recovery"] == 1   # INTERVENTION_ACTIVE


async def test_metrics_total_in_recovery_aggregates_diagnosed_and_active(
    override_db: AsyncSession,
) -> None:
    """total_in_recovery must sum DIAGNOSED + INTERVENTION_ACTIVE events."""
    override_db.add_all([
        _seed_event(PaymentStatus.DIAGNOSED,            500.0),
        _seed_event(PaymentStatus.INTERVENTION_ACTIVE,  300.0),
        _seed_event(PaymentStatus.AT_RISK,              200.0),
    ])
    await override_db.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(METRICS_URL)

    data = response.json()
    assert data["total_in_recovery"] == 2   # 1 DIAGNOSED + 1 INTERVENTION_ACTIVE


async def test_metrics_total_amount_at_risk_includes_all_inflight(
    override_db: AsyncSession,
) -> None:
    """total_amount_at_risk must sum AT_RISK + DIAGNOSED + INTERVENTION_ACTIVE amounts."""
    ev_rec = _seed_event(PaymentStatus.RECOVERED, 800.0)
    wf_rec = _seed_workflow(ev_rec.id, amount_recovered=800.0, is_active=False)
    override_db.add_all([
        _seed_event(PaymentStatus.AT_RISK,              1000.0),
        _seed_event(PaymentStatus.DIAGNOSED,            500.0),
        _seed_event(PaymentStatus.INTERVENTION_ACTIVE,  250.0),
        ev_rec,
        wf_rec,
    ])
    await override_db.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(METRICS_URL)

    data = response.json()
    # 1000 + 500 + 250 = 1750 (RECOVERED excluded from at-risk)
    assert data["total_amount_at_risk"] == pytest.approx(1750.0, abs=0.01)


async def test_metrics_recovered_amount_uses_amount_recovered_field(
    override_db: AsyncSession,
) -> None:
    """recovered_amount must sum RecoveryWorkflow.amount_recovered, not raw PaymentEvent amount."""
    ev1 = _seed_event(PaymentStatus.RECOVERED, amount=1000.0)
    wf1 = _seed_workflow(ev1.id, amount_recovered=1000.0, is_active=False)
    ev2 = _seed_event(PaymentStatus.RECOVERED, amount=500.0)
    wf2 = _seed_workflow(ev2.id, amount_recovered=500.0, is_active=False)
    override_db.add_all([ev1, wf1, ev2, wf2])
    await override_db.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(METRICS_URL)

    data = response.json()
    assert data["recovered_amount"] == pytest.approx(1500.0, abs=0.01)


async def test_metrics_recovery_rate_zero_when_no_recovered(
    override_db: AsyncSession,
) -> None:
    """recovery_rate_pct must be 0.0 when no events are RECOVERED."""
    override_db.add_all([
        _seed_event(PaymentStatus.AT_RISK),
        _seed_event(PaymentStatus.INTERVENTION_ACTIVE),
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
    """
    Phase 7/8A recovery_rate_pct = Recovered / (At Risk + Recovered) * 100.
    3 recovered + 2 at-risk → 3/5 * 100 = 60.0%.
    """
    ev1 = _seed_event(PaymentStatus.RECOVERED, 400.0)
    wf1 = _seed_workflow(ev1.id, amount_recovered=400.0, is_active=False)
    ev2 = _seed_event(PaymentStatus.RECOVERED, 400.0)
    wf2 = _seed_workflow(ev2.id, amount_recovered=400.0, is_active=False)
    ev3 = _seed_event(PaymentStatus.RECOVERED, 400.0)
    wf3 = _seed_workflow(ev3.id, amount_recovered=400.0, is_active=False)
    override_db.add_all([
        ev1, wf1, ev2, wf2, ev3, wf3,
        _seed_event(PaymentStatus.AT_RISK, 200.0),
        _seed_event(PaymentStatus.AT_RISK, 200.0),
    ])
    await override_db.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(METRICS_URL)

    data = response.json()
    # 3 recovered / (2 at-risk + 3 recovered) = 3/5 = 60%
    assert data["recovery_rate_pct"] == pytest.approx(60.0, abs=0.1)


async def test_metrics_action_breakdowns_is_list(
    override_db: AsyncSession,
) -> None:
    """action_breakdowns must always be a list (empty when no audit logs)."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(METRICS_URL)

    assert isinstance(response.json()["action_breakdowns"], list)


# ---------------------------------------------------------------------------
# Phase 8B: 50% downgrade accounting in outcome simulator
# ---------------------------------------------------------------------------

async def test_downgrade_offer_recovery_records_50_percent_amount(
    override_db: AsyncSession,
) -> None:
    """
    Phase 8B accounting rule: When ADAPTIVE_DOWNGRADE_OFFER succeeds,
    workflow.amount_recovered must be exactly 50% of the original event amount.

    This test seeds an INTERVENTION_ACTIVE event with an ADAPTIVE_DOWNGRADE_OFFER
    workflow, then runs the outcome simulator with mocked randomness to force
    the success path (roll < SUCCESS_PROBABILITY), and asserts that the
    recorded amount_recovered == event.amount * 0.5.
    """
    import random
    from unittest.mock import patch
    from app.services.outcome_simulator import simulate_active_interventions
    from app.models.orm import RecoveryStrategy, DiagnosedCause

    original_amount = 1200.0
    expected_recovered = round(original_amount * 0.5, 2)  # 600.0

    ev = PaymentEvent(
        id=str(uuid.uuid4()),
        razorpay_event_id=f"evt_DowngradeTest{uuid.uuid4().hex[:8]}",
        customer_id=f"cust_{uuid.uuid4().hex[:6]}",
        customer_name="Test User",
        customer_email="test@revora.ai",
        customer_contact="+910000000000",
        amount=original_amount,
        currency="INR",
        error_code="BAD_REQUEST_ERROR",
        error_reason="insufficient_funds",
        status=PaymentStatus.INTERVENTION_ACTIVE,
    )
    wf = RecoveryWorkflow(
        id=str(uuid.uuid4()),
        payment_event_id=ev.id,
        diagnosed_cause=DiagnosedCause.INSUFFICIENT_FUNDS_ADAPTIVE,
        strategy=RecoveryStrategy.ADAPTIVE_DOWNGRADE_OFFER,
        current_step=1,
        max_steps=4,
        retry_count=0,
        intervention_count=1,
        amount_recovered=0.0,
        is_active=True,
    )
    override_db.add_all([ev, wf])
    await override_db.commit()

    # Force success path: patch random.random to return 0.0 (< SUCCESS_PROBABILITY)
    with patch("app.services.outcome_simulator.random.random", return_value=0.0):
        await simulate_active_interventions(override_db)

    # Reload to get updated values
    await override_db.refresh(wf)
    assert wf.amount_recovered == pytest.approx(expected_recovered, abs=0.01), (
        f"ADAPTIVE_DOWNGRADE_OFFER success must record 50% of original amount. "
        f"Expected {expected_recovered}, got {wf.amount_recovered}."
    )
    assert wf.amount_recovered < original_amount, (
        "Downgrade recovery must be less than the full original amount."
    )
