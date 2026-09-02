"""
app/tests/test_phase10_economics.py
-----------------------------------
Comprehensive unit and integration test suite for Phase 10:
Customer Value & Cost-Aware Recovery Intelligence.

Test Coverage:
  1. CustomerContextResolver: Deterministic non-PII customer tier & tenure resolution.
  2. Net Recovery Value & Unit Economics Calculation:
     - 50% downgrade math vs 100% full recovery vs ₹0 human escalation.
     - Simulated channel cost deductions (₹0.00, ₹2.50, ₹150.00).
  3. Priority 3 Guardrail Override:
     - Negative unit economics overrides to SILENT_MANDATE_RETRY.
  4. Priority 4 Guardrail Precedence:
     - Anti-harassment attempt ceiling (intervention_count >= 2) overrides unit economics.
  5. Demo Studio Scenarios (A/5, B/6, C/7):
     - End-to-end integration and deterministic execution.
  6. Zero PII Context Protection:
     - Asserts customer names, emails, phone numbers never leak into Gemini analysis prompt.
  7. Audit Log Persistence:
     - Verifies intervention_cost and net_recovery_value are recorded in InterventionAuditLog.
"""

from __future__ import annotations

import json
import uuid
from typing import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.agents.recovery_agent import (
    _build_analysis_prompt,
    _fallback_heuristic_analysis,
    analyze_failure_context,
)
from app.core.database import get_db
from app.core.policies import GuardrailEngine, guardrail_engine
from app.main import app
from app.models.orm import (
    Base,
    InterventionAuditLog,
    PaymentEvent,
    PaymentStatus,
    RecoveryStrategy,
    RecoveryWorkflow,
)
from app.models.schemas import AgentDecision
from app.services.customer_context import (
    SIMULATED_INTERVENTION_COSTS,
    CustomerContext,
    CustomerContextResolver,
    calculate_economics,
)
from app.services.decision_engine import _run_engine

# --------------------------------------------------------------------------- #
# Isolated in-memory database for Phase 10 tests                              #
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


# --------------------------------------------------------------------------- #
# 1. Customer Context Resolver Determinism & Non-PII Isolation               #
# --------------------------------------------------------------------------- #
def test_customer_context_resolver_determinism():
    """Assert that CustomerContextResolver produces identical output for same non-PII event metadata."""
    event1 = PaymentEvent(
        id="evt_test_fixed_uuid_12345",
        customer_id="cust_1",
        customer_name="Secret Name",
        customer_email="secret@email.com",
        customer_contact="+919999999999",
        amount=15000.0,
    )
    event2 = PaymentEvent(
        id="evt_test_fixed_uuid_12345",
        customer_id="cust_2",
        customer_name="Different Name",
        customer_email="different@email.com",
        customer_contact="+918888888888",
        amount=15000.0,
    )

    ctx1 = CustomerContextResolver.resolve(event1)
    ctx2 = CustomerContextResolver.resolve(event2)

    assert ctx1.value_tier == ctx2.value_tier
    assert ctx1.tenure_months == ctx2.tenure_months
    assert ctx1.value_tier == "HIGH"
    assert "Simulated" in ctx1.description


def test_customer_context_resolver_tier_brackets():
    """Assert high-ticket, low-ticket, and payload tags resolve correctly."""
    high_event = PaymentEvent(id=str(uuid.uuid4()), amount=12000.0, customer_contact="+919876543210")
    low_event = PaymentEvent(id=str(uuid.uuid4()), amount=199.0, customer_contact="+919876543210")
    tagged_event = PaymentEvent(
        id=str(uuid.uuid4()),
        customer_contact="+919876543210",
        amount=2000.0,
        raw_payload='{"value_tier": "HIGH", "tenure_months": 48}',
    )

    assert CustomerContextResolver.resolve(high_event).value_tier == "HIGH"
    assert CustomerContextResolver.resolve(low_event).value_tier == "LOW"
    assert CustomerContextResolver.resolve(tagged_event).value_tier == "HIGH"
    assert CustomerContextResolver.resolve(tagged_event).tenure_months == 48


