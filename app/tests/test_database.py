"""
app/tests/test_database.py
--------------------------
Async pytest tests for the Revora ORM data layer.

Test coverage:
  1. init_db() creates all tables without errors.
  2. PaymentEvent can be created and persisted.
  3. RecoveryWorkflow can be linked to a PaymentEvent.
  4. RecoveryAuditLog can be linked to both a Workflow and a PaymentEvent.
  5. FK relationships resolve correctly on query.

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
    PaymentEvent,
    PaymentStatus,
    RecoveryAuditLog,
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
        is_active=True,
    )
    defaults.update(overrides)
    return RecoveryWorkflow(**defaults)


def _make_audit_log(
    workflow_id: str, payment_event_id: str, **overrides
) -> RecoveryAuditLog:
    """Factory for a valid RecoveryAuditLog instance."""
    defaults = dict(
        id=str(uuid.uuid4()),
        workflow_id=workflow_id,
        payment_event_id=payment_event_id,
        action_type="SILENT_RETRY_SCHEDULED",
        reasoning="Insufficient funds detected; scheduling silent mandate retry.",
        channel="SYSTEM",
        metadata_json='{"retry_window_minutes": 30}',
        timestamp=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return RecoveryAuditLog(**defaults)


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #

async def test_init_db_creates_tables() -> None:
    """init_db equivalent: create_all should succeed without raising."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Verify the tables exist by querying the sqlite_master
    from sqlalchemy import text
    async with TestAsyncSession() as session:
        result = await session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        )
        tables = {row[0] for row in result.fetchall()}

    assert "payment_events"     in tables, "payment_events table missing"
    assert "recovery_workflows" in tables, "recovery_workflows table missing"
    assert "recovery_audit_logs" in tables, "recovery_audit_logs table missing"


async def test_create_and_persist_payment_event() -> None:
    """A PaymentEvent can be inserted and retrieved by primary key."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    event = _make_payment_event()

    async with TestAsyncSession() as session:
        session.add(event)
        await session.commit()

    async with TestAsyncSession() as session:
        fetched = await session.get(PaymentEvent, event.id)

    assert fetched is not None
    assert fetched.id             == event.id
    assert fetched.customer_email == "priya.sharma@example.com"
    assert fetched.amount         == 999.00
    assert fetched.status         == PaymentStatus.AT_RISK
    assert fetched.currency       == "INR"


async def test_create_recovery_workflow_with_foreign_key() -> None:
    """A RecoveryWorkflow linked to a PaymentEvent persists correctly."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    event    = _make_payment_event()
    workflow = _make_workflow(event.id)

    async with TestAsyncSession() as session:
        session.add(event)
        session.add(workflow)
        await session.commit()

    async with TestAsyncSession() as session:
        fetched_wf = await session.get(RecoveryWorkflow, workflow.id)

    assert fetched_wf is not None
    assert fetched_wf.payment_event_id == event.id
    assert fetched_wf.strategy         == RecoveryStrategy.ADAPTIVE_DOWNGRADE_OFFER
    assert fetched_wf.diagnosed_cause  == DiagnosedCause.INSUFFICIENT_FUNDS_ADAPTIVE
    assert fetched_wf.is_active        is True
    assert fetched_wf.max_steps        == 3


async def test_create_audit_log_with_foreign_keys() -> None:
    """A RecoveryAuditLog with valid FK references can be persisted and retrieved."""
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
        fetched_log = await session.get(RecoveryAuditLog, audit_log.id)

    assert fetched_log is not None
    assert fetched_log.workflow_id      == workflow.id
    assert fetched_log.payment_event_id == event.id
    assert fetched_log.action_type      == "SILENT_RETRY_SCHEDULED"
    assert fetched_log.channel          == "SYSTEM"


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


async def test_audit_log_query_by_action_type() -> None:
    """Audit logs can be filtered by action_type."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    event    = _make_payment_event()
    workflow = _make_workflow(event.id)
    log1     = _make_audit_log(workflow.id, event.id, action_type="PAYMENT_LINK_GENERATED")
    log2     = _make_audit_log(workflow.id, event.id, action_type="WHATSAPP_NUDGE_SENT")
    log3     = _make_audit_log(workflow.id, event.id, action_type="PAYMENT_CONFIRMED")

    async with TestAsyncSession() as session:
        session.add_all([event, workflow, log1, log2, log3])
        await session.commit()

    async with TestAsyncSession() as session:
        result = await session.execute(
            select(RecoveryAuditLog).where(
                RecoveryAuditLog.action_type == "WHATSAPP_NUDGE_SENT"
            )
        )
        nudges = result.scalars().all()

    assert len(nudges) == 1
    assert nudges[0].channel == "SYSTEM"
