"""
app/tests/test_recovery_agent.py
---------------------------------
Unit tests for the Revora AI Recovery Agent (Phase 8B).

Tests both:
  1. Offline deterministic heuristic fallbacks (100% offline, zero external calls).
  2. Mocked Google GenAI (Gemini) client with structured outputs & error handling.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from app.agents.recovery_agent import _fallback_heuristic_analysis, analyze_failure_context
from app.models.schemas import AgentDecision


# --------------------------------------------------------------------------- #
# Helpers — PaymentEvent stub                                                 #
# --------------------------------------------------------------------------- #

@dataclass
class _EventStub:
    id: str
    amount: float = 999.0
    currency: str = "INR"
    customer_id: str = "cust_test_123"
    customer_name: str = "Test User"
    customer_email: str = "test@revora.ai"
    customer_contact: str = "+919876543210"
    error_code: Optional[str] = None
    error_reason: Optional[str] = None
    error_description: Optional[str] = None
    error_source: Optional[str] = None


def _make_event(
    error_code: Optional[str] = None,
    error_reason: Optional[str] = None,
    amount: float = 999.0,
) -> _EventStub:
    return _EventStub(
        id=str(uuid.uuid4()),
        amount=amount,
        error_code=error_code,
        error_reason=error_reason,
    )


# --------------------------------------------------------------------------- #
# Offline Heuristic Fallback Tests                                            #
# These tests call _fallback_heuristic_analysis directly — the function       #
# under test — making them fully offline, deterministic, and API-key-free.    #
# --------------------------------------------------------------------------- #

def test_heuristic_network_timeout() -> None:
    """Network timeouts should recommend SILENT_MANDATE_RETRY without consent."""
    event = _make_event(error_code="GATEWAY_TIMEOUT", error_reason="gateway_timeout")
    decision: AgentDecision = _fallback_heuristic_analysis(event)

    assert decision.recommended_strategy == "SILENT_MANDATE_RETRY"
    assert decision.confidence_score >= 0.90
    assert decision.requires_consent is False
    assert "transient network disruption" in decision.reasoning


def test_heuristic_card_declined() -> None:
    """Card decline should recommend SECURE_PAYMENT_LINK."""
    event = _make_event(error_code="BAD_REQUEST_ERROR", error_reason="card_declined")
    decision: AgentDecision = _fallback_heuristic_analysis(event)

    assert decision.recommended_strategy == "SECURE_PAYMENT_LINK"
    assert decision.confidence_score >= 0.85
    assert decision.requires_consent is False
    assert "expired or declined payment instrument" in decision.reasoning


def test_heuristic_insufficient_funds() -> None:
    """Insufficient funds should recommend ADAPTIVE_DOWNGRADE_OFFER with consent=True."""
    event = _make_event(error_reason="insufficient_funds", amount=1499.0)
    decision: AgentDecision = _fallback_heuristic_analysis(event)

    assert decision.recommended_strategy == "ADAPTIVE_DOWNGRADE_OFFER"
    assert decision.confidence_score >= 0.80
    assert decision.requires_consent is True
    assert "insufficient funds" in decision.reasoning


def test_heuristic_abandoned_checkout() -> None:
    """Abandoned checkout should recommend SECURE_PAYMENT_LINK."""
    event = _make_event(error_reason="checkout_abandoned")
    decision: AgentDecision = _fallback_heuristic_analysis(event)

    assert decision.recommended_strategy == "SECURE_PAYMENT_LINK"
    assert decision.confidence_score >= 0.85
    assert decision.requires_consent is False


def test_heuristic_mandate_declined() -> None:
    """Mandate decline should recommend UPI_AUTOPAY_MIGRATION with consent=True."""
    event = _make_event(error_reason="mandate_declined")
    decision: AgentDecision = _fallback_heuristic_analysis(event)

    assert decision.recommended_strategy == "UPI_AUTOPAY_MIGRATION"
    assert decision.confidence_score >= 0.75
    assert decision.requires_consent is True


def test_heuristic_unknown_error_fallback() -> None:
    """Unknown or unclassified failure contexts default to SECURE_PAYMENT_LINK."""
    event = _make_event(error_code="MYSTERY_CODE", error_reason="unknown_reason")
    decision: AgentDecision = _fallback_heuristic_analysis(event)

    assert decision.recommended_strategy == "SECURE_PAYMENT_LINK"
    assert decision.confidence_score < 0.70
    assert decision.requires_consent is False


# --------------------------------------------------------------------------- #
# Mocked Google GenAI Client Tests                                            #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_gemini_client_mock_success() -> None:
    """Test successful Gemini structured output parsing when client returns valid JSON."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_payload = {
        "recommended_strategy": "ESCALATE_TO_HUMAN",
        "confidence_score": 0.96,
        "reasoning": "Automated recovery exhausted; escalating to human agent for VIP case.",
        "requires_consent": False,
    }
    mock_response.text = json.dumps(mock_payload)
    mock_client.models.generate_content.return_value = mock_response

    event = _make_event(error_reason="user_cancelled", amount=5000.0)
    decision: AgentDecision = await analyze_failure_context(event, _client=mock_client)

    assert decision.recommended_strategy == "ESCALATE_TO_HUMAN"
    assert decision.confidence_score == 0.96
    assert "escalating to human agent" in decision.reasoning
    assert decision.requires_consent is False
    assert mock_client.models.generate_content.called


