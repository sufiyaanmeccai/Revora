"""
app/tests/test_policies.py
---------------------------
Pytest tests for the Phase 8B GuardrailEngine.

Coverage:
  1. Rule 1a — Consent enforced for ADAPTIVE_DOWNGRADE_OFFER.
  2. Rule 1a — Consent enforced for UPI_AUTOPAY_MIGRATION.
  3. Rule 1b — Downgrade offer blocked (< ₹500) → SILENT_MANDATE_RETRY.
  4. Rule 1b — Downgrade offer permitted for high-ticket (>= ₹500).
  5. Rule 2 — intervention_count >= 2 forces ESCALATE_TO_HUMAN.
  6. Rule 3 — Max retries forces ESCALATE_TO_HUMAN.
  7. Rule 3 — Does NOT trigger below max retries.
  8. Clean passthrough — valid decision, no guardrail triggers.
  9. Rule 1b + Rule 1a interaction.
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


def _make_workflow_with_intervention(intervention_count: int = 0, retry_count: int = 0) -> _WorkflowStub:
    """Create a workflow stub with both retry_count and intervention_count."""
    stub = _WorkflowStub(id=str(uuid.uuid4()), retry_count=retry_count)
    stub.intervention_count = intervention_count  # type: ignore[attr-defined]
    return stub


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #

engine = GuardrailEngine()


def test_rule1_blocks_downgrade_for_low_ticket() -> None:
    """
    Rule 1b: ADAPTIVE_DOWNGRADE_OFFER must be blocked for amounts < ₹500.
    Expected override → SILENT_MANDATE_RETRY (BLOCKED_AMOUNT_LIMIT).

    Note: We pass requires_consent=True to isolate Rule 1b without triggering
    Rule 1a (consent violation), which fires first.
    """
    event    = _make_event(amount=299.0)
    workflow = _make_workflow()
    # Pass consent=True so Rule 1a does NOT fire; Rule 1b triggers on ticket size
    decision = _downgrade_decision(requires_consent=True)

    result = engine.validate_agent_decision(decision, event, workflow)  # type: ignore[arg-type]

    assert result.recommended_strategy == RecoveryStrategy.SILENT_MANDATE_RETRY, (
        "Guardrail should override downgrade to SILENT_MANDATE_RETRY for low-ticket amount."
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
    Rule 3 (legacy): When retry_count >= MAX_RETRIES, guardrail must override to
    ESCALATE_TO_HUMAN regardless of agent recommendation.
    """
    event    = _make_event(amount=999.0)
    workflow = _make_workflow(retry_count=MAX_RETRIES)
    decision = _secure_link_decision()

    result = engine.validate_agent_decision(decision, event, workflow)  # type: ignore[arg-type]

    assert result.recommended_strategy == RecoveryStrategy.ESCALATE_TO_HUMAN, (
        "Guardrail must override to ESCALATE_TO_HUMAN at max retries."
    )
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
    Rule 1a: ADAPTIVE_DOWNGRADE_OFFER without requires_consent=True must be
    blocked and redirected to SECURE_PAYMENT_LINK (BLOCKED_CONSENT_VIOLATION).
    """
    event    = _make_event(amount=2000.0)
    workflow = _make_workflow()
    # Agent forgot to set requires_consent=True
    decision = _downgrade_decision(requires_consent=False)

    result = engine.validate_agent_decision(decision, event, workflow)  # type: ignore[arg-type]

    # Guardrail must have blocked the downgrade and redirected to SECURE_PAYMENT_LINK
    assert result.recommended_strategy == RecoveryStrategy.SECURE_PAYMENT_LINK, (
        "Guardrail must redirect ADAPTIVE_DOWNGRADE_OFFER without consent to SECURE_PAYMENT_LINK."
    )
    assert "BLOCKED_CONSENT_VIOLATION" in result.reasoning or "Consent violation blocked" in result.reasoning, (
        "Guardrail reasoning must mention BLOCKED_CONSENT_VIOLATION."
    )


def test_rule3_enforces_consent_for_upi_migration() -> None:
    """
    Rule 1a: UPI_AUTOPAY_MIGRATION without requires_consent=True must be
    blocked and redirected to SECURE_PAYMENT_LINK (BLOCKED_CONSENT_VIOLATION).
    """
    event    = _make_event(amount=1000.0)
    workflow = _make_workflow()
    decision = _upi_migration_decision()  # requires_consent=False

    result = engine.validate_agent_decision(decision, event, workflow)  # type: ignore[arg-type]

    assert result.recommended_strategy == RecoveryStrategy.SECURE_PAYMENT_LINK, (
        "Guardrail must redirect UPI_AUTOPAY_MIGRATION without consent to SECURE_PAYMENT_LINK."
    )
    assert "BLOCKED_CONSENT_VIOLATION" in result.reasoning or "Consent violation blocked" in result.reasoning


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
    Interaction test: Rule 1a fires first (consent violation for ADAPTIVE_DOWNGRADE_OFFER
    without consent) and redirects to SECURE_PAYMENT_LINK.
    Rule 1b does NOT fire after Rule 1a because the strategy is no longer ADAPTIVE_DOWNGRADE_OFFER.

    With amount=100 (< ₹500) and requires_consent=False:
    - Rule 1a: ADAPTIVE_DOWNGRADE_OFFER + no consent → SECURE_PAYMENT_LINK
    - Rule 1b: strategy is now SECURE_PAYMENT_LINK, not ADAPTIVE_DOWNGRADE_OFFER → skipped
    Result: SECURE_PAYMENT_LINK
    """
    event    = _make_event(amount=100.0)
    workflow = _make_workflow()
    decision = _downgrade_decision(requires_consent=False)

    result = engine.validate_agent_decision(decision, event, workflow)  # type: ignore[arg-type]

    # Rule 1a fires first → SECURE_PAYMENT_LINK (consent violation blocks before ticket-size check)
    assert result.recommended_strategy == RecoveryStrategy.SECURE_PAYMENT_LINK
    assert "BLOCKED_CONSENT_VIOLATION" in result.reasoning or "Consent violation blocked" in result.reasoning
    assert result.requires_consent is False