# --------------------------------------------------------------------------- #
# 2. Net Recovery Value & Unit Economics Calculation                          #
# --------------------------------------------------------------------------- #
def test_calculate_economics_standard_strategies():
    """Assert 100% expected amount for standard links and silent retries."""
    # SECURE_PAYMENT_LINK (Cost ₹2.50)
    exp, cost, net = calculate_economics(RecoveryStrategy.SECURE_PAYMENT_LINK, 5000.0)
    assert exp == 5000.0
    assert cost == 2.50
    assert net == 4997.50

    # SILENT_MANDATE_RETRY (Cost ₹0.00)
    exp, cost, net = calculate_economics(RecoveryStrategy.SILENT_MANDATE_RETRY, 500.0)
    assert exp == 500.0
    assert cost == 0.00
    assert net == 500.00

    # UPI_AUTOPAY_MIGRATION (Cost ₹2.50)
    exp, cost, net = calculate_economics(RecoveryStrategy.UPI_AUTOPAY_MIGRATION, 1200.0)
    assert exp == 1200.0
    assert cost == 2.50
    assert net == 1197.50


def test_calculate_economics_adaptive_downgrade():
    """Assert 50% expected amount for adaptive downgrade offers."""
    exp, cost, net = calculate_economics(RecoveryStrategy.ADAPTIVE_DOWNGRADE_OFFER, 10000.0)
    assert exp == 5000.0
    assert cost == 2.50
    assert net == 4997.50


def test_calculate_economics_escalate_to_human():
    """Assert potential recovery value and ₹150 cost for human escalation."""
    exp, cost, net = calculate_economics(RecoveryStrategy.ESCALATE_TO_HUMAN, 500.0)
    assert exp == 500.0
    assert cost == 150.00
    assert net == 350.00

    exp_micro, cost_micro, net_micro = calculate_economics(RecoveryStrategy.ESCALATE_TO_HUMAN, 100.0)
    assert exp_micro == 100.0
    assert cost_micro == 150.00
    assert net_micro == -50.00


# --------------------------------------------------------------------------- #
# 3. Priority 3 Guardrail Override: Negative Unit Economics                   #
# --------------------------------------------------------------------------- #
def test_guardrail_rule4_negative_unit_economics_override():
    """When an expensive strategy yields net <= 0, Guardrail Rule 4 overrides to SILENT_MANDATE_RETRY."""
    engine = GuardrailEngine()
    event = PaymentEvent(
        id=str(uuid.uuid4()),
        customer_contact="+919876543210",
        amount=100.0,
        status=PaymentStatus.AT_RISK,
    )
    workflow = RecoveryWorkflow(
        id=str(uuid.uuid4()),
        payment_event_id=event.id,
        retry_count=0,
        intervention_count=0,
        is_active=True,
    )

    decision = AgentDecision(
        recommended_strategy="ESCALATE_TO_HUMAN",
        confidence_score=0.85,
        reasoning="Proposing manual intervention.",
        requires_consent=False,
    )

    validated = engine.validate_agent_decision(decision, event, workflow)
    assert validated.recommended_strategy == "SILENT_MANDATE_RETRY"
    assert "RULE4_NEGATIVE_UNIT_ECONOMICS_BLOCK" in validated.reasoning


# --------------------------------------------------------------------------- #
# 4. Strict Guardrail Precedence: Priority 4 Safety Beats Economics          #
# --------------------------------------------------------------------------- #
def test_guardrail_precedence_safety_beats_economics():
    """
    Priority 4 (Anti-Harassment ceiling intervention_count >= 2) MUST override
    even if the AI proposes a free SILENT_MANDATE_RETRY with positive unit economics.
    """
    engine = GuardrailEngine()
    event = PaymentEvent(
        id=str(uuid.uuid4()),
        customer_contact="+919876543210",
        amount=200.0,
        status=PaymentStatus.AT_RISK,
    )
    workflow = RecoveryWorkflow(
        id=str(uuid.uuid4()),
        payment_event_id=event.id,
        retry_count=0,
        intervention_count=2,  # Max ceiling reached
        is_active=True,
    )

    # Free strategy (positive unit economics: ₹200 net)
    decision = AgentDecision(
        recommended_strategy="SILENT_MANDATE_RETRY",
        confidence_score=0.90,
        reasoning="Free retry is economically optimal.",
        requires_consent=False,
    )

    validated = engine.validate_agent_decision(decision, event, workflow)
    # Safety beats economics -> must force ESCALATE_TO_HUMAN
    assert validated.recommended_strategy == "ESCALATE_TO_HUMAN"
    assert "Maximum interventions reached" in validated.reasoning


