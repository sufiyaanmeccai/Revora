"""
app/tests/test_decision_engine.py
----------------------------------
Async pytest tests for the Revora Decision Engine.

Coverage:
  1. Gateway error  → TEMPORARY_NETWORK_FAILURE + SILENT_MANDATE_RETRY.
  2. Insufficient funds (high-ticket) → INSUFFICIENT_FUNDS_ADAPTIVE + ADAPTIVE_DOWNGRADE_OFFER.
  3. Insufficient funds (low-ticket)  → INSUFFICIENT_FUNDS_ADAPTIVE + SILENT_MANDATE_RETRY.
  4. Card declined  → EXPIRED_PAYMENT_METHOD + SECURE_PAYMENT_LINK.
  5. Unknown error  → MANDATE_DECLINED + SECURE_PAYMENT_LINK (default fallback).
  6. PaymentEvent.status is updated to IN_RECOVERY after processing.
  7. Audit log is created with WORKFLOW_INITIATED action type.

Strategy:
  • Each test creates an isolated in-memory SQLite DB with its own schema.
  • A PaymentEvent is inserted directly.
  • ``process_payment_event`` is called with the test session factory injected
    via the ``_session_factory`` parameter — no patching of module globals needed.
  • Assertions query the DB directly to verify the expected ORM state.
"""

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
    PaymentEvent,
    PaymentStatus,
    RecoveryAuditLog,
    RecoveryStrategy,
    RecoveryWorkflow,
)
from app.services.decision_engine import _diagnose, process_payment_event

# ---------------------------------------------------------------------------
# Test engine factory — each test gets a fresh in-memory DB
# ---------------------------------------------------------------------------
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
) -> PaymentEvent:
    return PaymentEvent(
        id=str(uuid.uuid4()),
        customer_id="cust_test",
        customer_name="Test User",
        customer_email="test@revora.ai",
        customer_contact="+910000000000",
        amount=amount,
        currency="INR",
        error_code=error_code,
        error_reason=error_reason,
        status=PaymentStatus.AT_RISK,
    )


# ---------------------------------------------------------------------------
# Unit tests on _diagnose() — pure function, no DB needed
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Integration tests — decision engine + DB
# ---------------------------------------------------------------------------

async def test_gateway_error_creates_network_failure_workflow() -> None:
    """Gateway error → TEMPORARY_NETWORK_FAILURE + SILENT_MANDATE_RETRY workflow."""
    eng, factory = _make_test_factory()
    await _bootstrap(eng)

    ev = _event(error_code="GATEWAY_ERROR", error_reason="timeout", amount=499.0)
    async with factory() as session:
        session.add(ev)
        await session.commit()

    await process_payment_event(ev.id, _session_factory=factory)

    async with factory() as session:
        wf_result = await session.execute(
            select(RecoveryWorkflow).where(RecoveryWorkflow.payment_event_id == ev.id)
        )
        workflows = wf_result.scalars().all()

    assert len(workflows) == 1
    wf = workflows[0]
    assert wf.diagnosed_cause == DiagnosedCause.TEMPORARY_NETWORK_FAILURE
    assert wf.strategy        == RecoveryStrategy.SILENT_MANDATE_RETRY
    assert wf.is_active       is True
    assert wf.current_step    == 1


async def test_insufficient_funds_high_ticket_creates_downgrade_workflow() -> None:
    """High-ticket insufficient funds → ADAPTIVE_DOWNGRADE_OFFER."""
    eng, factory = _make_test_factory()
    await _bootstrap(eng)

    ev = _event(error_code="BAD_REQUEST_ERROR", error_reason="insufficient_funds", amount=1999.0)
    async with factory() as session:
        session.add(ev)
        await session.commit()

    await process_payment_event(ev.id, _session_factory=factory)

    async with factory() as session:
        wf_result = await session.execute(
            select(RecoveryWorkflow).where(RecoveryWorkflow.payment_event_id == ev.id)
        )
        wf = wf_result.scalar_one()

    assert wf.diagnosed_cause == DiagnosedCause.INSUFFICIENT_FUNDS_ADAPTIVE
    assert wf.strategy        == RecoveryStrategy.ADAPTIVE_DOWNGRADE_OFFER
    assert wf.max_steps       == 4     # high-ticket gets an extra step