@pytest.mark.asyncio
async def test_gemini_client_mock_markdown_fences() -> None:
    """Test that markdown code fences around JSON are stripped and parsed correctly."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_payload = {
        "recommended_strategy": "SILENT_MANDATE_RETRY",
        "confidence_score": 0.95,
        "reasoning": "LLM identified transient gateway timeout from payload metadata.",
        "requires_consent": False,
    }
    mock_response.text = f"```json\n{json.dumps(mock_payload)}\n```"
    mock_client.models.generate_content.return_value = mock_response

    event = _make_event(error_code="GATEWAY_ERROR")
    decision: AgentDecision = await analyze_failure_context(event, _client=mock_client)

    assert decision.recommended_strategy == "SILENT_MANDATE_RETRY"
    assert decision.confidence_score == 0.95
    assert decision.requires_consent is False


@pytest.mark.asyncio
async def test_gemini_client_mock_error_falls_back_gracefully() -> None:
    """When Gemini API call throws an exception, agent must catch and fallback to heuristic."""
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = RuntimeError("QuotaExceeded / Network Error")

    event = _make_event(error_code="GATEWAY_TIMEOUT", error_reason="gateway_timeout")
    # Should not raise exception; must return heuristic decision
    decision: AgentDecision = await analyze_failure_context(event, _client=mock_client)

    assert decision.recommended_strategy == "SILENT_MANDATE_RETRY"
    assert decision.confidence_score >= 0.90
    assert "[AGENT:HEURISTIC]" in decision.reasoning


@pytest.mark.asyncio
async def test_missing_api_key_returns_secure_payment_link_fallback() -> None:
    """
    When GEMINI_API_KEY is not set and no client is injected:
    - Production path returns [FALLBACK] SECURE_PAYMENT_LINK, confidence_score=0.0.
    - Reasoning must carry '[FALLBACK]' prefix — NOT fabricated AI output.
    - No heuristic routing occurs; the safe default is always SECURE_PAYMENT_LINK.

    This satisfies the Phase 8B spec: the fallback must be deterministic,
    non-AI, and clearly marked for audit traceability.
    """
    from app.agents import recovery_agent as agent_module

    event = _make_event(error_reason="card_declined", amount=999.0)

    with patch.object(agent_module.settings, "GEMINI_API_KEY", ""), \
         patch.dict("os.environ", {"GEMINI_API_KEY": ""}, clear=False):
        decision: AgentDecision = await analyze_failure_context(event, _client=None)

    assert decision.recommended_strategy == "SECURE_PAYMENT_LINK", (
        "Missing API key must return SECURE_PAYMENT_LINK (safe default)."
    )
    assert decision.confidence_score == 0.0, (
        "Fallback must use confidence_score=0.0 — no AI confidence was computed."
    )
    assert decision.reasoning.startswith("[FALLBACK]"), (
        "Fallback reasoning must carry '[FALLBACK]' prefix for audit traceability."
    )
    assert "not configured" in decision.reasoning or "GEMINI_API_KEY" in decision.reasoning or "API key" in decision.reasoning, (
        "Fallback reasoning must name the root cause (missing API key)."
    )