# --------------------------------------------------------------------------- #
# 5. Zero PII Context in Gemini Prompt                                       #
# --------------------------------------------------------------------------- #
def test_zero_pii_in_gemini_prompt():
    """Ensure customer names, emails, contacts, and secrets NEVER appear in LLM prompt."""
    event = PaymentEvent(
        id="evt_pii_check",
        customer_id="cust_real_id",
        customer_name="John Doe",
        customer_email="john.doe@confidential-bank.com",
        customer_contact="+919876543210",
        amount=4500.0,
        currency="INR",
        error_code="BAD_REQUEST_ERROR",
        error_reason="card_declined",
    )
    ctx = CustomerContext(value_tier="HIGH", tenure_months=24)
    prompt = _build_analysis_prompt(event, customer_context=ctx)

    assert "John Doe" not in prompt
    assert "john.doe@confidential-bank.com" not in prompt
    assert "+919876543210" not in prompt
    assert "cust_real_id" not in prompt
    assert "₹4500.00" in prompt
    assert "Customer Value Tier: HIGH" in prompt
    assert "Account Tenure: 24 months" in prompt


# --------------------------------------------------------------------------- #
# 6. End-to-End Decision Engine with Economic Audit Log                      #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_decision_engine_persists_economic_audit_fields(db_session: AsyncSession):
    """Assert intervention_cost and net_recovery_value are stored in InterventionAuditLog."""
    event = PaymentEvent(
        id=str(uuid.uuid4()),
        customer_id="cust_econ_audit",
        customer_name="Economic Test Customer",
        customer_email="econ@test.com",
        customer_contact="+919876543210",
        amount=10000.0,
        status=PaymentStatus.AT_RISK,
        raw_payload='{"tier": "VIP Enterprise", "value_tier": "HIGH", "tenure_months": 36}',
    )
    db_session.add(event)
    await db_session.commit()

    mock_decision = AgentDecision(
        recommended_strategy="SECURE_PAYMENT_LINK",
        confidence_score=0.92,
        reasoning="High tier VIP customer payment link.",
        requires_consent=False,
    )

    with patch(
        "app.services.decision_engine.analyze_failure_context",
        new=AsyncMock(return_value=mock_decision),
    ):
        await _run_engine(db_session, event.id, is_simulated=True)

    # Verify audit log recorded economics
    audit_res = await db_session.execute(
        select(InterventionAuditLog).where(InterventionAuditLog.payment_event_id == event.id)
    )
    logs = list(audit_res.scalars().all())
    assert len(logs) == 1
    log = logs[0]
    assert log.intervention_cost == 2.50
    assert log.net_recovery_value == 9997.50

    meta = json.loads(log.metadata_json or "{}")
    assert meta["customer_value_tier"] == "HIGH"
    assert meta["customer_tenure_months"] == 36
    assert meta["net_recovery_value"] == 9997.50


# --------------------------------------------------------------------------- #
# 7. Demo Studio Phase 10 Scenarios (API Endpoints)                          #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_demo_scenario_5_vip_retention(override_db):
    """Scenario 5 / A: VIP customer full value retention."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/api/v1/demo/scenario/5")
    assert res.status_code == 200
    data = res.json()
    assert data["scenario_id"] == 5
    assert data["customer_value_tier"] == "HIGH"
    assert data["executed_strategy"] == "SECURE_PAYMENT_LINK"
    assert data["net_recovery_value"] == 14997.50
    assert data["final_status"] == "RECOVERED"


@pytest.mark.asyncio
async def test_demo_scenario_6_negative_unit_economics(override_db):
    """Scenario 6 / B: Micro invoice human escalation blocked by Guardrail Rule 4."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/api/v1/demo/scenario/6")
    assert res.status_code == 200
    data = res.json()
    assert data["scenario_id"] == 6
    assert data["executed_strategy"] == "SILENT_MANDATE_RETRY"
    assert "RULE4_NEGATIVE_UNIT_ECONOMICS_BLOCK" in data["guardrail_decision"]
    assert data["net_recovery_value"] == 100.0


@pytest.mark.asyncio
async def test_demo_scenario_7_safety_beats_economics(override_db):
    """Scenario 7 / C: Max attempts ceiling forces ESCALATE_TO_HUMAN despite economics."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/api/v1/demo/scenario/7")
    assert res.status_code == 200
    data = res.json()
    assert data["scenario_id"] == 7
    assert data["executed_strategy"] == "ESCALATE_TO_HUMAN"
    assert data["final_status"] == "ESCALATED_STOPPED"
    assert data["workflow_stopped"] is True
    assert "RULE2_MAX_INTERVENTIONS_ESCALATE" in data["guardrail_decision"]
