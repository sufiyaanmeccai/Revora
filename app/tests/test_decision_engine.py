"""
app/tests/test_decision_engine.py
----------------------------------
Async pytest tests for the Phase 7 Revora Decision Engine.

Coverage:
  1. _diagnose() pure-function unit tests (preserved from prior phases).
  2. State machine: AT_RISK → DIAGNOSED → INTERVENTION_ACTIVE on processing.
  3. Idempotency: RECOVERED (terminal) → engine no-op.
  4. Idempotency: ESCALATED_STOPPED (terminal) → engine no-op.
  5. Idempotency: INTERVENTION_ACTIVE already set → engine no-op.
  6. Comprehensive audit log is created (single log per run, action=INTERVENTION_DISPATCHED).
  7. Guardrail integration: low-ticket insufficient_funds → downgrade blocked.
  8. Workflow created with correct strategy and cause fields.
  9. Gateway error → SILENT_MANDATE_RETRY workflow.
 10. Card declined → SECURE_PAYMENT_LINK + WHATSAPP_NUDGE_SENT in audit metadata.

Strategy:
  • Each test creates an isolated in-memory SQLite DB with its own schema.
  • A PaymentEvent is inserted directly.
  • ``process_payment_event`` is called with the test session factory injected
    via the ``_session_factory`` parameter — no patching of module globals.
  • Assertions query the DB directly to verify ORM state.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.models.orm import (
    Base,
    DiagnosedCause,
    InterventionAuditLog,
    PaymentEvent,
    PaymentStatus,
    RecoveryStrategy,
    RecoveryWorkflow,
)
from unittest.mock import AsyncMock, patch

from app.models.schemas import AgentDecision
from app.services.decision_engine import _diagnose, process_payment_event

# --------------------------------------------------------------------------- #
# Test engine factory — each test gets a fresh in-memory DB                   #
# --------------------------------------------------------------------------- #
def _make_test_factory() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Return (engine, sessionmaker) backed by a fresh in-memory SQLite DB."""
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


