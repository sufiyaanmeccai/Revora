"""
app/models/schemas.py
----------------------
Pydantic v2 schemas for the Revora Revenue Recovery Engine.

Organised in layers:
  ① Core envelopes         — APIResponse, ErrorDetail (Phase 0)
  ② Payment domain         — PaymentEventCreate, PaymentEventRead
  ③ Recovery workflow      — RecoveryWorkflowCreate, RecoveryWorkflowRead
  ④ Audit log              — InterventionAuditLogRead (Phase 8A rename)
  ⑤ Agent decision         — AgentDecision (Phase 7)
  ⑥ Analytics              — RecoverySummaryStats

Phase 8A changes:
  • PaymentEventRead:  razorpay_event_id added, is_idempotent_lock removed,
    amount_recovered removed (moved to RecoveryWorkflowRead).
  • RecoveryWorkflowRead: amount_recovered + intervention_count added.
  • AuditLogRead renamed → InterventionAuditLogRead:
    - executed_strategy replaces action_type.
    - ai_recommended_strategy, ai_confidence, ai_reasoning, guardrail_decision added.
  • AuditLogRead kept as alias for backward compat.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Generic, List, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.orm import DiagnosedCause, PaymentStatus, RecoveryStrategy

# --------------------------------------------------------------------------- #
# Generic envelope (Phase 0 — preserved)                                       #
# --------------------------------------------------------------------------- #
DataT = TypeVar("DataT")


class APIResponse(BaseModel, Generic[DataT]):
    """Generic JSON envelope used by all Revora endpoints."""

    success: bool = True
    data: Optional[DataT] = None
    message: str = "OK"
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class ErrorDetail(BaseModel):
    """Structured error payload."""

    code: str
    message: str
    details: Optional[Any] = None


# --------------------------------------------------------------------------- #
# Phase 7: AI Agent Decision schema                                            #
# --------------------------------------------------------------------------- #

class AgentDecision(BaseModel):
    """
    Structured output produced by the Recovery Agent (Phase 7).

    Mirrors what an LLM structured-output framework (Instructor / LangChain)
    would yield when analysing a failed PaymentEvent. The GuardrailEngine
    validates and may override this decision before it is executed.

    Fields:
        recommended_strategy: The strategy the agent recommends.
        confidence_score:     Agent confidence in the recommendation (0.0–1.0).
        reasoning:            Human-readable explanation of the agent's logic.
        requires_consent:     True if the strategy requires explicit customer
                              consent before execution (e.g., plan downgrades,
                              mandate migrations).
    """

    recommended_strategy: RecoveryStrategy
    confidence_score: float = Field(
        ge=0.0,
        le=1.0,
        description="Agent confidence in the recommended strategy (0.0 = no confidence, 1.0 = certain).",
    )
    reasoning: str = Field(
        description="LLM reasoning chain explaining why this strategy was selected."
    )
    requires_consent: bool = Field(
        default=False,
        description="True if the strategy requires explicit customer consent before execution.",
    )

    model_config = ConfigDict(str_strip_whitespace=True)


# --------------------------------------------------------------------------- #
# PaymentEvent schemas                                                         #
# --------------------------------------------------------------------------- #

class PaymentEventCreate(BaseModel):
    """Fields required to ingest a new failed payment event."""

    # Razorpay identifiers (all optional — may not exist for abandoned checkouts)
    razorpay_event_id:        Optional[str] = None
    razorpay_payment_id:      Optional[str] = None
    razorpay_order_id:        Optional[str] = None
    razorpay_subscription_id: Optional[str] = None

    # Customer
    customer_id:      str
    customer_name:    str
    customer_email:   str
    customer_contact: str

    # Payment
    amount:   float = Field(gt=0, description="Amount in INR paise")
    currency: str   = Field(default="INR", max_length=3)

    # Failure context
    error_code:        Optional[str] = None
    error_description: Optional[str] = None
    error_source:      Optional[str] = None
    error_reason:      Optional[str] = None

    # Raw webhook payload — serialise dict to str if necessary
    raw_payload: Optional[Any] = None

    @field_validator("raw_payload", mode="before")
    @classmethod
    def serialise_payload(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        return v if isinstance(v, str) else json.dumps(v)

    model_config = ConfigDict(str_strip_whitespace=True)


class PaymentEventRead(BaseModel):
    """
    Full ORM-backed representation of a PaymentEvent (returned by the API).

    Phase 8A:
      • razorpay_event_id added.
      • is_idempotent_lock removed.
      • amount_recovered removed (now on RecoveryWorkflowRead).
    """

    id:                       str
    # Phase 8A: native event ID for idempotency
    razorpay_event_id:        Optional[str] = None
    razorpay_payment_id:      Optional[str] = None
    razorpay_order_id:        Optional[str] = None
    razorpay_subscription_id: Optional[str] = None
    customer_id:              str
    customer_name:            str
    customer_email:           str
    customer_contact:         str
    amount:                   float
    currency:                 str
    error_code:               Optional[str] = None
    error_description:        Optional[str] = None
    error_source:             Optional[str] = None
    error_reason:             Optional[str] = None
    status:                   PaymentStatus
    raw_payload:              Optional[str] = None
    created_at:               datetime
    updated_at:               datetime

    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- #
# RecoveryWorkflow schemas                                                     #
# --------------------------------------------------------------------------- #

class RecoveryWorkflowCreate(BaseModel):
    """Fields required to open a new recovery workflow for a payment event."""

    payment_event_id: str
    diagnosed_cause:  DiagnosedCause
    strategy:         RecoveryStrategy
    max_steps:        int = Field(default=3, ge=1, le=10)

    model_config = ConfigDict(str_strip_whitespace=True)


class RecoveryWorkflowRead(BaseModel):
    """
    Full ORM-backed representation of a RecoveryWorkflow.

    Phase 8A:
      • amount_recovered added (moved from PaymentEventRead).
      • intervention_count added.
    """

    id:               str
    payment_event_id: str
    diagnosed_cause:  DiagnosedCause
    strategy:         RecoveryStrategy
    current_step:     int
    max_steps:        int
    retry_count:      int
    # Phase 8A fields
    intervention_count: int = 0
    amount_recovered:   float = 0.0
    next_action_at:   Optional[datetime] = None
    resolved_at:      Optional[datetime] = None
    is_active:        bool
    created_at:       datetime
    updated_at:       datetime

    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- #
# InterventionAuditLog schemas  (Phase 8A rename from AuditLog)               #
# --------------------------------------------------------------------------- #

class InterventionAuditLogRead(BaseModel):
    """
    Full ORM-backed representation of an InterventionAuditLog.

    Phase 8A:
      • executed_strategy replaces action_type.
      • Structured AI/Guardrail columns: ai_recommended_strategy, ai_confidence,
        ai_reasoning, guardrail_decision.
    """

    id:               str
    workflow_id:      str
    payment_event_id: str

    # Phase 8A: strategy actually executed
    executed_strategy:        str

    # Phase 8A: structured AI audit
    ai_recommended_strategy:  Optional[str]   = None
    ai_confidence:            Optional[float] = None
    ai_reasoning:             Optional[str]   = None
    guardrail_decision:       Optional[str]   = None

    # Combined reasoning (backward compat)
    reasoning:        Optional[str] = None
    channel:          str
    metadata_json:    Optional[str] = None
    timestamp:        datetime
    created_at:       datetime

    model_config = ConfigDict(from_attributes=True)


#: Backward-compatibility alias
AuditLogRead = InterventionAuditLogRead


# --------------------------------------------------------------------------- #
# Analytics / reporting                                                        #
# --------------------------------------------------------------------------- #

class ActionBreakdown(BaseModel):
    """Count of audit log entries per executed_strategy."""

    action_type: str   # kept as "action_type" for API stability
    count:       int


class RecoverySummaryStats(BaseModel):
    """
    Aggregated metrics for the Revora recovery dashboard.

    Phase 7 state groupings:
      • "At Risk" = AT_RISK + DIAGNOSED + INTERVENTION_ACTIVE
        (all events not yet in a terminal state).
      • "Recovered" = RECOVERED (terminal success).
      • "Escalated" = ESCALATED_STOPPED (terminal failure).

    Phase 8A:
      • recovered_amount now sums RecoveryWorkflow.amount_recovered
        (moved from PaymentEvent).
    """

    # --- Phase 7 state counts ---
    total_at_risk:          int   = Field(description="Events in AT_RISK state")
    total_diagnosed:        int   = Field(description="Events in DIAGNOSED state")
    total_intervention:     int   = Field(description="Events in INTERVENTION_ACTIVE state")
    total_recovered:        int   = Field(description="Events successfully recovered (terminal)")
    total_escalated:        int   = Field(description="Events escalated/stopped (terminal)")

    # --- Aggregate "in-flight" count for dashboard display ---
    total_in_recovery:      int   = Field(description="DIAGNOSED + INTERVENTION_ACTIVE combined")

    # --- Revenue metrics ---
    recovery_rate_pct:      float = Field(description="Recovered / (At Risk + Recovered) × 100")
    total_amount_at_risk:   float = Field(description="Sum of amounts for AT_RISK+DIAGNOSED+INTERVENTION_ACTIVE (INR)")
    recovered_amount:       float = Field(description="Sum of workflow.amount_recovered for RECOVERED events (INR)")

    action_breakdowns:      List[ActionBreakdown] = Field(
        default_factory=list,
        description="Per-strategy audit log counts",
    )
    cause_breakdown:        Dict[str, int] = Field(
        default_factory=dict,
        description="Breakdown of workflows grouped by diagnosed cause",
    )
    strategy_breakdown:     Dict[str, int] = Field(
        default_factory=dict,
        description="Breakdown of workflows grouped by recovery strategy",
    )

    model_config = ConfigDict(from_attributes=True)
