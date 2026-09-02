"""
app/services/reconciliation.py
------------------------------
Unified Payment Reconciliation Service for the Revora Revenue Recovery Engine (Phase 8C).

Closes the autonomous recovery loop by:
  1. Matching incoming payment success identifiers (reference_id, payment_event_id,
     razorpay_event_id, razorpay_payment_id, razorpay_order_id) to the underlying
     PaymentEvent and active RecoveryWorkflow (zero PII matching).
  2. Idempotency & Late-Arrival Protection:
     - If the event is already RECOVERED, safely acknowledge without duplicate accounting.
     - If the event is ESCALATED_STOPPED, log the late arrival without overriding terminal status.
  3. Deterministic Accounting:
     - If the executed strategy was ADAPTIVE_DOWNGRADE_OFFER, captures exactly 50% of the
       original invoice amount (original_amount * 0.5), reflecting the discounted plan conversion.
     - For all other strategies (SILENT_MANDATE_RETRY, SECURE_PAYMENT_LINK, UPI_AUTOPAY_MIGRATION),
       captures 100% of the invoice amount (or amount_paid).
  4. Closing the workflow (is_active=False, resolved_at=now, amount_recovered=...) and
     transitioning PaymentEvent.status = PaymentStatus.RECOVERED.
  5. Writing an append-only InterventionAuditLog entry with executed_strategy="PAYMENT_SUCCESS_RECONCILED".

Both the live Razorpay webhook handler and the Outcome Simulator call this unified
service to eliminate parallel accounting logic.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.orm import (
    InterventionAuditLog,
    PaymentEvent,
    PaymentStatus,
    RecoveryStrategy,
    RecoveryWorkflow,
)

logger = logging.getLogger(__name__)


async def reconcile_payment_success(
    payment_identifier: str,
    amount_paid: Optional[float] = None,
    db: Optional[AsyncSession] = None,
    _session_factory: Optional[Callable[[], async_sessionmaker]] = None,
    source: str = "WEBHOOK",
    raw_payload: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Reconcile a successful payment confirmation against an active recovery workflow.

    Args:
        payment_identifier: Non-PII identifier (PaymentEvent.id, razorpay_event_id,
                            razorpay_payment_id, razorpay_order_id, or workflow.id).
        amount_paid:        Amount in INR reported by the gateway (optional).
        db:                 Active AsyncSession. If None, manages its own session lifecycle.
        _session_factory:   Optional session factory override for testing.
        source:             Origin tag (e.g. "WEBHOOK_PAYMENT_LINK", "WEBHOOK_PAYMENT_CAPTURED", "SIMULATION", "DEMO_STUDIO").
        raw_payload:        Raw webhook payload string for auditing.

    Returns:
        A dict with reconciliation results, or None if no matching event was found.
    """
    if db is not None:
        return await _execute_reconciliation(
            db=db,
            payment_identifier=payment_identifier,
            amount_paid=amount_paid,
            source=source,
            raw_payload=raw_payload,
        )

    if _session_factory is None:
        from app.core.database import AsyncSessionLocal as _session_factory  # type: ignore[assignment]

    async with _session_factory() as session:  # type: ignore[operator]
        return await _execute_reconciliation(
            db=session,
            payment_identifier=payment_identifier,
            amount_paid=amount_paid,
            source=source,
            raw_payload=raw_payload,
        )


