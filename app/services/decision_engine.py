"""
app/services/decision_engine.py
--------------------------------
Intelligent Decision Engine for the Revora Revenue Recovery Engine.

This module forms the core of Revora's autonomous recovery logic.

Responsibilities:
  1. Retrieve a failed PaymentEvent by ID.
  2. Diagnose the root cause using a deterministic rule engine that inspects
     Razorpay error codes and error reasons.
  3. Select the optimal recovery strategy based on the diagnosed cause and
     the payment amount (ticket-size segmentation).
  4. Atomically:
       • Transition PaymentEvent.status → IN_RECOVERY.
       • Create a RecoveryWorkflow record.
       • Append a RecoveryAuditLog documenting the reasoning.

Design principles:
  • The engine is intentionally *deterministic* in Phase 3 so the logic is
    fully testable and auditable.  LLM-driven diagnosis is layered on top
    in Phase 4.
  • ``process_payment_event`` manages its own DB session so it can safely run
    as a FastAPI BackgroundTask after the originating HTTP session closes.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.orm import (
    DiagnosedCause,
    PaymentEvent,
    PaymentStatus,
    RecoveryAuditLog,
    RecoveryStrategy,
    RecoveryWorkflow,
)
from app.services.outreach import OutreachService
from app.services.razorpay_client import RazorpayService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ticket-size threshold (INR) for strategy branching on insufficient funds
# ---------------------------------------------------------------------------
HIGH_TICKET_THRESHOLD_INR: float = 500.0

# ---------------------------------------------------------------------------
# Keyword sets used by the rule engine (lower-cased for case-insensitive match)
# ---------------------------------------------------------------------------
_NETWORK_CODES = {
    "gateway_error", "gateway_timeout", "upstream_timeout",
    "bank_offline", "timeout", "connection_error", "network_error",
}
_NETWORK_REASONS = {
    "timeout", "bank_offline", "gateway_timeout",
    "upstream_timeout", "network_error",
}

_CARD_CODES = {
    "bad_request_error",  # frequently used for card-level declines
}
_CARD_REASONS = {
    "invalid_card", "expired_card", "card_declined",
    "card_error", "invalid_instrument", "do_not_honour",
    "card_expired", "invalid_cvv",
}

_FUNDS_REASONS = {
    "insufficient_funds", "low_balance", "credit_limit_reached",
    "credit_limit", "not_sufficient_funds",
}

_ABANDONED_REASONS = {
    "checkout_abandoned", "payment_not_completed", "user_cancelled",
}


# ---------------------------------------------------------------------------
# Diagnosis result dataclass
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DiagnosisResult:
    cause:     DiagnosedCause
    strategy:  RecoveryStrategy
    reasoning: str
    max_steps: int = 3


# ---------------------------------------------------------------------------
# Rule engine
# ---------------------------------------------------------------------------
def _diagnose(
    error_code:   str,
    error_reason: str,
    amount_inr:   float,
) -> DiagnosisResult:
    """
    Apply a priority-ordered rule table to determine the root cause and
    recovery strategy for a failed payment.

    Rules are evaluated top-to-bottom; the first match wins.

    Args:
        error_code:   Razorpay ``error_code`` field (e.g. ``GATEWAY_ERROR``).
        error_reason: Razorpay ``error_reason`` field (e.g. ``timeout``).
        amount_inr:   Payment amount in INR (used for ticket-size branching).

    Returns:
        A frozen ``DiagnosisResult`` with cause, strategy, reasoning, and max_steps.
    """
    code   = (error_code   or "").lower().strip()
    reason = (error_reason or "").lower().strip()

    # ── Rule 1: Network / Gateway failure ───────────────────────────────────
    if code in _NETWORK_CODES or reason in _NETWORK_REASONS:
        return DiagnosisResult(
            cause=DiagnosedCause.TEMPORARY_NETWORK_FAILURE,
            strategy=RecoveryStrategy.SILENT_MANDATE_RETRY,
            reasoning=(
                f"Diagnosed as Temporary Network Failure. "
                f"Razorpay error_code='{error_code}', error_reason='{error_reason}'. "
                "Initiating silent mandate retry — no customer contact needed."
            ),
            max_steps=3,
        )

    # ── Rule 2: Card / Instrument issues ────────────────────────────────────
    if reason in _CARD_REASONS:
        return DiagnosisResult(
            cause=DiagnosedCause.EXPIRED_PAYMENT_METHOD,
            strategy=RecoveryStrategy.SECURE_PAYMENT_LINK,
            reasoning=(
                f"Diagnosed as Expired / Invalid Payment Method. "
                f"error_reason='{error_reason}' indicates a card-level decline. "
                "Generating a secure payment link to collect updated card details."
            ),
            max_steps=3,
        )

    # ── Rule 3: Insufficient funds — ticket-size branching ──────────────────
    if reason in _FUNDS_REASONS:
        if amount_inr >= HIGH_TICKET_THRESHOLD_INR:
            return DiagnosisResult(
                cause=DiagnosedCause.INSUFFICIENT_FUNDS_ADAPTIVE,
                strategy=RecoveryStrategy.ADAPTIVE_DOWNGRADE_OFFER,
                reasoning=(
                    f"Diagnosed as Insufficient Funds (high-ticket ₹{amount_inr:.2f}). "
                    "Offering an adaptive plan downgrade to reduce the immediate charge "
                    "and improve conversion probability."
                ),
                max_steps=4,
            )
        else:
            return DiagnosisResult(
                cause=DiagnosedCause.INSUFFICIENT_FUNDS_ADAPTIVE,
                strategy=RecoveryStrategy.SILENT_MANDATE_RETRY,
                reasoning=(
                    f"Diagnosed as Insufficient Funds (low-ticket ₹{amount_inr:.2f}). "
                    "Scheduling a silent mandate retry — funds likely available shortly."
                ),
                max_steps=3,
            )

    # ── Rule 4: Checkout abandoned / user cancelled ──────────────────────────
    if reason in _ABANDONED_REASONS:
        return DiagnosisResult(
            cause=DiagnosedCause.CHECKOUT_ABANDONED,
            strategy=RecoveryStrategy.SECURE_PAYMENT_LINK,
            reasoning=(
                f"Diagnosed as Checkout Abandoned. "
                f"error_reason='{error_reason}'. "
                "Sending a secure payment link to re-engage the customer."
            ),
            max_steps=2,
        )

    # ── Rule 5: Default fallback ─────────────────────────────────────────────
    return DiagnosisResult(
        cause=DiagnosedCause.MANDATE_DECLINED,
        strategy=RecoveryStrategy.SECURE_PAYMENT_LINK,
        reasoning=(
            f"Could not classify failure with precision "
            f"(error_code='{error_code}', error_reason='{error_reason}'). "
            "Defaulting to mandate-declined classification and sending a "
            "secure payment link for manual re-authorisation."
        ),
        max_steps=3,
    )


# ---------------------------------------------------------------------------
# Core processing function (self-contained session lifecycle)
# ---------------------------------------------------------------------------
async def process_payment_event(
    event_id: str,
    _session_factory: Optional[Callable[[], async_sessionmaker]] = None,
    _outreach_service: Optional[OutreachService] = None,
) -> None:
    """
    Run the decision engine for a given PaymentEvent ID.

    This function is designed to run as a FastAPI ``BackgroundTask``.
    It creates and manages its own database session so it is completely
    independent of the originating HTTP request's session lifecycle.

    Args:
        event_id:          Primary key of the ``PaymentEvent`` to process.
        _session_factory:  Optional override for the session factory (tests).
        _outreach_service: Optional override for OutreachService (tests).
    """
    if _session_factory is None:
        from app.core.database import AsyncSessionLocal as _session_factory  # type: ignore[assignment]

    if _outreach_service is None:
        _outreach_service = OutreachService()

    async with _session_factory() as db:  # type: ignore[operator]
        await _run_engine(db, event_id, _outreach_service)


async def _run_engine(
    db: AsyncSession,
    event_id: str,
    outreach_service: Optional[OutreachService] = None,
) -> None:
    """
    Core engine logic operating inside an existing AsyncSession.

    Separated from ``process_payment_event`` so it can be unit-tested
    directly by injecting a session and outreach service.
    """
    if outreach_service is None:
        outreach_service = OutreachService()

    razorpay = RazorpayService()

    # ── 1. Fetch PaymentEvent ────────────────────────────────────────────────
    result = await db.execute(
        select(PaymentEvent).where(PaymentEvent.id == event_id)
    )
    event: Optional[PaymentEvent] = result.scalar_one_or_none()

    if event is None:
        logger.error(
            "Decision engine: PaymentEvent '%s' not found — skipping.", event_id
        )
        return

    if event.status != PaymentStatus.AT_RISK:
        logger.info(
            "Decision engine: PaymentEvent '%s' is already in status '%s' — skipping.",
            event_id,
            event.status,
        )
        return

    # ── 2. Diagnose root cause ────────────────────────────────────────────────
    diagnosis = _diagnose(
        error_code=event.error_code   or "",
        error_reason=event.error_reason or "",
        amount_inr=event.amount,
    )

    logger.info(
        "Decision engine: PaymentEvent '%s' diagnosed as %s → strategy %s",
        event_id,
        diagnosis.cause.value,
        diagnosis.strategy.value,
    )

    # ── 3. Transition PaymentEvent status ─────────────────────────────────────
    event.status = PaymentStatus.IN_RECOVERY

    # ── 4. Create RecoveryWorkflow ────────────────────────────────────────────
    workflow = RecoveryWorkflow(
        id=str(uuid.uuid4()),
        payment_event_id=event_id,
        diagnosed_cause=diagnosis.cause,
        strategy=diagnosis.strategy,
        current_step=1,
        max_steps=diagnosis.max_steps,
        retry_count=0,
        is_active=True,
    )
    db.add(workflow)

    # Flush so workflow.id is available for the audit log FK
    await db.flush()

    # ── 5. Audit log #1 — WORKFLOW_INITIATED ──────────────────────────────────
    diagnosis_log = RecoveryAuditLog(
        id=str(uuid.uuid4()),
        workflow_id=workflow.id,
        payment_event_id=event_id,
        action_type="WORKFLOW_INITIATED",
        reasoning=diagnosis.reasoning,
        channel="SYSTEM",
        metadata_json=json.dumps({
            "diagnosed_cause": diagnosis.cause.value,
            "strategy":        diagnosis.strategy.value,
            "max_steps":       diagnosis.max_steps,
        }),
        timestamp=datetime.now(timezone.utc),
    )
    db.add(diagnosis_log)

    # ── 6. Execute outreach strategy ──────────────────────────────────────────
    outreach_result: Dict[str, Any]
    action_type:     str
    channel:         str

    strategy = diagnosis.strategy

    if strategy in (RecoveryStrategy.SECURE_PAYMENT_LINK, RecoveryStrategy.UPI_AUTOPAY_MIGRATION):
        link = await razorpay.generate_payment_link(
            amount=event.amount,
            customer_name=event.customer_name,
            customer_email=event.customer_email,
            reference_id=event.id,
        )
        outreach_result = await outreach_service.send_whatsapp_recovery(event, link)
        action_type     = "WHATSAPP_NUDGE_SENT"
        channel         = "WHATSAPP"

    elif strategy == RecoveryStrategy.HINGLISH_VOICE_OUTREACH:
        link = await razorpay.generate_payment_link(
            amount=event.amount,
            customer_name=event.customer_name,
            customer_email=event.customer_email,
            reference_id=event.id,
        )
        outreach_result = await outreach_service.trigger_hinglish_voice(event, link)
        action_type     = "VOICE_CALL_TRIGGERED"
        channel         = "VOICE"

    elif strategy == RecoveryStrategy.ADAPTIVE_DOWNGRADE_OFFER:
        outreach_result = await outreach_service.execute_adaptive_downgrade(event)
        action_type     = "DOWNGRADE_OFFER_SENT"
        channel         = "EMAIL"

    else:
        # SILENT_MANDATE_RETRY — no external outreach
        outreach_result = {"status": "scheduled_with_razorpay", "channel": "SYSTEM"}
        action_type     = "SILENT_RETRY_SCHEDULED"
        channel         = "SYSTEM"

    logger.info(
        "Decision engine: outreach dispatched for PaymentEvent '%s' | "
        "action=%s | status=%s",
        event_id,
        action_type,
        outreach_result.get("status"),
    )

    # ── 7. Audit log #2 — executed outreach action ────────────────────────────
    execution_log = RecoveryAuditLog(
        id=str(uuid.uuid4()),
        workflow_id=workflow.id,
        payment_event_id=event_id,
        action_type=action_type,
        reasoning=f"Strategy {strategy.value} executed. Result: {outreach_result.get('status')}.",
        channel=channel,
        metadata_json=json.dumps(outreach_result, default=str),
        timestamp=datetime.now(timezone.utc),
    )
    db.add(execution_log)

    # ── 8. Commit atomically ──────────────────────────────────────────────────
    await db.commit()

    logger.info(
        "Decision engine: Workflow '%s' created for PaymentEvent '%s'. "
        "Status: AT_RISK → IN_RECOVERY. Action: %s.",
        workflow.id,
        event_id,
        action_type,
    )
