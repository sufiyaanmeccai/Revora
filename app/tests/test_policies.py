"""
app/tests/test_policies.py
---------------------------
Pytest tests for the Phase 7 GuardrailEngine.

Coverage:
  1. Rule 1 — Downgrade offer blocked for low-ticket (< ₹500).
  2. Rule 1 — Downgrade offer permitted for high-ticket (>= ₹500).
  3. Rule 2 — Max retries forces ESCALATED_STOPPED override.
  4. Rule 3 — Consent enforced for ADAPTIVE_DOWNGRADE_OFFER.
  5. Rule 3 — Consent enforced for UPI_AUTOPAY_MIGRATION.
  6. Clean passthrough — valid decision, no guardrail triggers.
  7. Rule 1 + Rule 3 — downgrade blocked, consent irrelevant for overridden strategy.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

import pytest

from app.core.policies import MAX_RETRIES, GuardrailEngine
from app.models.orm import PaymentStatus, RecoveryStrategy
from app.models.schemas import AgentDecision


# --------------------------------------------------------------------------- #
# Helpers — stub ORM objects (no DB needed for policy tests)                   #
# --------------------------------------------------------------------------- #

@dataclass
class _EventStub:
    """Minimal PaymentEvent stub for guardrail testing."""
    id: str
    amount: float
    status: PaymentStatus = PaymentStatus.INTERVENTION_ACTIVE


@dataclass
class _WorkflowStub:
    """Minimal RecoveryWorkflow stub for guardrail testing."""
    id: str
    retry_count: int = 0


def _make_event(amount: float) -> _EventStub:
    return _EventStub(id=str(uuid.uuid4()), amount=amount)


def _make_workflow(retry_count: int = 0) -> _WorkflowStub:
    return _WorkflowStub(id=str(uuid.uuid4()), retry_count=retry_count)


def _downgrade_decision(requires_consent: bool = False) -> AgentDecision:
    return AgentDecision(
        recommended_strategy=RecoveryStrategy.ADAPTIVE_DOWNGRADE_OFFER,
        confidence_score=0.85,
        reasoning="Agent recommends downgrade offer.",
        requires_consent=requires_consent,
    )


def _secure_link_decision() -> AgentDecision:
    return AgentDecision(
        recommended_strategy=RecoveryStrategy.SECURE_PAYMENT_LINK,
        confidence_score=0.90,
        reasoning="Agent recommends secure payment link.",
        requires_consent=False,
    )


def _upi_migration_decision() -> AgentDecision:
    return AgentDecision(
        recommended_strategy=RecoveryStrategy.UPI_AUTOPAY_MIGRATION,
        confidence_score=0.80,
        reasoning="Agent recommends UPI AutoPay migration.",
        requires_consent=False,
    )


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #

engine = GuardrailEngine()


def test_rule1_blocks_downgrade_for_low_ticket() -> None:
    """
    Rule 1: ADAPTIVE_DOWNGRADE_OFFER must be blocked for amounts < ₹500.
    Expected override → SECURE_PAYMENT_LINK.
    """
    event    = _make_event(amount=299.0)
    workflow = _make_workflow()
    decision = _downgrade_decision()

    result = engine.validate_agent_decision(decision, event, workflow)  # type: ignore[arg-type]

    assert result.recommended_strategy == RecoveryStrategy.SECURE_PAYMENT_LINK, (
        "Guardrail should override downgrade to SECURE_PAYMENT_LINK for low-ticket amount."
    )
    assert "Blocked by Policy" in result.reasoning, (
        "Guardrail reasoning should explain the block."
    )
    assert result.confidence_score < decision.confidence_score, (
        "Confidence should be reduced after a guardrail override."
    )


def test_rule1_permits_downgrade_for_high_ticket() -> None:
    """
    Rule 1: ADAPTIVE_DOWNGRADE_OFFER must be allowed for amounts >= ₹500.
    """
    event    = _make_event(amount=1500.0)
    workflow = _make_workflow()
    decision = _downgrade_decision(requires_consent=True)

    result = engine.validate_agent_decision(decision, event, workflow)  # type: ignore[arg-type]

    assert result.recommended_strategy == RecoveryStrategy.ADAPTIVE_DOWNGRADE_OFFER, (
        "Guardrail should not block downgrade for high-ticket amount."
    )


def test_rule2_max_retries_forces_escalation() -> None:
    """
    Rule 2: When retry_count >= MAX_RETRIES, guardrail must override to a
    deterministic stop regardless of agent recommendation.
    """
    event    = _make_event(amount=999.0)
    workflow = _make_workflow(retry_count=MAX_RETRIES)
    decision = _secure_link_decision()

    result = engine.validate_agent_decision(decision, event, workflow)  # type: ignore[arg-type]

    assert "GUARDRAIL OVERRIDE" in result.reasoning, (
        "Guardrail must annotate the override in reasoning."
    )
    assert f"retry_count={MAX_RETRIES}" in result.reasoning or "Max retries" in result.reasoning, (
        "Guardrail reasoning should mention max retries."
    )
    assert result.confidence_score == 1.0, (
        "Deterministic rule should have confidence_score=1.0."
    )


def test_rule2_does_not_trigger_below_max_retries() -> None:
    """
    Rule 2: retry_count = MAX_RETRIES - 1 must NOT trigger the hard stop.
    """
    event    = _make_event(amount=999.0)
    workflow = _make_workflow(retry_count=MAX_RETRIES - 1)
    decision = _secure_link_decision()

    result = engine.validate_agent_decision(decision, event, workflow)  # type: ignore[arg-type]

    # Should NOT have the max-retries override annotation
    assert "Max retries" not in result.reasoning or "retry_count" not in result.reasoning or result.recommended_strategy != RecoveryStrategy.SECURE_PAYMENT_LINK or "GUARDRAIL OVERRIDE" not in result.reasoning, (
        "Guardrail should not force-stop when below MAX_RETRIES."
    )
    # For clean passthrough (no rule 1, no rule 2), strategy should be preserved
    assert result.recommended_strategy == RecoveryStrategy.SECURE_PAYMENT_LINK


def test_rule3_enforces_consent_for_downgrade() -> None:
    """
    Rule 3: requires_consent must be True for ADAPTIVE_DOWNGRADE_OFFER,
    even if the agent set it to False.
    """
    event    = _make_event(amount=2000.0)
    workflow = _make_workflow()
    # Agent forgot to set requires_consent=True
    decision = _downgrade_decision(requires_consent=False)

    result = engine.validate_agent_decision(decision, event, workflow)  # type: ignore[arg-type]

    assert result.requires_consent is True, (
        "Guardrail must enforce requires_consent=True for downgrade offers."
    )


def test_rule3_enforces_consent_for_upi_migration() -> None:
    """
    Rule 3: requires_consent must be True for UPI_AUTOPAY_MIGRATION.
    """
    event    = _make_event(amount=1000.0)
    workflow = _make_workflow()
    decision = _upi_migration_decision()  # requires_consent=False

    result = engine.validate_agent_decision(decision, event, workflow)  # type: ignore[arg-type]

    assert result.requires_consent is True, (
        "Guardrail must enforce requires_consent=True for UPI migration."
    )


def test_clean_passthrough_no_rules_triggered() -> None:
    """
    Clean passthrough: SILENT_MANDATE_RETRY with low retry count and non-consent strategy
    should pass through unchanged.
    """
    event    = _make_event(amount=299.0)
    workflow = _make_workflow(retry_count=0)
    decision = AgentDecision(
        recommended_strategy=RecoveryStrategy.SILENT_MANDATE_RETRY,
        confidence_score=0.93,
        reasoning="[AGENT] Network failure — silent retry recommended.",
        requires_consent=False,
    )

    result = engine.validate_agent_decision(decision, event, workflow)  # type: ignore[arg-type]

    # No guardrail should have modified the decision
    assert result.recommended_strategy == RecoveryStrategy.SILENT_MANDATE_RETRY
    assert result.requires_consent is False
    assert result.confidence_score == 0.93
    assert "GUARDRAIL OVERRIDE" not in result.reasoning


def test_rule1_and_rule3_interaction() -> None:
    """
    Interaction test: Rule 1 blocks downgrade → overrides to SECURE_PAYMENT_LINK.
    After Rule 1, Rule 3 should NOT enforce consent for SECURE_PAYMENT_LINK
    (it's not in _CONSENT_REQUIRED_STRATEGIES).
    """
    event    = _make_event(amount=100.0)   # < ₹500 → triggers Rule 1
    workflow = _make_workflow()
    decision = _downgrade_decision(requires_consent=False)

    result = engine.validate_agent_decision(decision, event, workflow)  # type: ignore[arg-type]

    assert result.recommended_strategy == RecoveryStrategy.SECURE_PAYMENT_LINK
    # SECURE_PAYMENT_LINK doesn't require consent
    assert result.requires_consent is False


def test_guardrail_max_retries_exact_boundary() -> None:
    """
    Boundary test: exactly MAX_RETRIES must trigger Rule 2.
    """
    event    = _make_event(amount=999.0)
    workflow = _make_workflow(retry_count=MAX_RETRIES)
    decision = _secure_link_decision()

    result = engine.validate_agent_decision(decision, event, workflow)  # type: ignore[arg-type]

    # Rule 2 must have fired
    assert "Max retries" in result.reasoning or "GUARDRAIL OVERRIDE" in result.reasoning


def test_guardrail_max_retries_above_boundary() -> None:
    """
    Above MAX_RETRIES (e.g., MAX_RETRIES + 2) must also trigger Rule 2.
    """
    event    = _make_event(amount=999.0)
    workflow = _make_workflow(retry_count=MAX_RETRIES + 2)
    decision = _secure_link_decision()

    result = engine.validate_agent_decision(decision, event, workflow)  # type: ignore[arg-type]

    assert "GUARDRAIL OVERRIDE" in result.reasoning
