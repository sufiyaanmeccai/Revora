"""
app/agents/recovery_agent.py
-----------------------------
AI Recovery Agent for the Revora Revenue Recovery Engine (Phase 7).

This module simulates the output of an LLM structured-output framework
(e.g., Instructor with GPT-4o or LangChain with Gemini Pro) without making
live API calls, enabling fully deterministic local testing and demonstration.

Design:
  • The ``analyze_failure_context`` function inspects error_code and
    error_reason fields on the PaymentEvent and returns a populated
    AgentDecision object that faithfully mirrors what a production LLM would
    produce via function-calling / structured output.
  • Each decision includes a confidence_score reflecting how unambiguous the
    failure signal is (e.g., an explicit GATEWAY_ERROR timeout → high
    confidence SILENT_MANDATE_RETRY).
  • The agent intentionally leaves guardrail enforcement to the
    GuardrailEngine — it does NOT self-censor based on amount or retry count.

Production upgrade path:
  Replace the body of ``analyze_failure_context`` with an Instructor call:
    client = instructor.from_openai(AsyncOpenAI())
    return await client.chat.completions.create(
        model="gpt-4o",
        response_model=AgentDecision,
        messages=[{"role": "user", "content": build_prompt(event)}],
    )
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.models.orm import RecoveryStrategy
from app.models.schemas import AgentDecision

if TYPE_CHECKING:
    from app.models.orm import PaymentEvent

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Error code / reason keyword sets (mirrors decision_engine for consistency)  #
# --------------------------------------------------------------------------- #

_NETWORK_CODES = frozenset({
    "gateway_error", "gateway_timeout", "upstream_timeout",
    "bank_offline", "timeout", "connection_error", "network_error",
})
_NETWORK_REASONS = frozenset({
    "timeout", "bank_offline", "gateway_timeout",
    "upstream_timeout", "network_error",
})

_CARD_REASONS = frozenset({
    "invalid_card", "expired_card", "card_declined", "card_error",
    "invalid_instrument", "do_not_honour", "card_expired", "invalid_cvv",
})

_FUNDS_REASONS = frozenset({
    "insufficient_funds", "low_balance", "credit_limit_reached",
    "credit_limit", "not_sufficient_funds",
})

_ABANDONED_REASONS = frozenset({
    "checkout_abandoned", "payment_not_completed", "user_cancelled",
})

_MANDATE_REASONS = frozenset({
    "mandate_declined", "invalid_upi_pin", "upi_failed",
})

# --------------------------------------------------------------------------- #
# Agent                                                                        #
# --------------------------------------------------------------------------- #

async def analyze_failure_context(event: "PaymentEvent") -> AgentDecision:
    """
    Analyse a failed PaymentEvent and return a structured recovery decision.

    Simulates the output of an LLM structured-output framework by applying
    a rule-based heuristic that mirrors real LLM reasoning patterns:
      • Explicit signal (network/card/funds) → high confidence (0.88–0.95).
      • Ambiguous / unknown signal → lower confidence (0.55–0.70).
      • The agent NEVER self-guards on ticket size or retries — that is the
        GuardrailEngine's responsibility.

    Args:
        event: The PaymentEvent to analyse.

    Returns:
        A populated AgentDecision mirroring LLM structured output.
    """
    code   = (event.error_code   or "").lower().strip()
    reason = (event.error_reason or "").lower().strip()

    logger.info(
        "Recovery Agent: analysing event %s | error_code='%s' | error_reason='%s' | amount=%.2f",
        event.id,
        code,
        reason,
        event.amount,
    )

    # ── Signal 1: Network / Gateway failure ───────────────────────────────────
    if code in _NETWORK_CODES or reason in _NETWORK_REASONS:
        decision = AgentDecision(
            recommended_strategy=RecoveryStrategy.SILENT_MANDATE_RETRY,
            confidence_score=0.93,
            reasoning=(
                f"[AGENT] Failure pattern is consistent with a transient network disruption. "
                f"Razorpay error_code='{event.error_code}', error_reason='{event.error_reason}'. "
                "High confidence this is a bank/gateway intermittent fault. "
                "Recommended action: silent mandate retry — no customer contact needed. "
                "Customer experience impact: zero."
            ),
            requires_consent=False,
        )

    # ── Signal 2: Card / instrument decline ──────────────────────────────────
    elif reason in _CARD_REASONS:
        decision = AgentDecision(
            recommended_strategy=RecoveryStrategy.SECURE_PAYMENT_LINK,
            confidence_score=0.90,
            reasoning=(
                f"[AGENT] Failure indicates an expired or declined payment instrument. "
                f"error_reason='{event.error_reason}' is a card-level hard decline. "
                "The mandate cannot be retried silently. "
                "Recommended action: generate a secure Razorpay payment link and send "
                "via WhatsApp to collect updated card details or a new mandate."
            ),
            requires_consent=False,
        )

    # ── Signal 3: Insufficient funds — agent recommends downgrade for all ─────
    #    (GuardrailEngine will block if amount < ₹500)
    elif reason in _FUNDS_REASONS:
        decision = AgentDecision(
            recommended_strategy=RecoveryStrategy.ADAPTIVE_DOWNGRADE_OFFER,
            confidence_score=0.85,
            reasoning=(
                f"[AGENT] Failure indicates insufficient funds (error_reason='{event.error_reason}'). "
                f"Customer amount=₹{event.amount:.2f}. "
                "Recommended action: offer an adaptive plan downgrade to reduce the immediate "
                "charge amount — this maximises conversion probability for price-sensitive customers. "
                "Note: strategy requires customer consent for plan modification."
            ),
            requires_consent=True,
        )

    # ── Signal 4: Checkout abandoned ─────────────────────────────────────────
    elif reason in _ABANDONED_REASONS:
        decision = AgentDecision(
            recommended_strategy=RecoveryStrategy.SECURE_PAYMENT_LINK,
            confidence_score=0.88,
            reasoning=(
                f"[AGENT] Customer initiated checkout but did not complete payment. "
                f"error_reason='{event.error_reason}'. "
                "High purchase intent signal — customer started the flow. "
                "Recommended action: send a secure payment link within 30 minutes "
                "to re-engage while purchase intent is still warm."
            ),
            requires_consent=False,
        )

    # ── Signal 5: Mandate / UPI failures ─────────────────────────────────────
    elif reason in _MANDATE_REASONS:
        decision = AgentDecision(
            recommended_strategy=RecoveryStrategy.UPI_AUTOPAY_MIGRATION,
            confidence_score=0.80,
            reasoning=(
                f"[AGENT] UPI/mandate failure detected (error_reason='{event.error_reason}'). "
                "Existing mandate is invalid or declined. "
                "Recommended action: migrate customer to a fresh UPI AutoPay mandate "
                "to restore recurring charge capability. Requires customer re-authorisation."
            ),
            requires_consent=True,
        )

    # ── Signal 6: Unknown / ambiguous failure ─────────────────────────────────
    else:
        decision = AgentDecision(
            recommended_strategy=RecoveryStrategy.SECURE_PAYMENT_LINK,
            confidence_score=0.62,
            reasoning=(
                f"[AGENT] Failure context is ambiguous or unclassified. "
                f"error_code='{event.error_code}', error_reason='{event.error_reason}'. "
                "Cannot determine root cause with high confidence. "
                "Defaulting to secure payment link as the lowest-risk intervention — "
                "allows the customer to retry via a fresh checkout session."
            ),
            requires_consent=False,
        )

    logger.info(
        "Recovery Agent decision for event %s: strategy=%s confidence=%.2f requires_consent=%s",
        event.id,
        decision.recommended_strategy.value,
        decision.confidence_score,
        decision.requires_consent,
    )

    return decision
