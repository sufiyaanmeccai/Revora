"""
app/agents/recovery_agent.py
-----------------------------
AI Recovery Agent for the Revora Revenue Recovery Engine (Phase 8B).

Uses the official Google GenAI SDK (google-genai) with structured output
to diagnose failed payments and select the optimal recovery strategy.

Design & Fallbacks:
  • Primary path: Uses `genai.Client()` with Gemini (default: gemini-3.7-flash)
    and `types.GenerateContentConfig(response_mime_type="application/json", response_schema=AgentDecision)`
    to enforce strict Pydantic structured output.
  • Safety Fallback: If `GEMINI_API_KEY` is unset or if the API call fails/times out,
    the agent automatically returns a safe deterministic fallback:
      - recommended_strategy = "SECURE_PAYMENT_LINK"
      - confidence_score = 0.0
      - reasoning prefix = "[FALLBACK] ..."
    This guarantees 100% offline resilience for local tests and CI.
  • The agent focuses purely on root-cause strategy recommendation and leaves
    hard policy constraints (e.g. ticket-size checks, retry limits) to the GuardrailEngine.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any, Optional

from pydantic import BaseModel, Field

from app.core.config import settings
from app.models.orm import RecoveryStrategy
from app.models.schemas import AgentDecision, RecoveryStrategyLiteral

if TYPE_CHECKING:
    from app.models.orm import PaymentEvent

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Gemini Structured Output Schema DTO (Strictly No Default Values)           #
# --------------------------------------------------------------------------- #

class _GeminiDecisionSchema(BaseModel):
    """
    Gemini API structured-output DTO without default values.

    The official google-genai SDK strictly prohibits the 'default' keyword
    in OpenAPI response schemas. This DTO enforces that all fields are strictly
    required to satisfy the Gemini schema validator, before being converted back
    into the canonical AgentDecision model.
    """

    recommended_strategy: RecoveryStrategyLiteral = Field(
        description="The recommended recovery strategy."
    )
    confidence_score: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence score from 0.0 to 1.0.",
    )
    reasoning: str = Field(
        description="LLM reasoning chain explaining why this strategy was selected."
    )
    requires_consent: bool = Field(
        description="True if the strategy requires explicit customer consent before execution.",
    )


# Try importing official Google GenAI SDK
try:
    from google import genai
    from google.genai import types
    _GENAI_AVAILABLE = True
except ImportError:  # pragma: no cover
    genai = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]
    _GENAI_AVAILABLE = False

# --------------------------------------------------------------------------- #
# Error code / reason keyword sets for heuristic fallback                     #
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


def _build_analysis_prompt(
    event: "PaymentEvent",
    previous_strategy: Optional[str] = None,
    previous_failure_reason: Optional[str] = None,
    attempt_count: int = 1,
    customer_context: Optional[Any] = None,
) -> str:
    """Construct the PII-free prompt sent to the Gemini AI Agent.

    Only non-PII context is included: payment amount, error code,
    error reason, simulated customer value signals, and multi-attempt context.
    No customer names, emails, phones, card details, or UPI IDs are sent to the LLM.
    """
    multi_attempt_context = ""
    if attempt_count > 1 or previous_strategy:
        multi_attempt_context = (
            f"\nMulti-Attempt Context:\n"
            f"- Attempt Count: {attempt_count}\n"
            f"- Previous Strategy: {previous_strategy or 'N/A'}\n"
            f"- Previous Failure Reason: {previous_failure_reason or 'Customer uncompleted / timeout'}\n"
            f"Note: The previous recovery strategy was unsuccessful. Adapt your strategy recommendation accordingly.\n"
        )

    customer_signals = ""
    if customer_context is not None:
        tier = getattr(customer_context, "value_tier", "STANDARD")
        tenure = getattr(customer_context, "tenure_months", 12)
        customer_signals = (
            f"\nCustomer Value Signals (Simulated Non-PII Context):\n"
            f"- Customer Value Tier: {tier}\n"
            f"- Account Tenure: {tenure} months\n"
            f"- Economic Objective: Maximize Net Recovery Value (Recoverable Amount - Intervention Cost) "
            f"while preserving high-value customer relationships.\n"
        )

    return (
        f"You are the Revora AI Revenue Recovery Agent. Analyze the following failed payment event "
        f"and determine the single best recovery strategy, confidence score (0.0 to 1.0), reasoning, "
        f"and whether customer consent is required.\n\n"
        f"Payment Context (non-PII):\n"
        f"- Amount: ₹{event.amount:.2f} {event.currency}\n"
        f"- Error Code: {event.error_code or 'N/A'}\n"
        f"- Error Reason: {event.error_reason or 'N/A'}\n"
        f"{customer_signals}"
        f"{multi_attempt_context}\n"
        f"Available Recovery Strategies & Channel Cost Assumptions:\n"
        f"1. SILENT_MANDATE_RETRY (Cost: ₹0.00): Best for transient network or bank gateway timeouts where retrying without contacting the customer is preferred.\n"
        f"2. SECURE_PAYMENT_LINK (Cost: ₹2.50): Best for declined/expired cards, abandoned checkouts, or HIGH-tier retention where a direct WhatsApp/SMS link completes full recovery.\n"
        f"3. UPI_AUTOPAY_MIGRATION (Cost: ₹2.50): Best for recurring mandate failures requiring migration to a fresh UPI AutoPay mandate.\n"
        f"4. ADAPTIVE_DOWNGRADE_OFFER (Cost: ₹2.50): Best for price-sensitive customers with insufficient funds where offering a 50% downsell tier prevents total churn.\n"
        f"5. ESCALATE_TO_HUMAN (Cost: ₹150.00): High-cost manual operations intervention — only appropriate when automated paths fail or high-touch handling is required.\n\n"
        f"Provide your decision strictly in the requested JSON format matching the schema."
    )


def _fallback_heuristic_analysis(
    event: "PaymentEvent",
    previous_strategy: Optional[str] = None,
    attempt_count: int = 1,
    customer_context: Optional[Any] = None,
) -> AgentDecision:
    """
    Deterministic rule-based fallback when Gemini API is unavailable or disabled.
    Ensures 100% offline stability and deterministic testing.
    """
    code = (event.error_code or "").lower().strip()
    reason = (event.error_reason or "").lower().strip()
    val_tier = getattr(customer_context, "value_tier", "STANDARD") if customer_context else "STANDARD"

    # Multi-attempt heuristic adaptation: if previous silent retry failed, adapt to customer link
    if attempt_count > 1 and previous_strategy in ("SILENT_MANDATE_RETRY", RecoveryStrategy.SILENT_MANDATE_RETRY):
        return AgentDecision(
            recommended_strategy="SECURE_PAYMENT_LINK",
            confidence_score=0.88,
            reasoning=(
                f"[AGENT:HEURISTIC] Multi-attempt adaptive routing (attempt {attempt_count}). "
                f"Previous strategy '{previous_strategy}' did not resolve the payment. "
                "Escalating to direct customer outreach via secure payment link."
            ),
            requires_consent=False,
        )

    # Signal 1: Network / Gateway failure
    if code in _NETWORK_CODES or reason in _NETWORK_REASONS:
        return AgentDecision(
            recommended_strategy="SILENT_MANDATE_RETRY",
            confidence_score=0.93,
            reasoning=(
                f"[AGENT:HEURISTIC] Failure pattern is consistent with a transient network disruption. "
                f"Razorpay error_code='{event.error_code}', error_reason='{event.error_reason}'. "
                "High confidence this is a bank/gateway intermittent fault. "
                "Recommended action: silent mandate retry — no customer contact needed."
            ),
            requires_consent=False,
        )

    # Signal 2: High Value Tier / VIP Retention check
    if val_tier == "HIGH" and event.amount >= 10000.0 and reason not in _NETWORK_REASONS:
        return AgentDecision(
            recommended_strategy="SECURE_PAYMENT_LINK",
            confidence_score=0.94,
            reasoning=(
                f"[AGENT:HEURISTIC] HIGH-value customer (Tier={val_tier}) with high-ticket payment (₹{event.amount:.2f}). "
                "Prioritizing full relationship retention via secure payment link over aggressive downsell."
            ),
            requires_consent=False,
        )

    # Signal 3: Card / instrument decline
    if reason in _CARD_REASONS:
        return AgentDecision(
            recommended_strategy="SECURE_PAYMENT_LINK",
            confidence_score=0.90,
            reasoning=(
                f"[AGENT:HEURISTIC] Failure indicates an expired or declined payment instrument. "
                f"error_reason='{event.error_reason}' is a card-level hard decline. "
                "Recommended action: generate a secure Razorpay payment link and send via WhatsApp."
            ),
            requires_consent=False,
        )

    # Signal 4: Insufficient funds
    if reason in _FUNDS_REASONS:
        return AgentDecision(
            recommended_strategy="ADAPTIVE_DOWNGRADE_OFFER",
            confidence_score=0.85,
            reasoning=(
                f"[AGENT:HEURISTIC] Failure indicates insufficient funds (error_reason='{event.error_reason}'). "
                f"Customer amount=₹{event.amount:.2f}. "
                "Recommended action: offer an adaptive plan downgrade to improve conversion probability."
            ),
            requires_consent=True,
        )

    # Signal 5: Checkout abandoned
    if reason in _ABANDONED_REASONS:
        return AgentDecision(
            recommended_strategy="SECURE_PAYMENT_LINK",
            confidence_score=0.88,
            reasoning=(
                f"[AGENT:HEURISTIC] Customer initiated checkout but did not complete payment. "
                f"error_reason='{event.error_reason}'. "
                "Recommended action: send a secure payment link within 30 minutes."
            ),
            requires_consent=False,
        )

    # Signal 6: Mandate / UPI failures
    if reason in _MANDATE_REASONS:
        return AgentDecision(
            recommended_strategy="UPI_AUTOPAY_MIGRATION",
            confidence_score=0.80,
            reasoning=(
                f"[AGENT:HEURISTIC] UPI/mandate failure detected (error_reason='{event.error_reason}'). "
                "Recommended action: migrate customer to a fresh UPI AutoPay mandate."
            ),
            requires_consent=True,
        )

    # Signal 7: Unknown / ambiguous failure
    return AgentDecision(
        recommended_strategy="SECURE_PAYMENT_LINK",
        confidence_score=0.62,
        reasoning=(
            f"[AGENT:HEURISTIC] Failure context is ambiguous or unclassified. "
            f"error_code='{event.error_code}', error_reason='{event.error_reason}'. "
            "Defaulting to secure payment link as the lowest-risk intervention."
        ),
        requires_consent=False,
    )


# --------------------------------------------------------------------------- #
# Production fallback helper (missing key / API error)                         #
# --------------------------------------------------------------------------- #

def _make_system_fallback(reason: str) -> AgentDecision:
    """
    Return a safe, clearly-marked system fallback AgentDecision.

    Used when GEMINI_API_KEY is missing or a live API call fails.
    Uses confidence_score=0.0 to signal no AI reasoning was performed.
    The [FALLBACK] prefix in reasoning allows audit consumers to distinguish
    this from genuine Gemini output.
    """
    return AgentDecision(
        recommended_strategy="SECURE_PAYMENT_LINK",
        confidence_score=0.0,
        reasoning=f"[FALLBACK] {reason}",
        requires_consent=False,
    )


async def analyze_failure_context(
    event: "PaymentEvent",
    _client: Optional[Any] = None,
    previous_strategy: Optional[str] = None,
    previous_failure_reason: Optional[str] = None,
    attempt_count: int = 1,
    customer_context: Optional[Any] = None,
) -> AgentDecision:
    """
    Analyse a failed PaymentEvent and return a structured recovery decision.

    Decision source tagging (visible in audit log ai_reasoning field):
      • Genuine Gemini output: reasoning starts with agent-supplied text.
      • Heuristic (test path): reasoning starts with "[AGENT:HEURISTIC]".
      • System fallback:       reasoning starts with "[FALLBACK]".

    Production fallback behaviour:
      • GEMINI_API_KEY missing → [FALLBACK] SECURE_PAYMENT_LINK, confidence=0.0.
      • API/network/quota/timeout error → [FALLBACK] SECURE_PAYMENT_LINK, confidence=0.0.
      • _client injected (test) + call raises → heuristic (preserves offline test behaviour).

    Args:
        event: The PaymentEvent to analyse.
        _client: Optional pre-configured genai.Client instance (useful for unit tests).
        previous_strategy: Strategy attempted in the previous recovery cycle (if any).
        previous_failure_reason: Reason why previous strategy failed (if any).
        attempt_count: Current attempt sequence number (1-indexed).

    Returns:
        A validated AgentDecision object.
    """
    api_key = settings.GEMINI_API_KEY or os.environ.get("GEMINI_API_KEY", "")
    model_name = getattr(settings, "GEMINI_MODEL", "gemini-3.7-flash")

    # ── Production path: no injected client, check API key ───────────────────
    if _client is None:
        if not api_key:
            # Missing key — return safe system fallback immediately.
            logger.warning(
                "Recovery Agent: GEMINI_API_KEY not set for event %s. "
                "Returning [FALLBACK] SECURE_PAYMENT_LINK (confidence=0.0).",
                event.id,
            )
            return _make_system_fallback("Gemini API key not configured.")

        if not _GENAI_AVAILABLE:
            logger.warning(
                "Recovery Agent: google-genai SDK unavailable for event %s. "
                "Returning [FALLBACK] SECURE_PAYMENT_LINK (confidence=0.0).",
                event.id,
            )
            return _make_system_fallback("google-genai SDK not installed.")

        # API key present + SDK available — attempt live Gemini call.
        try:
            client = genai.Client(api_key=api_key)
            prompt = _build_analysis_prompt(
                event,
                previous_strategy=previous_strategy,
                previous_failure_reason=previous_failure_reason,
                attempt_count=attempt_count,
                customer_context=customer_context,
            )

            logger.info("Recovery Agent: Calling Gemini (%s) for event %s (attempt=%d)", model_name, event.id, attempt_count)

            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=_GeminiDecisionSchema,
                    temperature=0.2,
                ),
            )

            if getattr(response, "parsed", None) is not None and isinstance(response.parsed, _GeminiDecisionSchema):
                parsed_dto: _GeminiDecisionSchema = response.parsed
                decision = AgentDecision(**parsed_dto.model_dump())
                logger.info(
                    "Recovery Agent: Gemini decision for %s -> strategy=%s confidence=%.2f",
                    event.id,
                    decision.recommended_strategy,
                    decision.confidence_score,
                )
                return decision

            if hasattr(response, "text") and response.text:
                raw_text = response.text.strip()
                if raw_text.startswith("```"):
                    raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
                    raw_text = re.sub(r"\s*```$", "", raw_text)
                parsed_dto = _GeminiDecisionSchema.model_validate_json(raw_text)
                decision = AgentDecision(**parsed_dto.model_dump())
                logger.info(
                    "Recovery Agent: Gemini decision for %s -> strategy=%s confidence=%.2f",
                    event.id,
                    decision.recommended_strategy,
                    decision.confidence_score,
                )
                return decision
            return _make_system_fallback("Gemini returned an empty response.")

        except Exception as exc:
            logger.warning(
                "Recovery Agent: Gemini API error for event %s (%s). "
                "Returning [FALLBACK] SECURE_PAYMENT_LINK (confidence=0.0).",
                event.id,
                exc,
            )
            return _make_system_fallback(f"Gemini API error: {exc}")

    # ── Test / offline path: injected _client ────────────────────────────────
    # Used by unit tests that supply a mock genai.Client.
    # On API failure here, fall back to deterministic heuristic (not SECURE_PAYMENT_LINK
    # system fallback) to preserve all offline heuristic test coverage.
    try:
        client = _client
        prompt = _build_analysis_prompt(
            event,
            previous_strategy=previous_strategy,
            previous_failure_reason=previous_failure_reason,
            attempt_count=attempt_count,
            customer_context=customer_context,
        )

        logger.info("Recovery Agent: Calling injected client for event %s (attempt=%d)", event.id, attempt_count)

        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_GeminiDecisionSchema,
                temperature=0.2,
            ),
        )

        if getattr(response, "parsed", None) is not None and isinstance(response.parsed, _GeminiDecisionSchema):
            parsed_dto: _GeminiDecisionSchema = response.parsed
            decision = AgentDecision(**parsed_dto.model_dump())
            logger.info(
                "Recovery Agent: Injected client decision for %s -> strategy=%s confidence=%.2f",
                event.id,
                decision.recommended_strategy,
                decision.confidence_score,
            )
            return decision

        if hasattr(response, "text") and response.text:
            raw_text = response.text.strip()
            if raw_text.startswith("```"):
                raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
                raw_text = re.sub(r"\s*```$", "", raw_text)
            parsed_dto = _GeminiDecisionSchema.model_validate_json(raw_text)
            decision = AgentDecision(**parsed_dto.model_dump())
            logger.info(
                "Recovery Agent: Injected client decision for %s -> strategy=%s confidence=%.2f",
                event.id,
                decision.recommended_strategy,
                decision.confidence_score,
            )
            return decision

    except Exception as exc:
        logger.warning(
            "Recovery Agent: Injected client call failed for event %s (%s). "
            "Falling back to deterministic heuristic.",
            event.id,
            exc,
        )

    # Heuristic fallback for test/offline path.
    decision = _fallback_heuristic_analysis(
        event,
        previous_strategy=previous_strategy,
        attempt_count=attempt_count,
        customer_context=customer_context,
    )
    logger.info(
        "Recovery Agent: Heuristic decision for %s -> strategy=%s confidence=%.2f",
        event.id,
        decision.recommended_strategy,
        decision.confidence_score,
    )
    return decision
