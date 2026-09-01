"""
app/tests/test_database.py
--------------------------
Async pytest tests for the Revora ORM data layer.

Test coverage:
  1. init_db() creates all tables without errors (payment_events, recovery_workflows, intervention_audit_log).
  2. PaymentEvent can be created and persisted with razorpay_event_id.
  3. RecoveryWorkflow can be linked to a PaymentEvent with amount_recovered & intervention_count.
  4. InterventionAuditLog can be linked to both a Workflow and a PaymentEvent with structured AI fields.
  5. FK relationships resolve correctly on query.
  6. Audit log filtering by executed_strategy.

All tests use an isolated in-memory SQLite database so they never touch the
development database file.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import select

from app.models.orm import (
    Base,
    DiagnosedCause,
    InterventionAuditLog,
    PaymentEvent,
    PaymentStatus,
    RecoveryStrategy,
    RecoveryWorkflow,
)

# --------------------------------------------------------------------------- #
# Shared in-memory engine & session factory                                   #
# --------------------------------------------------------------------------- #
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(
    TEST_DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False},
)

TestAsyncSession: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _make_payment_event(**overrides) -> PaymentEvent:
    """Factory for a valid PaymentEvent instance."""
    defaults = dict(
        id=str(uuid.uuid4()),
        razorpay_event_id=f"evt_Test{uuid.uuid4().hex[:8]}",
        razorpay_payment_id="pay_TestABC123",
        razorpay_order_id="order_TestXYZ789",
        razorpay_subscription_id="sub_TestSUB001",
        customer_id="cust_001",
        customer_name="Priya Sharma",
        customer_email="priya.sharma@example.com",
        customer_contact="+919876543210",
        amount=999.00,
        currency="INR",
        error_code="BAD_REQUEST_ERROR",
        error_description="Insufficient funds in the account.",
        error_source="customer",
        error_reason="low_balance",
        status=PaymentStatus.AT_RISK,
        raw_payload='{"event":"payment.failed"}',
    )
    defaults.update(overrides)
    return PaymentEvent(**defaults)


def _make_workflow(payment_event_id: str, **overrides) -> RecoveryWorkflow:
    """Factory for a valid RecoveryWorkflow instance."""
    defaults = dict(
        id=str(uuid.uuid4()),
        payment_event_id=payment_event_id,
        diagnosed_cause=DiagnosedCause.INSUFFICIENT_FUNDS_ADAPTIVE,
        strategy=RecoveryStrategy.ADAPTIVE_DOWNGRADE_OFFER,
        current_step=1,
        max_steps=3,
        retry_count=0,
        intervention_count=0,
        amount_recovered=0.0,
        is_active=True,
    )
    defaults.update(overrides)
    return RecoveryWorkflow(**defaults)


def _make_audit_log(
    workflow_id: str, payment_event_id: str, **overrides
) -> InterventionAuditLog:
    """Factory for a valid InterventionAuditLog instance."""
    defaults = dict(
        id=str(uuid.uuid4()),
        workflow_id=workflow_id,
        payment_event_id=payment_event_id,
        executed_strategy="SILENT_RETRY_SCHEDULED",
        ai_recommended_strategy="SILENT_MANDATE_RETRY",
        ai_confidence=0.95,
        ai_reasoning="Insufficient funds detected; scheduling silent mandate retry.",
        guardrail_decision="APPROVED",
        reasoning="Insufficient funds detected; scheduling silent mandate retry.",
        channel="SYSTEM",
        metadata_json='{"retry_window_minutes": 30}',
        timestamp=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return InterventionAuditLog(**defaults)


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #

async def test_init_db_creates_tables() -> None:
    """init_db equivalent: create_all should succeed without raising."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Verify the tables exist by querying sqlite_master
    from sqlalchemy import text
    async with TestAsyncSession() as session:
        result = await session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        )
        tables = {row[0] for row in result.fetchall()}

    assert "payment_events"          in tables, "payment_events table missing"
    assert "recovery_workflows"      in tables, "recovery_workflows table missing"
    assert "intervention_audit_log"  in tables, "intervention_audit_log table missing"