async def test_insufficient_funds_low_ticket_creates_silent_retry_workflow() -> None:
    """Low-ticket insufficient funds → SILENT_MANDATE_RETRY."""
    eng, factory = _make_test_factory()
    await _bootstrap(eng)

    ev = _event(error_code="BAD_REQUEST_ERROR", error_reason="low_balance", amount=99.0)
    async with factory() as session:
        session.add(ev)
        await session.commit()

    await process_payment_event(ev.id, _session_factory=factory)

    async with factory() as session:
        wf_result = await session.execute(
            select(RecoveryWorkflow).where(RecoveryWorkflow.payment_event_id == ev.id)
        )
        wf = wf_result.scalar_one()

    assert wf.diagnosed_cause == DiagnosedCause.INSUFFICIENT_FUNDS_ADAPTIVE
    assert wf.strategy        == RecoveryStrategy.SILENT_MANDATE_RETRY


async def test_payment_event_status_updated_to_in_recovery() -> None:
    """PaymentEvent.status must be IN_RECOVERY after the engine processes it."""
    eng, factory = _make_test_factory()
    await _bootstrap(eng)

    ev = _event(error_code="GATEWAY_ERROR", error_reason="bank_offline", amount=299.0)
    async with factory() as session:
        session.add(ev)
        await session.commit()

    assert ev.status == PaymentStatus.AT_RISK   # precondition

    await process_payment_event(ev.id, _session_factory=factory)

    async with factory() as session:
        fetched = await session.get(PaymentEvent, ev.id)

    assert fetched is not None
    assert fetched.status == PaymentStatus.IN_RECOVERY


async def test_audit_log_created_with_correct_action_type() -> None:
    """A WORKFLOW_INITIATED audit log entry must exist after processing."""
    eng, factory = _make_test_factory()
    await _bootstrap(eng)

    ev = _event(error_code="BAD_REQUEST_ERROR", error_reason="card_declined", amount=499.0)
    async with factory() as session:
        session.add(ev)
        await session.commit()

    await process_payment_event(ev.id, _session_factory=factory)

    async with factory() as session:
        log_result = await session.execute(
            select(RecoveryAuditLog).where(
                RecoveryAuditLog.payment_event_id == ev.id,
                RecoveryAuditLog.action_type == "WORKFLOW_INITIATED",
            )
        )
        logs = log_result.scalars().all()

    assert len(logs) == 1, f"Expected 1 audit log, found {len(logs)}"
    log = logs[0]
    assert log.channel  == "SYSTEM"
    assert log.reasoning is not None
    assert len(log.reasoning) > 10


async def test_default_fallback_routing() -> None:
    """Unknown error → MANDATE_DECLINED + SECURE_PAYMENT_LINK."""
    eng, factory = _make_test_factory()
    await _bootstrap(eng)

    ev = _event(error_code="WEIRD_CODE", error_reason="mystery_reason", amount=399.0)
    async with factory() as session:
        session.add(ev)
        await session.commit()

    await process_payment_event(ev.id, _session_factory=factory)

    async with factory() as session:
        wf_result = await session.execute(
            select(RecoveryWorkflow).where(RecoveryWorkflow.payment_event_id == ev.id)
        )
        wf = wf_result.scalar_one()

    assert wf.diagnosed_cause == DiagnosedCause.MANDATE_DECLINED
    assert wf.strategy        == RecoveryStrategy.SECURE_PAYMENT_LINK


async def test_already_in_recovery_is_skipped() -> None:
    """If a PaymentEvent is already IN_RECOVERY, the engine must be a no-op."""
    eng, factory = _make_test_factory()
    await _bootstrap(eng)

    ev = _event(error_code="GATEWAY_ERROR", error_reason="timeout")
    ev.status = PaymentStatus.IN_RECOVERY   # pre-set
    async with factory() as session:
        session.add(ev)
        await session.commit()

    await process_payment_event(ev.id, _session_factory=factory)

    async with factory() as session:
        wf_result = await session.execute(
            select(RecoveryWorkflow).where(RecoveryWorkflow.payment_event_id == ev.id)
        )
        workflows = wf_result.scalars().all()

    # No workflow should have been created
    assert workflows == [], "Engine should skip events that are already IN_RECOVERY"