async def _execute_reconciliation(
    db: AsyncSession,
    payment_identifier: str,
    amount_paid: Optional[float],
    source: str,
    raw_payload: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Internal reconciliation routine operating within a database session."""
    clean_id = (payment_identifier or "").strip()
    if not clean_id:
        logger.warning("Reconciliation called with empty payment_identifier.")
        return None

    # ── 1. Find PaymentEvent by non-PII identifiers ──────────────────────────
    stmt = select(PaymentEvent).where(
        or_(
            PaymentEvent.id == clean_id,
            PaymentEvent.razorpay_event_id == clean_id,
            PaymentEvent.razorpay_payment_id == clean_id,
            PaymentEvent.razorpay_order_id == clean_id,
        )
    )
    result = await db.execute(stmt)
    event: Optional[PaymentEvent] = result.scalar_one_or_none()

    # Fallback: check if the identifier was a workflow.id
    if event is None:
        wf_res = await db.execute(
            select(RecoveryWorkflow).where(RecoveryWorkflow.id == clean_id)
        )
        wf_direct: Optional[RecoveryWorkflow] = wf_res.scalar_one_or_none()
        if wf_direct is not None:
            ev_res = await db.execute(
                select(PaymentEvent).where(PaymentEvent.id == wf_direct.payment_event_id)
            )
            event = ev_res.scalar_one_or_none()

    if event is None:
        logger.warning(
            "Reconciliation: No PaymentEvent found matching identifier %r (source=%s).",
            clean_id,
            source,
        )
        return None

    # ── 2. Idempotency & Terminal State Check ─────────────────────────────────
    now = datetime.now(timezone.utc)

    if event.status == PaymentStatus.RECOVERED:
        logger.info(
            "Reconciliation: PaymentEvent %s is already RECOVERED — idempotent acknowledge (source=%s).",
            event.id,
            source,
        )
        return {
            "status": "already_recovered",
            "event_id": event.id,
            "recovered_amount": event.amount,
            "idempotent": True,
        }

    if event.status == PaymentStatus.ESCALATED_STOPPED:
        logger.info(
            "Reconciliation: PaymentEvent %s is ESCALATED_STOPPED — late-arrival logged (source=%s).",
            event.id,
            source,
        )
        return {
            "status": "escalated_stopped_late_arrival",
            "event_id": event.id,
            "recovered_amount": 0.0,
            "idempotent": True,
        }

    # ── 3. Query Active Workflow for Event ───────────────────────────────────
    wf_stmt = (
        select(RecoveryWorkflow)
        .where(
            RecoveryWorkflow.payment_event_id == event.id,
            RecoveryWorkflow.is_active.is_(True),
        )
        .order_by(RecoveryWorkflow.created_at.desc())
    )
    wf_res = await db.execute(wf_stmt)
    workflow: Optional[RecoveryWorkflow] = wf_res.scalars().first()

    # If no active workflow, try any latest workflow
    if workflow is None:
        wf_any = await db.execute(
            select(RecoveryWorkflow)
            .where(RecoveryWorkflow.payment_event_id == event.id)
            .order_by(RecoveryWorkflow.created_at.desc())
        )
        workflow = wf_any.scalars().first()

    strategy_str = (
        getattr(workflow.strategy, "value", str(workflow.strategy))
        if workflow
        else "UNKNOWN"
    )

    # ── 4. Deterministic Revenue Accounting ───────────────────────────────────
    # Phase 8B/8C: If ADAPTIVE_DOWNGRADE_OFFER was executed, customer accepted 50% discount.
    # Otherwise full invoice amount (or amount_paid if provided) is captured.
    if strategy_str == RecoveryStrategy.ADAPTIVE_DOWNGRADE_OFFER.value or strategy_str == "ADAPTIVE_DOWNGRADE_OFFER":
        amount_recovered = round(event.amount * 0.5, 2)
        downgrade_applied = True
    else:
        amount_recovered = (
            amount_paid if (amount_paid is not None and amount_paid > 0) else event.amount
        )
        downgrade_applied = False

    # ── 5. State Machine Transition & Workflow Close ──────────────────────────
    event.status = PaymentStatus.RECOVERED

    if workflow is not None:
        workflow.amount_recovered = amount_recovered
        workflow.is_active = False
        workflow.resolved_at = now

    # ── 6. Append-Only Audit Log ──────────────────────────────────────────────
    audit_metadata = {
        "reconciliation_source": source,
        "payment_identifier": clean_id,
        "original_amount": event.amount,
        "amount_recovered": amount_recovered,
        "downgrade_applied": downgrade_applied,
        "strategy": strategy_str,
        "amount_paid_reported": amount_paid,
    }

    audit = InterventionAuditLog(
        id=str(uuid.uuid4()),
        workflow_id=workflow.id if workflow else event.id,
        payment_event_id=event.id,
        executed_strategy="PAYMENT_SUCCESS_RECONCILED",
        ai_recommended_strategy=strategy_str,
        ai_confidence=1.0,
        ai_reasoning=f"Payment success confirmed via {source}. Amount captured: ₹{amount_recovered:.2f}.",
        guardrail_decision="APPROVED",
        reasoning=(
            f"Payment success reconciled via {source}. "
            f"Strategy: {strategy_str}. "
            f"Amount captured: ₹{amount_recovered:.2f} "
            f"({'50% downgrade pricing applied' if downgrade_applied else '100% full recovery'}). "
            f"Event transitioned to RECOVERED."
        ),
        channel="SYSTEM",
        metadata_json=json.dumps(audit_metadata, default=str),
        timestamp=now,
    )
    db.add(audit)

    await db.commit()

    logger.info(
        "Reconciliation SUCCESS: event=%s workflow=%s captured=₹%.2f (strategy=%s, source=%s)",
        event.id,
        workflow.id if workflow else "N/A",
        amount_recovered,
        strategy_str,
        source,
    )

    return {
        "status": "success",
        "event_id": event.id,
        "workflow_id": workflow.id if workflow else None,
        "original_amount": event.amount,
        "amount_recovered": amount_recovered,
        "downgrade_applied": downgrade_applied,
        "strategy": strategy_str,
        "source": source,
    }
