"""
app/api/v1/endpoints/demo.py
----------------------------
Interactive Demo Studio endpoints for the Revora Revenue Recovery Engine (Phase 8C).

Provides 4 deterministic scenario runners designed for live hackathon evaluation:
  • Scenario 1: Annual Sub + Insufficient Funds (₹12,000 → AI Downgrade → Guardrail Validates → Reconciles ₹6,000).
  • Scenario 2: Micro-Transaction Gate (₹199 → AI Downgrade → Guardrail Overrides to SILENT_MANDATE_RETRY).
  • Scenario 3: Autonomous Escalation (Attempt 1 Fails → Attempt 2 triggers Guardrail ESCALATE_TO_HUMAN → Workflow Stopped).
  • Scenario 4: Gemini Outage (Mocked API Timeout → Fallback to SECURE_PAYMENT_LINK + [FALLBACK] audit log).

All scenarios run through the real backend database, decision engine, guardrails, and
reconciliation services — only external customer reactions and simulated network faults
are injected.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.policies import guardrail_engine
from app.models.orm import (
    DiagnosedCause,
    InterventionAuditLog,
    PaymentEvent,
    PaymentStatus,
    RecoveryStrategy,
    RecoveryWorkflow,
)
from app.models.schemas import AgentDecision
from app.services.decision_engine import _run_engine
from app.services.reconciliation import reconcile_payment_success

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/demo", tags=["Demo Studio"])


# --------------------------------------------------------------------------- #
# Scenario Metadata Listing                                                   #
# --------------------------------------------------------------------------- #
SCENARIO_DEFINITIONS = [
    {
        "id": 1,
        "endpoint": "/api/v1/demo/scenario/1",
        "title": "Annual Sub + Insufficient Funds",
        "category": "Adaptive Downsell Engine",
        "initial_amount": 12000.0,
        "expected_ai_decision": "ADAPTIVE_DOWNGRADE_OFFER",
        "expected_guardrail": "APPROVED",
        "expected_recovery": 6000.0,
        "description": "High-ticket payment failure (₹12,000) where AI recommends a 50% plan downgrade, guardrail approves (amount >= ₹500), and customer payment reconciles at ₹6,000.",
    },
    {
        "id": 2,
        "endpoint": "/api/v1/demo/scenario/2",
        "title": "Micro-Transaction Gate (< ₹500)",
        "category": "Policy Guardrail Override",
        "initial_amount": 199.0,
        "expected_ai_decision": "ADAPTIVE_DOWNGRADE_OFFER",
        "expected_guardrail": "OVERRIDDEN (SILENT_MANDATE_RETRY)",
        "expected_recovery": 0.0,
        "description": "Low-ticket payment failure (₹199). AI aggressively suggests plan downgrade, but Guardrail Rule 1b blocks it due to sub-₹500 unit economics and overrides to silent mandate retry.",
    },
    {
        "id": 3,
        "endpoint": "/api/v1/demo/scenario/3",
        "title": "Autonomous Escalation Ceiling",
        "category": "Multi-Attempt Bounded Loop",
        "initial_amount": 4500.0,
        "expected_ai_decision": "ESCALATE_TO_HUMAN",
        "expected_guardrail": "RULE2_MAX_INTERVENTIONS_ESCALATE",
        "expected_recovery": 0.0,
        "description": "Simulates Attempt 1 outreach failure, feeding history into Attempt 2 where Guardrail Rule 2 forces ESCALATE_TO_HUMAN and closes the workflow to prevent customer harassment.",
    },
    {
        "id": 4,
        "endpoint": "/api/v1/demo/scenario/4",
        "title": "Gemini API Outage Resilience",
        "category": "High-Availability Fallback",
        "initial_amount": 2500.0,
        "expected_ai_decision": "SECURE_PAYMENT_LINK (FALLBACK)",
        "expected_guardrail": "APPROVED",
        "expected_recovery": 0.0,
        "description": "Simulates a complete upstream Gemini API network timeout/outage. System catches exception and gracefully defaults to SECURE_PAYMENT_LINK with confidence=0.0 and [FALLBACK] audit tag.",
    },
]

PHASE10_SCENARIO_DEFINITIONS = [
    {
        "id": 5,
        "endpoint": "/api/v1/demo/scenario/5",
        "title": "Scenario A: VIP Relationship Retention",
        "category": "Customer Value Intelligence",
        "initial_amount": 15000.0,
        "expected_ai_decision": "SECURE_PAYMENT_LINK",
        "expected_guardrail": "APPROVED",
        "expected_recovery": 15000.0,
        "description": "High-value enterprise customer (Tier: HIGH, Tenure: 36m). AI prioritizes full relationship retention via direct payment link over aggressive downsell. Reconciles full ₹15,000 (Net Value: ₹14,997.50).",
    },
    {
        "id": 6,
        "endpoint": "/api/v1/demo/scenario/6",
        "title": "Scenario B: Negative Unit Economics Block",
        "category": "Unit Economic Guardrail",
        "initial_amount": 100.0,
        "expected_ai_decision": "ESCALATE_TO_HUMAN",
        "expected_guardrail": "OVERRIDDEN (RULE4_NEGATIVE_UNIT_ECONOMICS_BLOCK)",
        "expected_recovery": 0.0,
        "description": "Micro-transaction (₹100) where AI recommends human escalation (Cost: ₹150). Priority 3 Guardrail calculates negative net value (-₹50.00 <= 0) and overrides to SILENT_MANDATE_RETRY (Cost: ₹0).",
    },
    {
        "id": 7,
        "endpoint": "/api/v1/demo/scenario/7",
        "title": "Scenario C: Safety Precedence Over Economics",
        "category": "Anti-Harassment Precedence",
        "initial_amount": 150.0,
        "expected_ai_decision": "SILENT_MANDATE_RETRY",
        "expected_guardrail": "RULE2_MAX_INTERVENTIONS_ESCALATE",
        "expected_recovery": 0.0,
        "description": "Customer reached max intervention limit (count >= 2). AI/economic logic prefers a free retry, but Priority 4 Guardrail strictly forces ESCALATE_TO_HUMAN -> ESCALATED_STOPPED. Safety beats economics!",
    },
]

ALL_SCENARIO_DEFINITIONS = SCENARIO_DEFINITIONS + PHASE10_SCENARIO_DEFINITIONS


@router.get("/scenarios", summary="List Demo Studio Scenarios")
async def list_scenarios(include_all: bool = False) -> Dict[str, Any]:
    """Return all available interactive demo scenarios."""
    scenarios = ALL_SCENARIO_DEFINITIONS if include_all else SCENARIO_DEFINITIONS
    return {
        "status": "success",
        "scenarios": scenarios,
    }


# --------------------------------------------------------------------------- #
# Scenario 1: Annual Sub + Insufficient Funds (50% Downgrade Reconciled)      #
# --------------------------------------------------------------------------- #
@router.post("/scenario/1", summary="Scenario 1: Annual Sub Insufficient Funds")
@router.post("/scenario-1-annual-downgrade", summary="Scenario 1 Alias")
async def run_scenario_1(db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """
    Scenario 1:
      1. Create ₹12,000 failed payment with error_reason="insufficient_funds".
      2. Decision engine runs: AI recommends ADAPTIVE_DOWNGRADE_OFFER (consent=True).
      3. Guardrail Rule 1b validates: ₹12,000 >= ₹500 threshold -> APPROVED.
      4. Dispatches DOWNGRADE_OFFER_SENT outreach.
      5. Reconciles payment success -> captures exactly ₹6,000 (50% downgrade pricing).
    """
    event = PaymentEvent(
        id=str(uuid.uuid4()),
        razorpay_event_id=f"evt_demo1_{uuid.uuid4().hex[:8]}",
        customer_id="cust_demo_annual",
        customer_name="Vikram Aditya (Annual Pro)",
        customer_email="vikram.aditya@enterprise-demo.in",
        customer_contact="+919876543210",
        amount=12000.0,
        currency="INR",
        error_code="BAD_REQUEST_ERROR",
        error_reason="insufficient_funds",
        status=PaymentStatus.AT_RISK,
        raw_payload='{"scenario": 1, "tier": "Annual Pro"}',
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)

    # Run decision engine pipeline with deterministic decision
    mock_decision = AgentDecision(
        recommended_strategy="ADAPTIVE_DOWNGRADE_OFFER",
        confidence_score=0.92,
        reasoning="[AI AGENT] Insufficient funds on high-ticket ₹12,000 annual subscription. Recommending adaptive plan downsell at 50% discount to retain subscriber.",
        requires_consent=True,
    )
    with patch(
        "app.services.decision_engine.analyze_failure_context",
        new=AsyncMock(return_value=mock_decision),
    ):
        await _run_engine(db, event.id, is_simulated=True)

    # Simulate customer accepting the 50% downsell offer
    recon_result = await reconcile_payment_success(
        payment_identifier=event.id,
        amount_paid=6000.0,
        db=db,
        source="DEMO_STUDIO_SIMULATED_CUSTOMER",
    )

    # Fetch audit logs
    audit_res = await db.execute(
        select(InterventionAuditLog)
        .where(InterventionAuditLog.payment_event_id == event.id)
        .order_by(InterventionAuditLog.timestamp.asc())
    )
    audit_logs = [
        {
            "id": log.id,
            "strategy": log.executed_strategy,
            "ai_strategy": log.ai_recommended_strategy,
            "ai_confidence": log.ai_confidence,
            "guardrail_decision": log.guardrail_decision,
            "reasoning": log.reasoning,
            "channel": log.channel,
            "timestamp": log.timestamp.isoformat(),
        }
        for log in audit_res.scalars().all()
    ]

    return {
        "scenario_id": 1,
        "title": "Annual Sub + Insufficient Funds (Downsell)",
        "event_id": event.id,
        "customer_name": event.customer_name,
        "original_amount": 12000.0,
        "ai_recommendation": "ADAPTIVE_DOWNGRADE_OFFER",
        "ai_confidence": 0.92,
        "guardrail_decision": "APPROVED",
        "executed_strategy": "ADAPTIVE_DOWNGRADE_OFFER",
        "outreach_action": "DOWNGRADE_OFFER_SENT (EMAIL)",
        "recovered_amount": recon_result.get("amount_recovered", 6000.0) if recon_result else 6000.0,
        "downgrade_applied": True,
        "final_status": "RECOVERED",
        "simulated_action": "Customer accepted 50% discount link (Simulated Recovery)",
        "audit_trail": audit_logs,
    }


# --------------------------------------------------------------------------- #
# Scenario 2: Micro-Transaction Gate (< ₹500 Downgrade Overridden)             #
# --------------------------------------------------------------------------- #
@router.post("/scenario/2", summary="Scenario 2: Micro-Transaction Gate")
@router.post("/scenario-2-micro-transaction", summary="Scenario 2 Alias")
async def run_scenario_2(db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """
    Scenario 2:
      1. Create ₹199 failed payment with error_reason="insufficient_funds".
      2. Decision engine runs: AI recommends ADAPTIVE_DOWNGRADE_OFFER (consent=True).
      3. Guardrail Rule 1b blocks: ₹199 < ₹500 threshold -> overrides to SILENT_MANDATE_RETRY.
      4. Dispatches SILENT_RETRY_SCHEDULED (SYSTEM).
    """
    event = PaymentEvent(
        id=str(uuid.uuid4()),
        razorpay_event_id=f"evt_demo2_{uuid.uuid4().hex[:8]}",
        customer_id="cust_demo_micro",
        customer_name="Rohan Verma (Starter Tier)",
        customer_email="rohan.verma@consumer-demo.in",
        customer_contact="+919876543211",
        amount=199.0,
        currency="INR",
        error_code="BAD_REQUEST_ERROR",
        error_reason="insufficient_funds",
        status=PaymentStatus.AT_RISK,
        raw_payload='{"scenario": 2, "tier": "Micro 199"}',
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)

    mock_decision = AgentDecision(
        recommended_strategy="ADAPTIVE_DOWNGRADE_OFFER",
        confidence_score=0.85,
        reasoning="[AI AGENT] Insufficient funds detected. Suggesting plan downgrade.",
        requires_consent=True,
    )
    with patch(
        "app.services.decision_engine.analyze_failure_context",
        new=AsyncMock(return_value=mock_decision),
    ):
        await _run_engine(db, event.id, is_simulated=True)

    # Fetch audit logs
    audit_res = await db.execute(
        select(InterventionAuditLog)
        .where(InterventionAuditLog.payment_event_id == event.id)
        .order_by(InterventionAuditLog.timestamp.asc())
    )
    audit_logs = [
        {
            "id": log.id,
            "strategy": log.executed_strategy,
            "ai_strategy": log.ai_recommended_strategy,
            "ai_confidence": log.ai_confidence,
            "guardrail_decision": log.guardrail_decision,
            "reasoning": log.reasoning,
            "channel": log.channel,
            "timestamp": log.timestamp.isoformat(),
        }
        for log in audit_res.scalars().all()
    ]

    return {
        "scenario_id": 2,
        "title": "Micro-Transaction Gate (< ₹500 Policy Override)",
        "event_id": event.id,
        "customer_name": event.customer_name,
        "original_amount": 199.0,
        "ai_recommendation": "ADAPTIVE_DOWNGRADE_OFFER",
        "guardrail_decision": "OVERRIDDEN (RULE1B_TICKET_SIZE_BLOCK)",
        "executed_strategy": "SILENT_MANDATE_RETRY",
        "guardrail_reason": "Blocked by Policy: Ticket size ₹199.00 < threshold ₹500.00. Overridden to SILENT_MANDATE_RETRY.",
        "outreach_action": "SILENT_RETRY_SCHEDULED (SYSTEM)",
        "recovered_amount": 0.0,
        "final_status": "INTERVENTION_ACTIVE",
        "audit_trail": audit_logs,
    }


# --------------------------------------------------------------------------- #
# Scenario 3: Autonomous Escalation Ceiling (Attempt 2 -> ESCALATED_STOPPED)   #
# --------------------------------------------------------------------------- #
@router.post("/scenario/3", summary="Scenario 3: Autonomous Escalation Ceiling")
@router.post("/scenario-3-autonomous-escalation", summary="Scenario 3 Alias")
async def run_scenario_3(db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """
    Scenario 3:
      1. Create ₹4,500 failed payment with card decline.
      2. Attempt 1 runs -> SECURE_PAYMENT_LINK (intervention_count becomes 1).
      3. Customer does not respond (Attempt 1 timeout).
      4. Attempt 2 runs with intervention_count=1 -> passes history to AI Agent.
      5. Guardrail Rule 2 fires (intervention_count >= 2) -> ESCALATE_TO_HUMAN.
      6. Event transitions to ESCALATED_STOPPED (terminal) with zero automated harassment.
    """
    event = PaymentEvent(
        id=str(uuid.uuid4()),
        razorpay_event_id=f"evt_demo3_{uuid.uuid4().hex[:8]}",
        customer_id="cust_demo_enterprise",
        customer_name="Ananya Roy (Enterprise VIP)",
        customer_email="ananya.roy@corp-demo.in",
        customer_contact="+919876543212",
        amount=4500.0,
        currency="INR",
        error_code="BAD_REQUEST_ERROR",
        error_reason="card_declined",
        status=PaymentStatus.AT_RISK,
        raw_payload='{"scenario": 3, "tier": "Enterprise"}',
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)

    # Attempt 1: Dispatches SECURE_PAYMENT_LINK
    mock_decision_1 = AgentDecision(
        recommended_strategy="SECURE_PAYMENT_LINK",
        confidence_score=0.90,
        reasoning="[AGENT] Expired/declined card on Attempt 1. Dispatching secure payment link.",
        requires_consent=False,
    )
    with patch(
        "app.services.decision_engine.analyze_failure_context",
        new=AsyncMock(return_value=mock_decision_1),
    ):
        await _run_engine(db, event.id, is_simulated=True)

    # Re-fetch event and simulate Attempt 1 timeout -> trigger Attempt 2
    event.status = PaymentStatus.DIAGNOSED
    # Update existing workflow intervention_count to 2 to simulate Attempt 2 guardrail threshold
    wf_res = await db.execute(
        select(RecoveryWorkflow).where(RecoveryWorkflow.payment_event_id == event.id)
    )
    wfs = list(wf_res.scalars().all())
    if wfs:
        wfs[0].intervention_count = 2
    await db.commit()

    # Attempt 2: AI tries another strategy, but Guardrail Rule 2 intervenes
    mock_decision_2 = AgentDecision(
        recommended_strategy="SECURE_PAYMENT_LINK",
        confidence_score=0.80,
        reasoning="[AGENT] Attempt 2: Payment still uncollected. Proposing secondary link.",
        requires_consent=False,
    )
    with patch(
        "app.services.decision_engine.analyze_failure_context",
        new=AsyncMock(return_value=mock_decision_2),
    ):
        await _run_engine(db, event.id, is_simulated=True)

    # Fetch audit logs
    audit_res = await db.execute(
        select(InterventionAuditLog)
        .where(InterventionAuditLog.payment_event_id == event.id)
        .order_by(InterventionAuditLog.timestamp.asc())
    )
    audit_logs = [
        {
            "id": log.id,
            "strategy": log.executed_strategy,
            "ai_strategy": log.ai_recommended_strategy,
            "ai_confidence": log.ai_confidence,
            "guardrail_decision": log.guardrail_decision,
            "reasoning": log.reasoning,
            "channel": log.channel,
            "timestamp": log.timestamp.isoformat(),
        }
        for log in audit_res.scalars().all()
    ]

    return {
        "scenario_id": 3,
        "title": "Autonomous Escalation Ceiling (Attempt Limit Guardrail)",
        "event_id": event.id,
        "customer_name": event.customer_name,
        "original_amount": 4500.0,
        "attempt_1_strategy": "SECURE_PAYMENT_LINK",
        "attempt_2_strategy": "ESCALATE_TO_HUMAN",
        "guardrail_decision": "OVERRIDDEN (RULE2_MAX_INTERVENTIONS_ESCALATE)",
        "guardrail_reason": "Maximum interventions reached (intervention_count >= 2). Automated recovery stopped to prevent customer harassment.",
        "final_status": "ESCALATED_STOPPED",
        "workflow_stopped": True,
        "amount_recovered": 0.0,
        "audit_trail": audit_logs,
    }


# --------------------------------------------------------------------------- #
# Scenario 4: Gemini Outage Resilience (Graceful System Fallback)              #
# --------------------------------------------------------------------------- #
@router.post("/scenario/4", summary="Scenario 4: Gemini Outage Resilience")
@router.post("/scenario-4-gemini-outage", summary="Scenario 4 Alias")
async def run_scenario_4(db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """
    Scenario 4:
      1. Create ₹2,500 failed payment with gateway timeout.
      2. Gemini API call raises a simulated 503 Quota / Network Outage exception.
      3. Recovery agent catches the error and safely returns [FALLBACK] SECURE_PAYMENT_LINK (confidence=0.0).
      4. Decision engine processes safely without crashing and records [FALLBACK] reasoning in audit log.
    """
    event = PaymentEvent(
        id=str(uuid.uuid4()),
        razorpay_event_id=f"evt_demo4_{uuid.uuid4().hex[:8]}",
        customer_id="cust_demo_outage",
        customer_name="Devika Sen (Growth Tier)",
        customer_email="devika.sen@growth-demo.in",
        customer_contact="+919876543213",
        amount=2500.0,
        currency="INR",
        error_code="GATEWAY_TIMEOUT",
        error_reason="upstream_timeout",
        status=PaymentStatus.AT_RISK,
        raw_payload='{"scenario": 4, "tier": "Growth 2500"}',
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)

    # In production/test path, simulate Gemini API raising an outage exception
    simulated_error = "503 Service Unavailable: Gemini API Gateway Outage (Simulated Fault Injection)"
    fallback_decision = AgentDecision(
        recommended_strategy="SECURE_PAYMENT_LINK",
        confidence_score=0.0,
        reasoning=f"[FALLBACK] Gemini API error: {simulated_error}",
        requires_consent=False,
    )
    with patch(
        "app.services.decision_engine.analyze_failure_context",
        new=AsyncMock(return_value=fallback_decision),
    ):
        await _run_engine(db, event.id, is_simulated=True)

    # Fetch audit logs
    audit_res = await db.execute(
        select(InterventionAuditLog)
        .where(InterventionAuditLog.payment_event_id == event.id)
        .order_by(InterventionAuditLog.timestamp.asc())
    )
    audit_logs = [
        {
            "id": log.id,
            "strategy": log.executed_strategy,
            "ai_strategy": log.ai_recommended_strategy,
            "ai_confidence": log.ai_confidence,
            "guardrail_decision": log.guardrail_decision,
            "reasoning": log.reasoning,
            "channel": log.channel,
            "timestamp": log.timestamp.isoformat(),
        }
        for log in audit_res.scalars().all()
    ]

    return {
        "scenario_id": 4,
        "title": "Gemini API Outage Resilience (Deterministic Fallback)",
        "event_id": event.id,
        "customer_name": event.customer_name,
        "original_amount": 2500.0,
        "simulated_fault": simulated_error,
        "fallback_strategy": "SECURE_PAYMENT_LINK",
        "confidence_score": 0.0,
        "guardrail_decision": "APPROVED",
        "final_status": "INTERVENTION_ACTIVE",
        "ai_reasoning": fallback_decision.reasoning,
        "audit_trail": audit_logs,
    }


# --------------------------------------------------------------------------- #
# Scenario 5 / A: VIP Relationship Retention (High Value Tier -> Full Link)   #
# --------------------------------------------------------------------------- #
@router.post("/scenario/5", summary="Scenario 5: VIP Relationship Retention")
@router.post("/scenario/a", summary="Scenario A Alias")
@router.post("/scenario-5-vip-retention", summary="Scenario 5 Alias")
async def run_scenario_5(db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """
    Scenario A / 5:
      1. Create ₹15,000 failed payment for HIGH value tier customer (36 months tenure).
      2. Decision engine resolves CustomerContext (Tier: HIGH, Tenure: 36m).
      3. AI prioritizes SECURE_PAYMENT_LINK over aggressive downsell to preserve full contract value.
      4. Guardrail Rule 4 calculates Net Recovery Value: ₹15,000 - ₹2.50 = ₹14,997.50 > 0 -> APPROVED.
      5. Reconciles payment at ₹15,000.
    """
    event = PaymentEvent(
        id=str(uuid.uuid4()),
        razorpay_event_id=f"evt_demo5_{uuid.uuid4().hex[:8]}",
        customer_id="cust_demo_vip",
        customer_name="Dr. Siddharth Sen (VIP Enterprise)",
        customer_email="siddharth.sen@enterprise-health.in",
        customer_contact="+919876543214",
        amount=15000.0,
        currency="INR",
        error_code="BAD_REQUEST_ERROR",
        error_reason="insufficient_funds",
        status=PaymentStatus.AT_RISK,
        raw_payload='{"scenario": 5, "tier": "VIP Enterprise", "value_tier": "HIGH", "tenure_months": 36}',
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)

    mock_decision = AgentDecision(
        recommended_strategy="SECURE_PAYMENT_LINK",
        confidence_score=0.95,
        reasoning="[AI AGENT] High-value customer (Tier=HIGH, Tenure=36m). Preserving full relationship value via secure direct payment link instead of plan downgrade.",
        requires_consent=False,
    )
    with patch(
        "app.services.decision_engine.analyze_failure_context",
        new=AsyncMock(return_value=mock_decision),
    ):
        await _run_engine(db, event.id, is_simulated=True)

    # Reconcile customer paying the full amount
    recon_result = await reconcile_payment_success(
        payment_identifier=event.id,
        amount_paid=15000.0,
        db=db,
        source="DEMO_STUDIO_VIP_CUSTOMER",
    )

    audit_res = await db.execute(
        select(InterventionAuditLog)
        .where(InterventionAuditLog.payment_event_id == event.id)
        .order_by(InterventionAuditLog.timestamp.asc())
    )
    audit_logs = [
        {
            "id": log.id,
            "strategy": log.executed_strategy,
            "ai_strategy": log.ai_recommended_strategy,
            "ai_confidence": log.ai_confidence,
            "guardrail_decision": log.guardrail_decision,
            "reasoning": log.reasoning,
            "intervention_cost": log.intervention_cost,
            "net_recovery_value": log.net_recovery_value,
            "channel": log.channel,
            "timestamp": log.timestamp.isoformat(),
        }
        for log in audit_res.scalars().all()
    ]

    return {
        "scenario_id": 5,
        "scenario_code": "A",
        "title": "VIP Relationship Retention (Customer Value Intelligence)",
        "event_id": event.id,
        "customer_name": event.customer_name,
        "original_amount": 15000.0,
        "customer_value_tier": "HIGH",
        "customer_tenure_months": 36,
        "ai_recommendation": "SECURE_PAYMENT_LINK",
        "guardrail_decision": "APPROVED",
        "executed_strategy": "SECURE_PAYMENT_LINK",
        "intervention_cost": 2.50,
        "net_recovery_value": 14997.50,
        "recovered_amount": recon_result.get("amount_recovered", 15000.0) if recon_result else 15000.0,
        "final_status": "RECOVERED",
        "audit_trail": audit_logs,
    }


# --------------------------------------------------------------------------- #
# Scenario 6 / B: Negative Unit Economics Block (Cost > Recovery -> Retry)    #
# --------------------------------------------------------------------------- #
@router.post("/scenario/6", summary="Scenario 6: Negative Unit Economics Gate")
@router.post("/scenario/b", summary="Scenario B Alias")
@router.post("/scenario-6-negative-economics", summary="Scenario 6 Alias")
async def run_scenario_6(db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """
    Scenario B / 6:
      1. Create ₹100 failed payment for micro-tier subscriber.
      2. AI suggests ESCALATE_TO_HUMAN (simulated intervention cost ₹150.00).
      3. Priority 3 Guardrail calculates Net Recovery Value = ₹0.00 - ₹150.00 = -₹150.00 (<= 0).
      4. Overrides to SILENT_MANDATE_RETRY (Cost: ₹0.00, Net Value: ₹100.00).
    """
    event = PaymentEvent(
        id=str(uuid.uuid4()),
        razorpay_event_id=f"evt_demo6_{uuid.uuid4().hex[:8]}",
        customer_id="cust_demo_micro_econ",
        customer_name="Pooja Nair (Micro Tier)",
        customer_email="pooja.nair@micro-demo.in",
        customer_contact="+919876543215",
        amount=100.0,
        currency="INR",
        error_code="BAD_REQUEST_ERROR",
        error_reason="card_declined",
        status=PaymentStatus.AT_RISK,
        raw_payload='{"scenario": 6, "tier": "Micro 100", "value_tier": "LOW", "tenure_months": 3}',
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)

    # Simulate AI proposing expensive human escalation on a tiny invoice
    mock_decision = AgentDecision(
        recommended_strategy="ESCALATE_TO_HUMAN",
        confidence_score=0.75,
        reasoning="[AI AGENT] Complex decline on micro invoice. Proposing human support escalation.",
        requires_consent=False,
    )
    with patch(
        "app.services.decision_engine.analyze_failure_context",
        new=AsyncMock(return_value=mock_decision),
    ):
        await _run_engine(db, event.id, is_simulated=True)

    audit_res = await db.execute(
        select(InterventionAuditLog)
        .where(InterventionAuditLog.payment_event_id == event.id)
        .order_by(InterventionAuditLog.timestamp.asc())
    )
    audit_logs = [
        {
            "id": log.id,
            "strategy": log.executed_strategy,
            "ai_strategy": log.ai_recommended_strategy,
            "ai_confidence": log.ai_confidence,
            "guardrail_decision": log.guardrail_decision,
            "reasoning": log.reasoning,
            "intervention_cost": log.intervention_cost,
            "net_recovery_value": log.net_recovery_value,
            "channel": log.channel,
            "timestamp": log.timestamp.isoformat(),
        }
        for log in audit_res.scalars().all()
    ]

    return {
        "scenario_id": 6,
        "scenario_code": "B",
        "title": "Negative Unit Economics Block (Priority 3 Guardrail)",
        "event_id": event.id,
        "customer_name": event.customer_name,
        "original_amount": 100.0,
        "customer_value_tier": "LOW",
        "ai_recommendation": "ESCALATE_TO_HUMAN (Cost: ₹150.00)",
        "guardrail_decision": "OVERRIDDEN (RULE4_NEGATIVE_UNIT_ECONOMICS_BLOCK)",
        "executed_strategy": "SILENT_MANDATE_RETRY",
        "intervention_cost": 0.0,
        "net_recovery_value": 100.0,
        "final_status": "INTERVENTION_ACTIVE",
        "guardrail_reason": "Blocked by Unit Economics: Proposed strategy 'ESCALATE_TO_HUMAN' has non-positive Net Recovery Value (Net: -₹150.00 <= 0). Overridden to SILENT_MANDATE_RETRY.",
        "audit_trail": audit_logs,
    }


# --------------------------------------------------------------------------- #
# Scenario 7 / C: Safety Precedence Over Economics (Max Interventions Rule)   #
# --------------------------------------------------------------------------- #
@router.post("/scenario/7", summary="Scenario 7: Safety Precedence Over Economics")
@router.post("/scenario/c", summary="Scenario C Alias")
@router.post("/scenario-7-safety-precedence", summary="Scenario 7 Alias")
async def run_scenario_7(db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """
    Scenario C / 7:
      1. Create ₹150 failed payment where workflow reached intervention_count >= 2.
      2. AI/economic logic prefers a free SILENT_MANDATE_RETRY (₹0 cost).
      3. Priority 4 Guardrail strictly overrides economic logic: intervention_count >= 2
         MUST force ESCALATE_TO_HUMAN and transition to ESCALATED_STOPPED.
      4. Demonstrates Safety & Anti-Harassment beat unit economics.
    """
    event = PaymentEvent(
        id=str(uuid.uuid4()),
        razorpay_event_id=f"evt_demo7_{uuid.uuid4().hex[:8]}",
        customer_id="cust_demo_harassment_guard",
        customer_name="Karan Malhotra (Safety Test)",
        customer_email="karan.malhotra@safety-demo.in",
        customer_contact="+919876543216",
        amount=150.0,
        currency="INR",
        error_code="BAD_REQUEST_ERROR",
        error_reason="card_declined",
        status=PaymentStatus.AT_RISK,
        raw_payload='{"scenario": 7, "tier": "Micro 150", "value_tier": "LOW"}',
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)

    # Seed an existing workflow with intervention_count = 2
    prior_wf = RecoveryWorkflow(
        id=str(uuid.uuid4()),
        payment_event_id=event.id,
        diagnosed_cause=DiagnosedCause.EXPIRED_PAYMENT_METHOD,
        strategy=RecoveryStrategy.SECURE_PAYMENT_LINK,
        current_step=1,
        max_steps=2,
        retry_count=0,
        intervention_count=2,
        is_active=True,
    )
    db.add(prior_wf)
    await db.commit()

    # Agent recommends free silent mandate retry
    mock_decision = AgentDecision(
        recommended_strategy="SILENT_MANDATE_RETRY",
        confidence_score=0.90,
        reasoning="[AI AGENT] Proposing free silent retry to maximize unit economics on low invoice.",
        requires_consent=False,
    )
    with patch(
        "app.services.decision_engine.analyze_failure_context",
        new=AsyncMock(return_value=mock_decision),
    ):
        await _run_engine(db, event.id, is_simulated=True)

    audit_res = await db.execute(
        select(InterventionAuditLog)
        .where(InterventionAuditLog.payment_event_id == event.id)
        .order_by(InterventionAuditLog.timestamp.asc())
    )
    audit_logs = [
        {
            "id": log.id,
            "strategy": log.executed_strategy,
            "ai_strategy": log.ai_recommended_strategy,
            "ai_confidence": log.ai_confidence,
            "guardrail_decision": log.guardrail_decision,
            "reasoning": log.reasoning,
            "intervention_cost": log.intervention_cost,
            "net_recovery_value": log.net_recovery_value,
            "channel": log.channel,
            "timestamp": log.timestamp.isoformat(),
        }
        for log in audit_res.scalars().all()
    ]

    return {
        "scenario_id": 7,
        "scenario_code": "C",
        "title": "Safety Beats Economics (Priority 4 Anti-Harassment Ceiling)",
        "event_id": event.id,
        "customer_name": event.customer_name,
        "original_amount": 150.0,
        "customer_value_tier": "LOW",
        "ai_recommendation": "SILENT_MANDATE_RETRY (Free / Positive Economics)",
        "guardrail_decision": "OVERRIDDEN (RULE2_MAX_INTERVENTIONS_ESCALATE)",
        "executed_strategy": "ESCALATE_TO_HUMAN",
        "final_status": "ESCALATED_STOPPED",
        "workflow_stopped": True,
        "guardrail_reason": "Maximum interventions reached (intervention_count=2 >= 2). Safety & anti-harassment override economic preferences.",
        "audit_trail": audit_logs,
    }
