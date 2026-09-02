"""
app/tests/test_phase8c_reconciliation_demo.py
---------------------------------------------
Comprehensive test suite for Phase 8C:
  1. Success webhook ingestion (payment_link.paid, payment.captured).
  2. Unified reconciliation service (50% downgrade math vs 100% full recovery).
  3. Idempotency & late-arrival protection.
  4. Adaptive multi-attempt loop & escalation ceiling.
  5. Interactive Demo Studio deterministic scenario endpoints (Scenarios 1-4).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.database import get_db
from app.main import app
from app.models.orm import (
    Base,
    DiagnosedCause,
    InterventionAuditLog,
    PaymentEvent,
    PaymentStatus,
    RecoveryStrategy,
    RecoveryWorkflow,
)
from app.models.schemas import AgentDecision
from app.services.decision_engine import _run_engine, process_payment_event
from app.services.reconciliation import reconcile_payment_success

# --------------------------------------------------------------------------- #
# Isolated test database                                                      #
# --------------------------------------------------------------------------- #
TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

_test_engine = create_async_engine(
    TEST_DB_URL,
    echo=False,
    connect_args={"check_same_thread": False},
)

_TestSession: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=_test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)

TEST_SECRET = "test_phase8c_webhook_secret_xyz"


@pytest.fixture(autouse=True)
async def setup_db():
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    async with _TestSession() as session:
        yield session


@pytest.fixture
def override_db(db_session: AsyncSession):
    async def _override():
        yield db_session

    app.dependency_overrides[get_db] = _override
    yield
    app.dependency_overrides.pop(get_db, None)


def _sign(payload: dict | bytes, secret: str = TEST_SECRET) -> str:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- #
# 1. Webhook Ingestion Tests for payment_link.paid and payment.captured       #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_payment_link_paid_webhook_ingestion(override_db, db_session: AsyncSession):
    """Verify payment_link.paid webhook validates signature and queues reconciliation."""
    payload = {
        "entity": "event",
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_Test001",
                    "reference_id": "evt_ref_12345",
                    "amount": 99900,
                    "amount_paid": 99900,
                    "status": "paid",
                }
            }
        },
    }
    body = json.dumps(payload).encode()
    sig = _sign(body)

    with patch("app.api.v1.endpoints.webhooks.settings") as mock_settings, \
         patch("app.api.v1.endpoints.webhooks.reconcile_payment_success", new_callable=AsyncMock) as mock_recon:
        mock_settings.RAZORPAY_WEBHOOK_SECRET = TEST_SECRET

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/webhooks/razorpay",
                content=body,
                headers={"content-type": "application/json", "x-razorpay-signature": sig},
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["event"] == "payment_link.paid"
    assert data["reconciled"] is True
    assert data["reference_id"] == "evt_ref_12345"
    mock_recon.assert_called_once()


@pytest.mark.asyncio
async def test_payment_captured_webhook_ingestion(override_db, db_session: AsyncSession):
    """Verify payment.captured webhook validates signature and extracts notes/identifiers."""
    payload = {
        "entity": "event",
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_Captured001",
                    "order_id": "order_Captured001",
                    "amount": 150000,
                    "status": "captured",
                    "notes": {"reference_id": "evt_captured_999"},
                }
            }
        },
    }
    body = json.dumps(payload).encode()
    sig = _sign(body)

    with patch("app.api.v1.endpoints.webhooks.settings") as mock_settings, \
         patch("app.api.v1.endpoints.webhooks.reconcile_payment_success", new_callable=AsyncMock) as mock_recon:
        mock_settings.RAZORPAY_WEBHOOK_SECRET = TEST_SECRET

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/webhooks/razorpay",
                content=body,
                headers={"content-type": "application/json", "x-razorpay-signature": sig},
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["event"] == "payment.captured"
    assert data["reference_id"] == "evt_captured_999"
    mock_recon.assert_called_once()


# --------------------------------------------------------------------------- #
# 2. Unified Reconciliation Service Tests (50% Downgrade vs Full Recovery)   #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_reconcile_adaptive_downgrade_captures_50_percent(db_session: AsyncSession):
    """
    When the executed strategy was ADAPTIVE_DOWNGRADE_OFFER:
    Reconciliation MUST capture exactly 50% of the original invoice amount.
    """
    event = PaymentEvent(
        id=str(uuid.uuid4()),
        customer_id="cust_1",
        customer_name="Test User",
        customer_email="test@user.com",
        customer_contact="+919876543210",
        amount=10000.0,
        currency="INR",
        status=PaymentStatus.INTERVENTION_ACTIVE,
    )
    workflow = RecoveryWorkflow(
        id=str(uuid.uuid4()),
        payment_event_id=event.id,
        diagnosed_cause=DiagnosedCause.INSUFFICIENT_FUNDS_ADAPTIVE,
        strategy=RecoveryStrategy.ADAPTIVE_DOWNGRADE_OFFER,
        current_step=1,
        max_steps=3,
        retry_count=0,
        is_active=True,
    )
    db_session.add(event)
    db_session.add(workflow)
    await db_session.commit()

    result = await reconcile_payment_success(
        payment_identifier=event.id,
        db=db_session,
        source="WEBHOOK_TEST",
    )

    assert result is not None
    assert result["status"] == "success"
    assert result["original_amount"] == 10000.0
    assert result["amount_recovered"] == 5000.0  # Exactly 50%
    assert result["downgrade_applied"] is True

    # Check DB state
    fetched_event = await db_session.get(PaymentEvent, event.id)
    fetched_wf = await db_session.get(RecoveryWorkflow, workflow.id)
    assert fetched_event.status == PaymentStatus.RECOVERED
    assert fetched_wf.is_active is False
    assert fetched_wf.amount_recovered == 5000.0


@pytest.mark.asyncio
async def test_reconcile_full_recovery_for_standard_strategies(db_session: AsyncSession):
    """
    Standard strategies (SECURE_PAYMENT_LINK, SILENT_MANDATE_RETRY, UPI_AUTOPAY_MIGRATION)
    must capture 100% of the invoice amount upon reconciliation.
    """
    event = PaymentEvent(
        id=str(uuid.uuid4()),
        customer_id="cust_2",
        customer_name="Standard User",
        customer_email="standard@user.com",
        customer_contact="+919876543211",
        amount=2500.0,
        currency="INR",
        status=PaymentStatus.INTERVENTION_ACTIVE,
    )
    workflow = RecoveryWorkflow(
        id=str(uuid.uuid4()),
        payment_event_id=event.id,
        diagnosed_cause=DiagnosedCause.EXPIRED_PAYMENT_METHOD,
        strategy=RecoveryStrategy.SECURE_PAYMENT_LINK,
        current_step=1,
        max_steps=3,
        retry_count=0,
        is_active=True,
    )
    db_session.add(event)
    db_session.add(workflow)
    await db_session.commit()

    result = await reconcile_payment_success(
        payment_identifier=event.id,
        amount_paid=2500.0,
        db=db_session,
        source="WEBHOOK_TEST",
    )

    assert result is not None
    assert result["amount_recovered"] == 2500.0
    assert result["downgrade_applied"] is False

    fetched_event = await db_session.get(PaymentEvent, event.id)
    fetched_wf = await db_session.get(RecoveryWorkflow, workflow.id)
    assert fetched_event.status == PaymentStatus.RECOVERED
    assert fetched_wf.amount_recovered == 2500.0


@pytest.mark.asyncio
async def test_reconciliation_idempotency_already_recovered(db_session: AsyncSession):
    """Reconciling an already RECOVERED event must be an idempotent no-op."""
    event = PaymentEvent(
        id=str(uuid.uuid4()),
        customer_id="cust_3",
        customer_name="Recovered User",
        customer_email="rec@user.com",
        customer_contact="+919876543212",
        amount=999.0,
        currency="INR",
        status=PaymentStatus.RECOVERED,
    )
    db_session.add(event)
    await db_session.commit()

    result = await reconcile_payment_success(
        payment_identifier=event.id,
        db=db_session,
    )

    assert result is not None
    assert result["status"] == "already_recovered"
    assert result["idempotent"] is True


@pytest.mark.asyncio
async def test_reconciliation_late_arrival_on_escalated_stopped(db_session: AsyncSession):
    """A late-arriving payment for an ESCALATED_STOPPED event logs without overriding terminal state."""
    event = PaymentEvent(
        id=str(uuid.uuid4()),
        customer_id="cust_4",
        customer_name="Escalated User",
        customer_email="esc@user.com",
        customer_contact="+919876543213",
        amount=5000.0,
        currency="INR",
        status=PaymentStatus.ESCALATED_STOPPED,
    )
    db_session.add(event)
    await db_session.commit()

    result = await reconcile_payment_success(
        payment_identifier=event.id,
        db=db_session,
    )

    assert result is not None
    assert result["status"] == "escalated_stopped_late_arrival"
    assert result["idempotent"] is True
    assert event.status == PaymentStatus.ESCALATED_STOPPED


# --------------------------------------------------------------------------- #
# 3. Adaptive Multi-Attempt Loop Tests                                        #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_adaptive_loop_escalates_on_attempt_2_ceiling(db_session: AsyncSession):
    """
    When Attempt 1 has occurred (intervention_count=1) and Attempt 2 runs:
    If intervention_count reaches 2, Guardrail Rule 2 forces ESCALATE_TO_HUMAN
    and marks event as ESCALATED_STOPPED with workflow.is_active=False.
    """
    event = PaymentEvent(
        id=str(uuid.uuid4()),
        customer_id="cust_multi",
        customer_name="Multi-Attempt Customer",
        customer_email="multi@corp.com",
        customer_contact="+919876543214",
        amount=3000.0,
        currency="INR",
        error_code="BAD_REQUEST_ERROR",
        error_reason="card_declined",
        status=PaymentStatus.AT_RISK,
    )
    db_session.add(event)
    await db_session.commit()

    # Attempt 1
    mock_decision_1 = AgentDecision(
        recommended_strategy="SECURE_PAYMENT_LINK",
        confidence_score=0.90,
        reasoning="Attempt 1: Send link.",
        requires_consent=False,
    )
    with patch("app.services.decision_engine.analyze_failure_context", new=AsyncMock(return_value=mock_decision_1)):
        await _run_engine(db_session, event.id)

    # Simulate Attempt 1 timeout -> reset status to DIAGNOSED and simulate attempt 2
    event.status = PaymentStatus.DIAGNOSED
    wf_res = await db_session.execute(select(RecoveryWorkflow).where(RecoveryWorkflow.payment_event_id == event.id))
    wf1 = wf_res.scalars().first()
    assert wf1.intervention_count == 1

    # Simulate intervention_count being 2 on the next cycle
    wf1.intervention_count = 2
    await db_session.commit()

    mock_decision_2 = AgentDecision(
        recommended_strategy="SECURE_PAYMENT_LINK",
        confidence_score=0.75,
        reasoning="Attempt 2: Retry link.",
        requires_consent=False,
    )
    with patch("app.services.decision_engine.analyze_failure_context", new=AsyncMock(return_value=mock_decision_2)):
        await _run_engine(db_session, event.id)

    fetched_event = await db_session.get(PaymentEvent, event.id)
    assert fetched_event.status == PaymentStatus.ESCALATED_STOPPED


# --------------------------------------------------------------------------- #
# 4. Interactive Demo Studio Endpoints Tests (Scenarios 1 - 4)                #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_demo_scenario_list(override_db):
    """GET /api/v1/demo/scenarios returns all 4 defined scenarios."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/v1/demo/scenarios")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert len(data["scenarios"]) == 4


