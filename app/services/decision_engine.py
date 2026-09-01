"""
app/services/decision_engine.py
--------------------------------
Intelligent Decision Engine for the Revora Revenue Recovery Engine.

Phase 7 pipeline (strict, bounded, auditable):
  1. Idempotency Check  — Skip terminal states (RECOVERED, ESCALATED_STOPPED).
  2. Diagnose           — Transition to DIAGNOSED. Run Recovery Agent.
  3. Guard              — Pass AgentDecision through GuardrailEngine.
  4. Execute            — Trigger OutreachService. Transition to INTERVENTION_ACTIVE.
  5. Audit              — Write ONE comprehensive InterventionAuditLog capturing
                          structured AI fields AND the guardrail's validation outcome.

Phase 8A changes:
  • RecoveryAuditLog → InterventionAuditLog (backward-compat alias still works).
  • audit.action_type → audit.executed_strategy.
  • amount_recovered written to RecoveryWorkflow (not PaymentEvent).
  • Structured AI columns: ai_recommended_strategy, ai_confidence, ai_reasoning.
  • guardrail_decision: "APPROVED" | "OVERRIDDEN".

Design principles:
  • The engine is idempotent — re-running for terminal states is a no-op.
  • The GuardrailEngine may override the agent's decision; both the original
    and validated decisions are logged for full traceability.
  • ``process_payment_event`` manages its own DB session so it can safely run
    as a FastAPI BackgroundTask after the originating HTTP session closes.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.recovery_agent import analyze_failure_context
from app.core.policies import MAX_RETRIES, guardrail_engine
from app.models.orm import (
    DiagnosedCause,
    PaymentEvent,
    PaymentStatus,
    InterventionAuditLog,
    RecoveryStrategy,
    RecoveryWorkflow,
)
from app.models.schemas import AgentDecision
from app.services.outreach import OutreachService
from app.services.razorpay_client import RazorpayService

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Terminal states — idempotency check                                          #
# --------------------------------------------------------------------------- #
_TERMINAL_STATES: frozenset[PaymentStatus] = frozenset({
    PaymentStatus.RECOVERED,
    PaymentStatus.ESCALATED_STOPPED,
})

# --------------------------------------------------------------------------- #
# Ticket-size threshold (INR) — kept for backward-compatible _diagnose()      #
# --------------------------------------------------------------------------- #
HIGH_TICKET_THRESHOLD_INR: float = 500.0

# --------------------------------------------------------------------------- #
# Keyword sets used by the legacy rule engine                                  #
# --------------------------------------------------------------------------- #
_NETWORK_CODES = {
    "gateway_error", "gateway_timeout", "upstream_timeout",
    "bank_offline", "timeout", "connection_error", "network_error",
}
_NETWORK_REASONS = {
    "timeout", "bank_offline", "gateway_timeout",
    "upstream_timeout", "network_error",
}
_CARD_CODES = {
    "bad_request_error",
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


# --------------------------------------------------------------------------- #
# Legacy diagnosis helper (preserved for direct unit-test coverage)           #
# --------------------------------------------------------------------------- #
from dataclasses import dataclass


@dataclass(frozen=True)
class DiagnosisResult:
    cause:     DiagnosedCause
    strategy:  RecoveryStrategy
    reasoning: str
    max_steps: int = 3


def _diagnose(
    error_code:   str,
    error_reason: str,
    amount_inr:   float,
) -> DiagnosisResult:
    """
    Apply a priority-ordered rule table to determine the root cause and
    recovery strategy for a failed payment.

    This function is preserved for unit-test compatibility and is used to
    populate the RecoveryWorkflow's diagnosed_cause and initial strategy.
    The Phase 7 agent layer then re-evaluates with richer context.
    """
    code   = (error_code   or "").lower().strip()
    reason = (error_reason or "").lower().strip()

    # ── Rule 1: Network / Gateway failure ────────────────────────────────────
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

    # ── Rule 2: Card / Instrument issues ─────────────────────────────────────
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

    # ── Rule 3: Insufficient funds — ticket-size branching ───────────────────
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

    # ── Rule 5: Default fallback ──────────────────────────────────────────────
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


# --------------------------------------------------------------------------- #
# Core processing function (self-contained session lifecycle)                  #
# --------------------------------------------------------------------------- #

async def process_payment_event(
    event_id: str,
    _session_factory: Optional[Callable[[], async_sessionmaker]] = None,
    _outreach_service: Optional[OutreachService] = None,
) -> None:
    """
    Run the Phase 7 decision engine for a given PaymentEvent ID.

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
    Phase 7 engine logic operating inside an existing AsyncSession.

    Pipeline:
      1. Idempotency check (RECOVERED / ESCALATED_STOPPED → skip)
      2. Transition → DIAGNOSED
      3. Recovery Agent → AgentDecision (simulated LLM structured output)
      4. GuardrailEngine.validate_agent_decision() → validated AgentDecision
      5. Create RecoveryWorkflow
      6. Execute outreach → transition to INTERVENTION_ACTIVE
      7. Write single comprehensive RecoveryAuditLog
      8. Commit atomically
    """
    if outreach_service is None:
        outreach_service = OutreachService()

    razorpay = RazorpayService()

    # ── 1. Fetch PaymentEvent ─────────────────────────────────────────────────
    result = await db.execute(
        select(PaymentEvent).where(PaymentEvent.id == event_id)
    )
    event: Optional[PaymentEvent] = result.scalar_one_or_none()

    if event is None:
        logger.error(
            "Decision engine: PaymentEvent '%s' not found — skipping.", event_id
        )
        return

    # ── 2. Idempotency check ──────────────────────────────────────────────────
    if event.status in _TERMINAL_STATES:
        logger.info(
            "Decision engine: PaymentEvent '%s' is already in terminal state '%s' — skipping.",
            event_id,
            event.status.value,
        )
        return

    if event.status == PaymentStatus.INTERVENTION_ACTIVE:
        logger.info(
            "Decision engine: PaymentEvent '%s' already has INTERVENTION_ACTIVE — skipping.",
            event_id,
        )
        return

    # ── 3. Transition → DIAGNOSED ─────────────────────────────────────────────
    event.status = PaymentStatus.DIAGNOSED
    logger.info("Decision engine: PaymentEvent '%s' → DIAGNOSED", event_id)

    # ── 4. Root-cause diagnosis (deterministic, populates workflow fields) ────
    diagnosis = _diagnose(
        error_code=event.error_code or "",
        error_reason=event.error_reason or "",
        amount_inr=event.amount,
    )

    # ── 5. Recovery Agent → raw AgentDecision ────────────────────────────────
    raw_agent_decision: AgentDecision = await analyze_failure_context(event)

    logger.info(
        "Decision engine: Agent decision for '%s': strategy=%s confidence=%.2f",
        event_id,
        raw_agent_decision.recommended_strategy.value,
        raw_agent_decision.confidence_score,
    )

    # ── 6. GuardrailEngine → validated AgentDecision ─────────────────────────
    # Create a stub workflow to pass retry_count — actual workflow created below
    class _WorkflowStub:
        retry_count = 0

    validated_decision: AgentDecision = guardrail_engine.validate_agent_decision(
        raw_agent_decision,
        event,
        _WorkflowStub(),  # type: ignore[arg-type]
    )

    logger.info(
        "Decision engine: Guardrail-validated decision for '%s': strategy=%s",
        event_id,
        validated_decision.recommended_strategy.value,
    )

    # ── 7. Create RecoveryWorkflow ────────────────────────────────────────────
    workflow = RecoveryWorkflow(
        id=str(uuid.uuid4()),
        payment_event_id=event_id,
        diagnosed_cause=diagnosis.cause,
        strategy=validated_decision.recommended_strategy,
        current_step=1,
        max_steps=diagnosis.max_steps,
        retry_count=0,
        is_active=True,
    )
    db.add(workflow)

    # Flush so workflow.id is available for the audit log FK
    await db.flush()

    # ── 8. Execute outreach strategy ──────────────────────────────────────────
    strategy = validated_decision.recommended_strategy
    outreach_result: Dict[str, Any]
    action_type: str
    channel: str

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

    # ── 9. Transition → INTERVENTION_ACTIVE ──────────────────────────────────
    event.status = PaymentStatus.INTERVENTION_ACTIVE
    logger.info(
        "Decision engine: PaymentEvent '%s' → INTERVENTION_ACTIVE | action=%s",
        event_id,
        action_type,
    )

    # ── 10. Single comprehensive audit log (Phase 8A structured fields) ────────
    guardrail_overridden = (
        raw_agent_decision.recommended_strategy != validated_decision.recommended_strategy
    )
    guardrail_decision_str = "OVERRIDDEN" if guardrail_overridden else "APPROVED"

    audit_metadata = {
        # Workflow context
        "diagnosed_cause":          diagnosis.cause.value,
        "workflow_strategy":        workflow.strategy.value,
        "max_steps":                diagnosis.max_steps,
        # Guardrail layer (summary in metadata for JSON consumers)
        "guardrail_validated_strategy": validated_decision.recommended_strategy.value,
        "guardrail_overridden":         guardrail_overridden,
        "guardrail_requires_consent":   validated_decision.requires_consent,
        # Agent consent flag
        "agent_requires_consent":       raw_agent_decision.requires_consent,
        # Execution layer
        "outreach_action":  action_type,
        "outreach_channel": channel,
        "outreach_status":  outreach_result.get("status"),
        "outreach_result":  outreach_result,
    }

    comprehensive_log = InterventionAuditLog(
        id=str(uuid.uuid4()),
        workflow_id=workflow.id,
        payment_event_id=event_id,
        # Phase 8A: renamed field + structured AI columns
        executed_strategy="INTERVENTION_DISPATCHED",
        ai_recommended_strategy=raw_agent_decision.recommended_strategy.value,
        ai_confidence=raw_agent_decision.confidence_score,
        ai_reasoning=raw_agent_decision.reasoning,
        guardrail_decision=guardrail_decision_str,
        reasoning=(
            f"AGENT REASONING: {raw_agent_decision.reasoning} | "
            f"GUARDRAIL VALIDATION: {validated_decision.reasoning}"
        ),
        channel=channel,
        metadata_json=json.dumps(audit_metadata, default=str),
        timestamp=datetime.now(timezone.utc),
    )
    db.add(comprehensive_log)

    # ── 11. Commit atomically ─────────────────────────────────────────────────
    await db.commit()

    logger.info(
        "Decision engine: Workflow '%s' created for PaymentEvent '%s'. "
        "AT_RISK → DIAGNOSED → INTERVENTION_ACTIVE. Action: %s. "
        "Guardrail override: %s.",
        workflow.id,
        event_id,
        action_type,
        audit_metadata["guardrail_overridden"],
    )