async def _bootstrap(engine: AsyncEngine) -> None:
    """Create all tables in the given async engine."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _event(
    error_code: str = "UNKNOWN",
    error_reason: str = "",
    amount: float = 999.0,
    status: PaymentStatus = PaymentStatus.AT_RISK,
) -> PaymentEvent:
    return PaymentEvent(
        id=str(uuid.uuid4()),
        razorpay_event_id=f"evt_Test{uuid.uuid4().hex[:8]}",
        customer_id="cust_test",
        customer_name="Test User",
        customer_email="test@revora.ai",
        customer_contact="+910000000000",
        amount=amount,
        currency="INR",
        error_code=error_code,
        error_reason=error_reason,
        status=status,
    )


# --------------------------------------------------------------------------- #
# Unit tests on _diagnose() — pure function, no DB needed                     #
# --------------------------------------------------------------------------- #

def test_diagnose_gateway_error() -> None:
    result = _diagnose("GATEWAY_ERROR", "timeout", 499.0)
    assert result.cause    == DiagnosedCause.TEMPORARY_NETWORK_FAILURE
    assert result.strategy == RecoveryStrategy.SILENT_MANDATE_RETRY
    assert "Network Failure" in result.reasoning


def test_diagnose_card_declined() -> None:
    result = _diagnose("BAD_REQUEST_ERROR", "card_declined", 299.0)
    assert result.cause    == DiagnosedCause.EXPIRED_PAYMENT_METHOD
    assert result.strategy == RecoveryStrategy.SECURE_PAYMENT_LINK


def test_diagnose_insufficient_funds_high_ticket() -> None:
    result = _diagnose("BAD_REQUEST_ERROR", "insufficient_funds", 2000.0)
    assert result.cause    == DiagnosedCause.INSUFFICIENT_FUNDS_ADAPTIVE
    assert result.strategy == RecoveryStrategy.ADAPTIVE_DOWNGRADE_OFFER


def test_diagnose_insufficient_funds_low_ticket() -> None:
    result = _diagnose("BAD_REQUEST_ERROR", "insufficient_funds", 99.0)
    assert result.cause    == DiagnosedCause.INSUFFICIENT_FUNDS_ADAPTIVE
    assert result.strategy == RecoveryStrategy.SILENT_MANDATE_RETRY


def test_diagnose_default_fallback() -> None:
    result = _diagnose("UNKNOWN_CODE", "some_weird_reason", 500.0)
    assert result.cause    == DiagnosedCause.MANDATE_DECLINED
    assert result.strategy == RecoveryStrategy.SECURE_PAYMENT_LINK


# --------------------------------------------------------------------------- #
# Phase 7 state machine tests                                                  #
# --------------------------------------------------------------------------- #

async def test_state_machine_at_risk_to_intervention_active() -> None:
    """
    Core pipeline: AT_RISK → DIAGNOSED → INTERVENTION_ACTIVE.
    The event's final status must be INTERVENTION_ACTIVE.
    """
    eng, factory = _make_test_factory()
    await _bootstrap(eng)

    ev = _event(error_code="GATEWAY_ERROR", error_reason="timeout", amount=999.0)
    async with factory() as session:
        session.add(ev)
        await session.commit()

    assert ev.status == PaymentStatus.AT_RISK  # precondition

    await process_payment_event(ev.id, _session_factory=factory)

    async with factory() as session:
        fetched = await session.get(PaymentEvent, ev.id)

    assert fetched is not None
    assert fetched.status == PaymentStatus.INTERVENTION_ACTIVE, (
        f"Expected INTERVENTION_ACTIVE, got {fetched.status}"
    )


async def test_idempotency_recovered_is_terminal() -> None:
    """
    Idempotency: RECOVERED is a terminal state. Engine must be a no-op.
    No workflow should be created.
    """
    eng, factory = _make_test_factory()
    await _bootstrap(eng)

    ev = _event(error_code="GATEWAY_ERROR", error_reason="timeout")
    ev.status = PaymentStatus.RECOVERED  # pre-set terminal
    async with factory() as session:
        session.add(ev)
        await session.commit()

    await process_payment_event(ev.id, _session_factory=factory)

    async with factory() as session:
        wf_result = await session.execute(
            select(RecoveryWorkflow).where(RecoveryWorkflow.payment_event_id == ev.id)
        )
        workflows = wf_result.scalars().all()

    assert workflows == [], "Engine must skip RECOVERED events (terminal state)."


async def test_idempotency_escalated_stopped_is_terminal() -> None:
    """
    Idempotency: ESCALATED_STOPPED is a terminal state. Engine must be a no-op.
    """
    eng, factory = _make_test_factory()
    await _bootstrap(eng)

    ev = _event(error_code="GATEWAY_ERROR", error_reason="timeout")
    ev.status = PaymentStatus.ESCALATED_STOPPED  # pre-set terminal
    async with factory() as session:
        session.add(ev)
        await session.commit()

    await process_payment_event(ev.id, _session_factory=factory)

    async with factory() as session:
        wf_result = await session.execute(
            select(RecoveryWorkflow).where(RecoveryWorkflow.payment_event_id == ev.id)
        )
        workflows = wf_result.scalars().all()

    assert workflows == [], "Engine must skip ESCALATED_STOPPED events (terminal state)."


async def test_idempotency_intervention_active_skipped() -> None:
    """
    Idempotency: INTERVENTION_ACTIVE events must not be reprocessed.
    """
    eng, factory = _make_test_factory()
    await _bootstrap(eng)

    ev = _event(error_code="GATEWAY_ERROR", error_reason="timeout")
    ev.status = PaymentStatus.INTERVENTION_ACTIVE  # pre-set
    async with factory() as session:
        session.add(ev)
        await session.commit()

    await process_payment_event(ev.id, _session_factory=factory)

    async with factory() as session:
        wf_result = await session.execute(
            select(RecoveryWorkflow).where(RecoveryWorkflow.payment_event_id == ev.id)
        )
        workflows = wf_result.scalars().all()

    assert workflows == [], "Engine must skip INTERVENTION_ACTIVE events (already processed)."


async def test_single_comprehensive_audit_log_created() -> None:
    """
    Phase 7/8A: A single INTERVENTION_DISPATCHED audit log must be created
    per pipeline run (not two separate logs).
    The log must contain structured AI fields and guardrail outcome.
    """
    eng, factory = _make_test_factory()
    await _bootstrap(eng)

    ev = _event(error_code="GATEWAY_ERROR", error_reason="timeout", amount=999.0)
    async with factory() as session:
        session.add(ev)
        await session.commit()

    await process_payment_event(ev.id, _session_factory=factory)

    async with factory() as session:
        log_result = await session.execute(
            select(InterventionAuditLog).where(InterventionAuditLog.payment_event_id == ev.id)
        )
        logs = log_result.scalars().all()

    assert len(logs) == 1, (
        f"Phase 8A requires exactly 1 comprehensive audit log per run, found {len(logs)}."
    )

    log = logs[0]
    # Phase 8A column check
    assert log.executed_strategy == "INTERVENTION_DISPATCHED"
    assert log.ai_recommended_strategy is not None
    assert log.ai_confidence is not None
    assert log.guardrail_decision in ("APPROVED", "OVERRIDDEN")

    # Reasoning must contain both agent and guardrail sections
    assert "AGENT REASONING" in log.reasoning, "Audit log must contain agent reasoning."
    assert "GUARDRAIL VALIDATION" in log.reasoning, "Audit log must contain guardrail outcome."

    # Metadata must contain all audit fields
    assert log.metadata_json is not None
    meta = json.loads(log.metadata_json)
    assert "workflow_strategy" in meta
    assert "guardrail_validated_strategy" in meta
    assert "guardrail_overridden" in meta
    assert "outreach_action" in meta
    assert "outreach_channel" in meta


async def test_gateway_error_creates_network_failure_workflow() -> None:
    """Gateway error → TEMPORARY_NETWORK_FAILURE + SILENT_MANDATE_RETRY workflow."""
    eng, factory = _make_test_factory()
    await _bootstrap(eng)

    ev = _event(error_code="GATEWAY_ERROR", error_reason="timeout", amount=499.0)
    async with factory() as session:
        session.add(ev)
        await session.commit()

    # Inject heuristic-equivalent agent decision so no API key is required
    mock_decision = AgentDecision(
        recommended_strategy="SILENT_MANDATE_RETRY",
        confidence_score=0.93,
        reasoning="[AGENT:HEURISTIC] Transient network disruption.",
        requires_consent=False,
    )
    with patch(
        "app.services.decision_engine.analyze_failure_context",
        new=AsyncMock(return_value=mock_decision),
    ):
        await process_payment_event(ev.id, _session_factory=factory)

    async with factory() as session:
        wf_result = await session.execute(
            select(RecoveryWorkflow).where(RecoveryWorkflow.payment_event_id == ev.id)
        )
        wf = wf_result.scalar_one()

    assert wf.diagnosed_cause == DiagnosedCause.TEMPORARY_NETWORK_FAILURE
    assert wf.strategy        == RecoveryStrategy.SILENT_MANDATE_RETRY
    assert wf.is_active       is True
    assert wf.current_step    == 1



async def test_card_declined_audit_log_metadata() -> None:
    """Card declined → SECURE_PAYMENT_LINK. Audit log metadata must have outreach_action."""
    eng, factory = _make_test_factory()
    await _bootstrap(eng)

    ev = _event(error_code="BAD_REQUEST_ERROR", error_reason="card_declined", amount=499.0)
    async with factory() as session:
        session.add(ev)
        await session.commit()

    mock_decision = AgentDecision(
        recommended_strategy="SECURE_PAYMENT_LINK",
        confidence_score=0.90,
        reasoning="[AGENT:HEURISTIC] Failure indicates an expired or declined payment instrument.",
        requires_consent=False,
    )
    with patch(
        "app.services.decision_engine.analyze_failure_context",
        new=AsyncMock(return_value=mock_decision),
    ):
        await process_payment_event(ev.id, _session_factory=factory)

    async with factory() as session:
        log_result = await session.execute(
            select(InterventionAuditLog).where(InterventionAuditLog.payment_event_id == ev.id)
        )
        log = log_result.scalar_one()

    assert log.executed_strategy == "INTERVENTION_DISPATCHED"
    meta = json.loads(log.metadata_json)
    assert meta["outreach_action"] == "WHATSAPP_NUDGE_SENT"
    assert meta["outreach_channel"] == "WHATSAPP"
    assert meta["guardrail_validated_strategy"] == "SECURE_PAYMENT_LINK"


async def test_guardrail_blocks_downgrade_for_low_ticket_in_pipeline() -> None:
    """
    Integration: Low-ticket insufficient_funds agent recommends ADAPTIVE_DOWNGRADE_OFFER,
    but guardrail must override to SILENT_MANDATE_RETRY (Phase 8B Rule 1b).
    The workflow.strategy must reflect the guardrail-validated decision.
    """
    eng, factory = _make_test_factory()
    await _bootstrap(eng)

    # Amount < ₹500 triggers Rule 1b in guardrail
    ev = _event(
        error_code="BAD_REQUEST_ERROR",
        error_reason="insufficient_funds",
        amount=299.0,  # Agent recommends downgrade, guardrail will block
    )
    async with factory() as session:
        session.add(ev)
        await session.commit()

    mock_decision = AgentDecision(
        recommended_strategy="ADAPTIVE_DOWNGRADE_OFFER",
        confidence_score=0.85,
        reasoning="[AGENT] Insufficient funds — downgrade offer recommended.",
        requires_consent=True,
    )
    with patch(
        "app.services.decision_engine.analyze_failure_context",
        new=AsyncMock(return_value=mock_decision),
    ):
        await process_payment_event(ev.id, _session_factory=factory)

    async with factory() as session:
        wf_result = await session.execute(
            select(RecoveryWorkflow).where(RecoveryWorkflow.payment_event_id == ev.id)
        )
        wf = wf_result.scalar_one()

    # Guardrail must have overridden ADAPTIVE_DOWNGRADE_OFFER → SILENT_MANDATE_RETRY
    assert wf.strategy == RecoveryStrategy.SILENT_MANDATE_RETRY, (
        "Guardrail should override downgrade to SILENT_MANDATE_RETRY for amount < ₹500."
    )

    # Audit log must document the override
    async with factory() as session:
        log_result = await session.execute(
            select(InterventionAuditLog).where(InterventionAuditLog.payment_event_id == ev.id)
        )
        log = log_result.scalar_one()

    assert log.guardrail_decision == "OVERRIDDEN"
    assert log.ai_recommended_strategy == "ADAPTIVE_DOWNGRADE_OFFER"

    meta = json.loads(log.metadata_json)
    # Guardrail overrode to silent retry
    assert meta["guardrail_validated_strategy"] == "SILENT_MANDATE_RETRY"
    assert meta["guardrail_overridden"] is True


async def test_insufficient_funds_high_ticket_creates_downgrade_workflow() -> None:
    """
    High-ticket insufficient_funds → INSUFFICIENT_FUNDS_ADAPTIVE + ADAPTIVE_DOWNGRADE_OFFER.
    Agent decision is mocked to isolate pipeline/guardrail logic from API key requirement.
    """
    eng, factory = _make_test_factory()
    await _bootstrap(eng)

    ev = _event(
        error_code="BAD_REQUEST_ERROR",
        error_reason="insufficient_funds",
        amount=1500.0,   # > ₹500 high-ticket
    )
    async with factory() as session:
        session.add(ev)
        await session.commit()

    mock_decision = AgentDecision(
        recommended_strategy="ADAPTIVE_DOWNGRADE_OFFER",
        confidence_score=0.85,
        reasoning="[AGENT:HEURISTIC] Insufficient funds — downgrade offer recommended.",
        requires_consent=True,   # consent=True avoids Rule 1a
    )
    with patch(
        "app.services.decision_engine.analyze_failure_context",
        new=AsyncMock(return_value=mock_decision),
    ):
        await process_payment_event(ev.id, _session_factory=factory)

    async with factory() as session:
        wf_result = await session.execute(
            select(RecoveryWorkflow).where(RecoveryWorkflow.payment_event_id == ev.id)
        )
        wf = wf_result.scalar_one()

    assert wf.diagnosed_cause == DiagnosedCause.INSUFFICIENT_FUNDS_ADAPTIVE
    assert wf.strategy        == RecoveryStrategy.ADAPTIVE_DOWNGRADE_OFFER
    assert wf.max_steps       == 4  # high-ticket gets an extra step


async def test_escalate_to_human_transitions_to_escalated_stopped() -> None:
    """
    Pipeline test: When agent produces ESCALATE_TO_HUMAN:
    - workflow is created with strategy ESCALATE_TO_HUMAN and is_active=False.
    - event status is set to ESCALATED_STOPPED.
    - event status is NOT set to INTERVENTION_ACTIVE.
    - audit log is written with channel='HUMAN' and executed_strategy='INTERVENTION_DISPATCHED'.
    """
    eng, factory = _make_test_factory()
    await _bootstrap(eng)

    ev = _event(error_code="FRAUD_RISK", error_reason="manual_review_needed", amount=15000.0)
    async with factory() as session:
        session.add(ev)
        await session.commit()

    mock_decision = AgentDecision(
        recommended_strategy="ESCALATE_TO_HUMAN",
        confidence_score=0.98,
        reasoning="[AGENT] High-risk anomaly detected — escalating to human.",
        requires_consent=False,
    )
    with patch(
        "app.services.decision_engine.analyze_failure_context",
        new=AsyncMock(return_value=mock_decision),
    ):
        await process_payment_event(ev.id, _session_factory=factory)

    async with factory() as session:
        fetched_ev = await session.get(PaymentEvent, ev.id)
        wf_result = await session.execute(
            select(RecoveryWorkflow).where(RecoveryWorkflow.payment_event_id == ev.id)
        )
        wf = wf_result.scalar_one()
        log_result = await session.execute(
            select(InterventionAuditLog).where(InterventionAuditLog.payment_event_id == ev.id)
        )
        log = log_result.scalar_one()

    assert fetched_ev is not None
    assert fetched_ev.status == PaymentStatus.ESCALATED_STOPPED
    assert fetched_ev.status != PaymentStatus.INTERVENTION_ACTIVE
    assert wf.strategy == RecoveryStrategy.ESCALATE_TO_HUMAN
    assert wf.is_active is False
    assert wf.resolved_at is not None
    assert log.channel == "HUMAN"


async def test_default_fallback_routing() -> None:
    """Unknown error → MANDATE_DECLINED + SECURE_PAYMENT_LINK."""
    eng, factory = _make_test_factory()
    await _bootstrap(eng)

    ev = _event(error_code="WEIRD_CODE", error_reason="mystery_reason", amount=399.0)
    async with factory() as session:
        session.add(ev)
        await session.commit()

    mock_decision = AgentDecision(
        recommended_strategy="SECURE_PAYMENT_LINK",
        confidence_score=0.62,
        reasoning="[AGENT:HEURISTIC] Defaulting to secure payment link.",
        requires_consent=False,
    )
    with patch(
        "app.services.decision_engine.analyze_failure_context",
        new=AsyncMock(return_value=mock_decision),
    ):
        await process_payment_event(ev.id, _session_factory=factory)

    async with factory() as session:
        wf_result = await session.execute(
            select(RecoveryWorkflow).where(RecoveryWorkflow.payment_event_id == ev.id)
        )
        wf = wf_result.scalar_one()

    assert wf.diagnosed_cause == DiagnosedCause.MANDATE_DECLINED
    assert wf.strategy        == RecoveryStrategy.SECURE_PAYMENT_LINK


async def test_workflow_initial_state() -> None:
    """Newly created workflow must have correct default state values."""
    eng, factory = _make_test_factory()
    await _bootstrap(eng)

    ev = _event(error_code="GATEWAY_ERROR", error_reason="bank_offline", amount=299.0)
    async with factory() as session:
        session.add(ev)
        await session.commit()

    mock_decision = AgentDecision(
        recommended_strategy="SILENT_MANDATE_RETRY",
        confidence_score=0.93,
        reasoning="[AGENT:HEURISTIC] Gateway error.",
        requires_consent=False,
    )
    with patch(
        "app.services.decision_engine.analyze_failure_context",
        new=AsyncMock(return_value=mock_decision),
    ):
        await process_payment_event(ev.id, _session_factory=factory)

    async with factory() as session:
        wf_result = await session.execute(
            select(RecoveryWorkflow).where(RecoveryWorkflow.payment_event_id == ev.id)
        )
        wf = wf_result.scalar_one()

    assert wf.current_step      == 1
    assert wf.retry_count       == 0
    assert wf.is_active         is True
    assert wf.resolved_at       is None
    assert wf.amount_recovered  == 0.0

