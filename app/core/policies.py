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

        Rules evaluated in order (first match wins for overriding rules):

        Rule 1a — Consent violation gate:
            If the strategy requires customer consent (ADAPTIVE_DOWNGRADE_OFFER /
            UPI_AUTOPAY_MIGRATION) but requires_consent=False, redirect to
            SECURE_PAYMENT_LINK (BLOCKED_CONSENT_VIOLATION).
            Rationale: Regulatory compliance — strategy cannot execute without consent.

        Rule 1b — Ticket-size gate for downgrade offers:
            If recommended_strategy is ADAPTIVE_DOWNGRADE_OFFER but
            event.amount < ₹500, override to SILENT_MANDATE_RETRY (BLOCKED_AMOUNT_LIMIT).
            Rationale: Downgrade economics don't work below this threshold.

        Rule 2 — Max interventions hard stop:
            If workflow.intervention_count >= 2, immediately force
            ESCALATE_TO_HUMAN (BLOCKED_MAX_ATTEMPTS).
            Rationale: Prevents indefinite looping and customer harassment.

        Rule 3 — Max retries hard stop (legacy):
            If workflow.retry_count >= MAX_RETRIES, also force ESCALATE_TO_HUMAN.
            Rationale: Backward compatibility with retry-based tracking.

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

        # ── Rule 1a: Consent violation → SECURE_PAYMENT_LINK ─────────────────
        # If a consent-required strategy is recommended WITHOUT explicit consent,
        # block the strategy and redirect to SECURE_PAYMENT_LINK.
        # Tag: BLOCKED_CONSENT_VIOLATION
        strategy_str = getattr(strategy, "value", str(strategy))
        consent_required_names = {s.value if hasattr(s, "value") else str(s) for s in _CONSENT_REQUIRED_STRATEGIES}
        if strategy_str in consent_required_names and not consent:
            original_consent_strat = strategy_str
            strategy = RecoveryStrategy.SECURE_PAYMENT_LINK.value
            strategy_str = strategy
            reasoning = (
                f"[GUARDRAIL OVERRIDE] Consent violation blocked: strategy '{original_consent_strat}' "
                f"requires explicit customer consent but requires_consent=False was set by the agent. "
                f"Strategy redirected to 'SECURE_PAYMENT_LINK' (BLOCKED_CONSENT_VIOLATION). "
                f"Original agent reasoning: {decision.reasoning}"
            )
            consent = False
            confidence = max(0.0, confidence - 0.1)
            guardrail_notes.append("RULE1A_BLOCKED_CONSENT_VIOLATION")
            logger.info(
                "Guardrail Rule 1a triggered: consent violation for strategy '%s' on event %s. "
                "Redirected to SECURE_PAYMENT_LINK.",
                original_consent_strat,
                event.id,
            )

        # ── Rule 1b: Ticket-size gate (ADAPTIVE_DOWNGRADE_OFFER < ₹500 → SILENT_MANDATE_RETRY) ──
        if (
            strategy_str == RecoveryStrategy.ADAPTIVE_DOWNGRADE_OFFER.value
            and event.amount < _DOWNGRADE_MIN_AMOUNT_INR
        ):
            original = strategy_str
            strategy = RecoveryStrategy.SILENT_MANDATE_RETRY.value
            strategy_str = strategy
            reasoning = (
                f"[GUARDRAIL OVERRIDE] Blocked by Policy: Ticket size too low for downgrade. "
                f"Event amount \u20b9{event.amount:.2f} < threshold \u20b9{_DOWNGRADE_MIN_AMOUNT_INR:.2f}. "
                f"Agent recommended '{original}' but strategy overridden to "
                f"'{strategy}'. "
                f"Original agent reasoning: {decision.reasoning}"
            )
            confidence = max(0.0, confidence - 0.2)  # Reduce confidence on override
            consent = False  # SILENT_MANDATE_RETRY does not need consent
            guardrail_notes.append("RULE1B_TICKET_SIZE_BLOCK")
            logger.info(
                "Guardrail Rule 1b triggered: Downgrade blocked for event %s "
                "(amount=%.2f). Overridden to SILENT_MANDATE_RETRY.",
                event.id,
                event.amount,
            )

        # ── Rule 4 (Priority 3): Economic Viability Gate ─────────────────────
        from app.services.customer_context import calculate_economics
        expected_amt, cost, net_val = calculate_economics(strategy_str, event.amount)
        if net_val <= 0 and strategy_str != RecoveryStrategy.SILENT_MANDATE_RETRY.value:
            original = strategy_str
            strategy = RecoveryStrategy.SILENT_MANDATE_RETRY.value
            strategy_str = strategy
            reasoning = (
                f"[GUARDRAIL OVERRIDE] Blocked by Unit Economics: Proposed strategy '{original}' "
                f"has non-positive Net Recovery Value (Expected: \u20b9{expected_amt:.2f}, Cost: \u20b9{cost:.2f}, "
                f"Net: \u20b9{net_val:.2f} <= 0). Overridden to SILENT_MANDATE_RETRY (RULE4_NEGATIVE_UNIT_ECONOMICS_BLOCK). "
                f"Original agent reasoning: {decision.reasoning}"
            )
            confidence = max(0.0, confidence - 0.2)
            consent = False
            guardrail_notes.append("RULE4_NEGATIVE_UNIT_ECONOMICS_BLOCK")
            logger.info(
                "Guardrail Rule 4 triggered: Negative unit economics for strategy '%s' on event %s "
                "(amount=%.2f, net=%.2f). Overridden to SILENT_MANDATE_RETRY.",
                original,
                event.id,
                event.amount,
                net_val,
            )

        # ── Rule 2 (Priority 4): Max intervention_count hard stop → ESCALATE_TO_HUMAN ─────
        # Safety beats economics! Must evaluate last.
        intervention_count = getattr(workflow, "intervention_count", 0)
        if intervention_count >= 2:
            strategy = RecoveryStrategy.ESCALATE_TO_HUMAN.value
            reasoning = (
                f"[GUARDRAIL OVERRIDE] Maximum interventions reached: workflow.intervention_count="
                f"{intervention_count} >= 2. "
                "Forcing ESCALATE_TO_HUMAN. Automated recovery exhausted; "
                "escalating to human agent for manual follow-up. "
                f"Original agent reasoning: {decision.reasoning}"
            )
            confidence = 1.0  # Deterministic rule — high certainty
            consent = False
            guardrail_notes.append("RULE2_MAX_INTERVENTIONS_ESCALATE")
            logger.warning(
                "Guardrail Rule 2 triggered: intervention_count=%d >= 2 for event %s. "
                "Forcing ESCALATE_TO_HUMAN.",
                intervention_count,
                event.id,
            )

        # ── Rule 3 (Priority 4): Max retries hard stop (legacy) → ESCALATE_TO_HUMAN ───────
        if workflow.retry_count >= MAX_RETRIES:
            strategy = RecoveryStrategy.ESCALATE_TO_HUMAN.value
            reasoning = (
                f"[GUARDRAIL OVERRIDE] Max retries reached: workflow.retry_count="
                f"{workflow.retry_count} >= MAX_RETRIES={MAX_RETRIES}. "
                "Forcing ESCALATE_TO_HUMAN. "
                f"Original agent reasoning: {decision.reasoning}"
            )
            confidence = 1.0  # Deterministic rule — high certainty
            consent = False
            guardrail_notes.append("RULE3_MAX_RETRIES_ESCALATE")
            logger.warning(
                "Guardrail Rule 3 triggered: Max retries (%d) reached for event %s. "
                "Forcing ESCALATE_TO_HUMAN.",
                MAX_RETRIES,
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

        strat_literal = getattr(strategy, "value", str(strategy))
        return AgentDecision(
            recommended_strategy=strat_literal,  # type: ignore[arg-type]
            confidence_score=confidence,
            reasoning=reasoning,
            requires_consent=consent,
        )

    # Alias for convenience
    validate = validate_agent_decision


# --------------------------------------------------------------------------- #
# Module-level singleton                                                       #
# --------------------------------------------------------------------------- #

guardrail_engine = GuardrailEngine()
"""Module-level singleton for convenience imports."""
