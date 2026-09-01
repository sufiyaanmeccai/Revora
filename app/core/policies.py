"""
app/core/policies.py
---------------------
Deterministic guardrail engine for the Revora Revenue Recovery Engine.

The GuardrailEngine acts as a hard constraint layer that runs *after* the
AI Recovery Agent produces its recommendation. It enforces business rules and
regulatory constraints that the LLM cannot override.

Design principles (Phase 7):
  • Rules are evaluated in priority order; the FIRST blocking rule wins.
  • All overrides are logged in the AgentDecision's reasoning field so the
    audit trail is fully transparent.
  • The engine returns a (possibly mutated) AgentDecision — it never raises.
    The caller is responsible for acting on the validated decision.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.models.orm import PaymentStatus, RecoveryStrategy
from app.models.schemas import AgentDecision

if TYPE_CHECKING:
    from app.models.orm import PaymentEvent, RecoveryWorkflow

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Policy constants                                                             #
# --------------------------------------------------------------------------- #

MAX_RETRIES: int = 3
"""Maximum number of retry attempts before a workflow is force-stopped."""

MAX_WINDOW_HOURS: int = 48
"""Maximum hours a recovery window may remain open before auto-escalation."""

# Strategies that require explicit customer consent
_CONSENT_REQUIRED_STRATEGIES: frozenset[RecoveryStrategy] = frozenset({
    RecoveryStrategy.ADAPTIVE_DOWNGRADE_OFFER,
    RecoveryStrategy.UPI_AUTOPAY_MIGRATION,
})

# Minimum amount (INR) required for a downgrade offer to be viable
_DOWNGRADE_MIN_AMOUNT_INR: float = 500.0


# --------------------------------------------------------------------------- #
# Guardrail Engine                                                             #
# --------------------------------------------------------------------------- #

class GuardrailEngine:
    """
    Deterministic policy validator for AI-generated recovery decisions.

    Usage:
        engine = GuardrailEngine()
        validated = engine.validate_agent_decision(decision, event, workflow)

    The returned AgentDecision may differ from the input if a rule triggered
    an override. The ``reasoning`` field will explain any modifications.
    """

    def validate_agent_decision(
        self,
        decision: AgentDecision,
        event: "PaymentEvent",
        workflow: "RecoveryWorkflow",
    ) -> AgentDecision:
        """
        Validate and potentially override an AI agent's recovery decision.

        Rules evaluated in order (first match wins for blocking rules):

        Rule 1 — Ticket-size gate for downgrade offers:
            If recommended_strategy is ADAPTIVE_DOWNGRADE_OFFER but
            event.amount < 500 INR, override to SECURE_PAYMENT_LINK.
            Rationale: Downgrade economics don't work below this threshold.

        Rule 2 — Max retries hard stop:
            If workflow.retry_count >= MAX_RETRIES, force-transition to
            ESCALATED_STOPPED regardless of agent recommendation.
            Rationale: Prevents indefinite looping and customer harassment.

        Rule 3 — Consent enforcement:
            If the strategy requires customer consent (downgrade / mandate
            migration), ensure requires_consent is explicitly True.
            Rationale: Regulatory compliance — customer must authorise changes.

        Args:
            decision: The AgentDecision produced by the recovery agent.
            event:    The PaymentEvent being processed.
            workflow: The RecoveryWorkflow tracking retry state.

        Returns:
            A (possibly modified) AgentDecision with validated fields.
        """
        strategy   = decision.recommended_strategy
        reasoning  = decision.reasoning
        confidence = decision.confidence_score
        consent    = decision.requires_consent

        guardrail_notes: list[str] = []

        # ── Rule 1: Ticket-size gate ─────────────────────────────────────────
        if (
            strategy == RecoveryStrategy.ADAPTIVE_DOWNGRADE_OFFER
            and event.amount < _DOWNGRADE_MIN_AMOUNT_INR
        ):
            original = strategy.value
            strategy = RecoveryStrategy.SECURE_PAYMENT_LINK
            reasoning = (
                f"[GUARDRAIL OVERRIDE] Blocked by Policy: Ticket size too low for downgrade. "
                f"Event amount ₹{event.amount:.2f} < threshold ₹{_DOWNGRADE_MIN_AMOUNT_INR:.2f}. "
                f"Agent recommended '{original}' but strategy overridden to "
                f"'{strategy.value}'. "
                f"Original agent reasoning: {decision.reasoning}"
            )
            confidence = max(0.0, confidence - 0.2)  # Reduce confidence on override
            guardrail_notes.append("RULE1_TICKET_SIZE_BLOCK")
            logger.info(
                "Guardrail Rule 1 triggered: Downgrade blocked for event %s "
                "(amount=%.2f). Overridden to SECURE_PAYMENT_LINK.",
                event.id,
                event.amount,
            )

        # ── Rule 2: Max retries hard stop ────────────────────────────────────
        if workflow.retry_count >= MAX_RETRIES:
            strategy = RecoveryStrategy.SECURE_PAYMENT_LINK  # marker — caller escalates
            reasoning = (
                f"[GUARDRAIL OVERRIDE] Max retries reached: workflow.retry_count="
                f"{workflow.retry_count} >= MAX_RETRIES={MAX_RETRIES}. "
                f"Forcing transition to ESCALATED_STOPPED. "
                f"Original agent reasoning: {decision.reasoning}"
            )
            confidence = 1.0  # Deterministic rule — high certainty
            consent = False
            guardrail_notes.append("RULE2_MAX_RETRIES_STOP")
            logger.warning(
                "Guardrail Rule 2 triggered: Max retries (%d) reached for event %s. "
                "Workflow will be ESCALATED_STOPPED.",
                MAX_RETRIES,
                event.id,
            )

        # ── Rule 3: Consent enforcement ──────────────────────────────────────
        if strategy in _CONSENT_REQUIRED_STRATEGIES and not consent:
            consent = True
            guardrail_notes.append("RULE3_CONSENT_ENFORCED")
            logger.info(
                "Guardrail Rule 3 triggered: requires_consent set to True for "
                "strategy '%s' on event %s.",
                strategy.value,
                event.id,
            )

        if guardrail_notes:
            logger.info(
                "Guardrail validation complete for event %s: rules=%s",
                event.id,
                guardrail_notes,
            )
        else:
            logger.debug(
                "Guardrail validation passed (no overrides) for event %s.",
                event.id,
            )

        return AgentDecision(
            recommended_strategy=strategy,
            confidence_score=confidence,
            reasoning=reasoning,
            requires_consent=consent,
        )


# --------------------------------------------------------------------------- #
# Module-level singleton                                                       #
# --------------------------------------------------------------------------- #

guardrail_engine = GuardrailEngine()
"""Module-level singleton for convenience imports."""
