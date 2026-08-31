"""
app/models/schemas.py
----------------------
Pydantic v2 schemas for the Revora Revenue Recovery Engine.

Organised in layers:
  ① Core envelopes         — APIResponse, ErrorDetail (Phase 0)
  ② Payment domain         — PaymentEventCreate, PaymentEventRead
  ③ Recovery workflow      — RecoveryWorkflowCreate, RecoveryWorkflowRead
  ④ Audit log              — AuditLogCreate, AuditLogRead
  ⑤ Analytics              — RecoverySummaryStats
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
# PaymentEvent schemas                                                         #
# --------------------------------------------------------------------------- #

class PaymentEventCreate(BaseModel):
    """Fields required to ingest a new failed payment event."""

    # Razorpay identifiers (all optional — may not exist for abandoned checkouts)
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
    """Full ORM-backed representation of a PaymentEvent (returned by the API)."""

    id:                       str
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
    """Full ORM-backed representation of a RecoveryWorkflow."""

    id:               str
    payment_event_id: str
    diagnosed_cause:  DiagnosedCause
    strategy:         RecoveryStrategy
    current_step:     int
    max_steps:        int
    retry_count:      int
    next_action_at:   Optional[datetime] = None
    resolved_at:      Optional[datetime] = None
    is_active:        bool
    created_at:       datetime
    updated_at:       datetime

    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- #
# AuditLog schemas                                                             #
# --------------------------------------------------------------------------- #

class AuditLogCreate(BaseModel):
    """Fields required to append an audit record to the recovery trail."""

    workflow_id:      str
    payment_event_id: str
    action_type:      str = Field(
        description=(
            "One of: SILENT_RETRY_SCHEDULED, PAYMENT_LINK_GENERATED, "
            "WHATSAPP_NUDGE_SENT, VOICE_CALL_TRIGGERED, "
            "PAYMENT_CONFIRMED, STOP_LIMIT_REACHED"
        )
    )
    reasoning:     Optional[str] = None
    channel:       str           = Field(default="SYSTEM")
    metadata_json: Optional[Any] = None

    @field_validator("metadata_json", mode="before")
    @classmethod
    def serialise_metadata(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        return v if isinstance(v, str) else json.dumps(v)

    model_config = ConfigDict(str_strip_whitespace=True)


class AuditLogRead(BaseModel):
    """Full ORM-backed representation of a RecoveryAuditLog."""

    id:               str
    workflow_id:      str
    payment_event_id: str
    action_type:      str
    reasoning:        Optional[str] = None
    channel:          str
    metadata_json:    Optional[str] = None
    timestamp:        datetime
    created_at:       datetime

    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- #
# Analytics / reporting                                                        #
# --------------------------------------------------------------------------- #

class ActionBreakdown(BaseModel):
    """Count of audit log entries per action type."""

    action_type: str
    count:       int


class RecoverySummaryStats(BaseModel):
    """
    Aggregated metrics for the Revora recovery dashboard.

    Returned by the analytics endpoint to summarise engine performance
    over a given time window.
    """

    total_at_risk:        int   = Field(description="Events currently in AT_RISK state")
    total_in_recovery:    int   = Field(description="Events actively being recovered")
    total_recovered:      int   = Field(description="Events successfully recovered")
    total_failed:         int   = Field(description="Events exhausted without recovery")
    total_stopped:        int   = Field(description="Events stopped for compliance")

    recovery_rate_pct:    float = Field(description="Recovered / (Recovered + Failed) × 100")
    total_amount_at_risk: float = Field(description="Sum of amounts for AT_RISK events (INR)")
    recovered_amount:     float = Field(description="Sum of amounts for RECOVERED events (INR)")

    action_breakdowns:    List[ActionBreakdown] = Field(
        default_factory=list,
        description="Per-action-type audit log counts",
    )

    model_config = ConfigDict(from_attributes=True)