@pytest.mark.asyncio
async def test_demo_scenario_1_annual_downgrade(override_db):
    """Scenario 1: ₹12,000 -> 50% Downgrade Reconciled (₹6,000)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/demo/scenario/1")

    assert resp.status_code == 200
    data = resp.json()
    assert data["scenario_id"] == 1
    assert data["original_amount"] == 12000.0
    assert data["recovered_amount"] == 6000.0
    assert data["downgrade_applied"] is True
    assert data["final_status"] == "RECOVERED"
    assert data["guardrail_decision"] == "APPROVED"


@pytest.mark.asyncio
async def test_demo_scenario_2_micro_transaction_gate(override_db):
    """Scenario 2: ₹199 -> Guardrail blocks downgrade to SILENT_MANDATE_RETRY."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/demo/scenario/2")

    assert resp.status_code == 200
    data = resp.json()
    assert data["scenario_id"] == 2
    assert data["original_amount"] == 199.0
    assert data["executed_strategy"] == "SILENT_MANDATE_RETRY"
    assert "OVERRIDDEN" in data["guardrail_decision"]
    assert data["final_status"] == "INTERVENTION_ACTIVE"


@pytest.mark.asyncio
async def test_demo_scenario_3_autonomous_escalation(override_db):
    """Scenario 3: Attempt limit ceiling -> ESCALATE_TO_HUMAN + ESCALATED_STOPPED."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/demo/scenario/3")

    assert resp.status_code == 200
    data = resp.json()
    assert data["scenario_id"] == 3
    assert data["attempt_2_strategy"] == "ESCALATE_TO_HUMAN"
    assert "OVERRIDDEN" in data["guardrail_decision"]
    assert data["final_status"] == "ESCALATED_STOPPED"
    assert data["workflow_stopped"] is True


@pytest.mark.asyncio
async def test_demo_scenario_4_gemini_outage_resilience(override_db):
    """Scenario 4: Simulated 503 Gemini Outage -> Safe [FALLBACK] SECURE_PAYMENT_LINK."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/demo/scenario/4")

    assert resp.status_code == 200
    data = resp.json()
    assert data["scenario_id"] == 4
    assert data["fallback_strategy"] == "SECURE_PAYMENT_LINK"
    assert data["confidence_score"] == 0.0
    assert "[FALLBACK]" in data["ai_reasoning"]
    assert data["final_status"] == "INTERVENTION_ACTIVE"