async def test_create_and_persist_payment_event() -> None:
    """A PaymentEvent can be inserted and retrieved by primary key."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    event = _make_payment_event(razorpay_event_id="evt_TestUnique001")

    async with TestAsyncSession() as session:
        session.add(event)
        await session.commit()

    async with TestAsyncSession() as session:
        fetched = await session.get(PaymentEvent, event.id)

    assert fetched is not None
    assert fetched.id                == event.id
    assert fetched.razorpay_event_id == "evt_TestUnique001"
    assert fetched.customer_email    == "priya.sharma@example.com"
    assert fetched.amount            == 999.00
    assert fetched.status            == PaymentStatus.AT_RISK
    assert fetched.currency          == "INR"


async def test_create_recovery_workflow_with_foreign_key() -> None:
    """A RecoveryWorkflow linked to a PaymentEvent persists correctly with Phase 8A fields."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    event    = _make_payment_event()
    workflow = _make_workflow(event.id, amount_recovered=0.0, intervention_count=1)

    async with TestAsyncSession() as session:
        session.add(event)
        session.add(workflow)
        await session.commit()

    async with TestAsyncSession() as session:
        fetched_wf = await session.get(RecoveryWorkflow, workflow.id)

    assert fetched_wf is not None
    assert fetched_wf.payment_event_id   == event.id
    assert fetched_wf.strategy           == RecoveryStrategy.ADAPTIVE_DOWNGRADE_OFFER
    assert fetched_wf.diagnosed_cause    == DiagnosedCause.INSUFFICIENT_FUNDS_ADAPTIVE
    assert fetched_wf.is_active          is True
    assert fetched_wf.max_steps          == 3
    assert fetched_wf.amount_recovered   == 0.0
    assert fetched_wf.intervention_count == 1


async def test_create_audit_log_with_foreign_keys() -> None:
    """An InterventionAuditLog with valid FK references and AI audit fields persists and is retrieved."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    event     = _make_payment_event()
    workflow  = _make_workflow(event.id)
    audit_log = _make_audit_log(workflow.id, event.id)

    async with TestAsyncSession() as session:
        session.add(event)
        session.add(workflow)
        session.add(audit_log)
        await session.commit()

    async with TestAsyncSession() as session:
        fetched_log = await session.get(InterventionAuditLog, audit_log.id)

    assert fetched_log is not None
    assert fetched_log.workflow_id             == workflow.id
    assert fetched_log.payment_event_id        == event.id
    assert fetched_log.executed_strategy       == "SILENT_RETRY_SCHEDULED"
    assert fetched_log.ai_recommended_strategy == "SILENT_MANDATE_RETRY"
    assert fetched_log.ai_confidence           == pytest.approx(0.95)
    assert fetched_log.guardrail_decision      == "APPROVED"
    assert fetched_log.channel                 == "SYSTEM"


async def test_relationship_query_workflows_for_event() -> None:
    """Querying RecoveryWorkflows by payment_event_id FK returns correct rows."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    event     = _make_payment_event()
    workflow1 = _make_workflow(event.id, strategy=RecoveryStrategy.SECURE_PAYMENT_LINK)
    workflow2 = _make_workflow(event.id, strategy=RecoveryStrategy.SILENT_MANDATE_RETRY)

    async with TestAsyncSession() as session:
        session.add_all([event, workflow1, workflow2])
        await session.commit()

    async with TestAsyncSession() as session:
        result = await session.execute(
            select(RecoveryWorkflow).where(
                RecoveryWorkflow.payment_event_id == event.id
            )
        )
        workflows = result.scalars().all()

    assert len(workflows) == 2
    strategies = {w.strategy for w in workflows}
    assert RecoveryStrategy.SECURE_PAYMENT_LINK   in strategies
    assert RecoveryStrategy.SILENT_MANDATE_RETRY  in strategies


async def test_audit_log_query_by_executed_strategy() -> None:
    """InterventionAuditLog entries can be filtered by executed_strategy."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    event    = _make_payment_event()
    workflow = _make_workflow(event.id)
    log1     = _make_audit_log(workflow.id, event.id, executed_strategy="PAYMENT_LINK_GENERATED")
    log2     = _make_audit_log(workflow.id, event.id, executed_strategy="WHATSAPP_NUDGE_SENT")
    log3     = _make_audit_log(workflow.id, event.id, executed_strategy="PAYMENT_CONFIRMED")

    async with TestAsyncSession() as session:
        session.add_all([event, workflow, log1, log2, log3])
        await session.commit()

    async with TestAsyncSession() as session:
        result = await session.execute(
            select(InterventionAuditLog).where(
                InterventionAuditLog.executed_strategy == "WHATSAPP_NUDGE_SENT"
            )
        )
        nudges = result.scalars().all()

    assert len(nudges) == 1
    assert nudges[0].channel == "SYSTEM"
    assert nudges[0].executed_strategy == "WHATSAPP_NUDGE_SENT"