def test_rule2_intervention_count_forces_escalate_to_human() -> None:
    """
    Rule 2: intervention_count >= 2 must immediately force ESCALATE_TO_HUMAN.
    This is the Phase 8B maximum-attempt escalation rule.
    """
    event    = _make_event(amount=999.0)
    workflow = _make_workflow_with_intervention(intervention_count=2, retry_count=0)
    decision = _secure_link_decision()

    result = engine.validate_agent_decision(decision, event, workflow)  # type: ignore[arg-type]

    assert result.recommended_strategy == RecoveryStrategy.ESCALATE_TO_HUMAN, (
        "Guardrail must force ESCALATE_TO_HUMAN when intervention_count >= 2."
    )
    assert "GUARDRAIL OVERRIDE" in result.reasoning
    assert "intervention_count=2" in result.reasoning or "intervention_count" in result.reasoning
    assert result.confidence_score == 1.0


def test_rule2_intervention_count_exactly_2_triggers() -> None:
    """Boundary: exactly 2 interventions must trigger Rule 2."""
    event    = _make_event(amount=500.0)
    workflow = _make_workflow_with_intervention(intervention_count=2)
    decision = _downgrade_decision(requires_consent=True)

    result = engine.validate_agent_decision(decision, event, workflow)  # type: ignore[arg-type]

    assert result.recommended_strategy == RecoveryStrategy.ESCALATE_TO_HUMAN


def test_rule2_intervention_count_1_does_not_trigger() -> None:
    """intervention_count=1 must NOT trigger Rule 2 escalation."""
    event    = _make_event(amount=999.0)
    workflow = _make_workflow_with_intervention(intervention_count=1, retry_count=0)
    decision = _secure_link_decision()

    result = engine.validate_agent_decision(decision, event, workflow)  # type: ignore[arg-type]

    # Rule 2 should NOT have fired
    assert result.recommended_strategy != RecoveryStrategy.ESCALATE_TO_HUMAN


def test_guardrail_max_retries_exact_boundary() -> None:
    """
    Boundary test: exactly MAX_RETRIES must trigger Rule 3 (legacy retry stop).
    Expected result: ESCALATE_TO_HUMAN.
    """
    event    = _make_event(amount=999.0)
    workflow = _make_workflow(retry_count=MAX_RETRIES)
    decision = _secure_link_decision()

    result = engine.validate_agent_decision(decision, event, workflow)  # type: ignore[arg-type]

    # Rule 3 must have fired
    assert result.recommended_strategy == RecoveryStrategy.ESCALATE_TO_HUMAN
    assert "Max retries" in result.reasoning or "GUARDRAIL OVERRIDE" in result.reasoning


def test_guardrail_max_retries_above_boundary() -> None:
    """
    Above MAX_RETRIES (e.g., MAX_RETRIES + 2) must also trigger Rule 3.
    """
    event    = _make_event(amount=999.0)
    workflow = _make_workflow(retry_count=MAX_RETRIES + 2)
    decision = _secure_link_decision()

    result = engine.validate_agent_decision(decision, event, workflow)  # type: ignore[arg-type]

    assert result.recommended_strategy == RecoveryStrategy.ESCALATE_TO_HUMAN
    assert "GUARDRAIL OVERRIDE" in result.reasoning
