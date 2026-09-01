"""
app/models/orm.py
-----------------
SQLAlchemy 2.x ORM models for the Revora Revenue Recovery Engine.

Models:
  • PaymentEvent          — captures a failed payment with full customer context.
  • RecoveryWorkflow      — orchestrates an AI-driven recovery strategy for an event.
  • InterventionAuditLog  — immutable, structured audit trail of every recovery action.

All models share a common declarative Base and include:
  • id          — UUID string primary key (generated at Python level).
  • created_at  — server-side timestamp set on INSERT.
  • updated_at  — server-side timestamp updated on each UPDATE.

Phase 7 changes:
  • PaymentStatus enum updated to strict bounded state machine states.
  • PaymentEvent gains amount_recovered and is_idempotent_lock columns.

Phase 8A changes:
  • PaymentEvent.razorpay_event_id — unique, indexed column for native DB-level
    idempotency (replaces is_idempotent_lock row-lock approach).
  • PaymentEvent.is_idempotent_lock removed.
  • PaymentEvent.amount_recovered moved → RecoveryWorkflow.amount_recovered.
  • RecoveryWorkflow.intervention_count added to separate workflow runs from pulses.
  • RecoveryAuditLog renamed → InterventionAuditLog with structured AI/Guardrail fields.
    - action_type renamed → executed_strategy.
    - ai_recommended_strategy, ai_confidence, ai_reasoning, guardrail_decision added.
  • RecoveryAuditLog kept as a compatibility alias (= InterventionAuditLog).
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# --------------------------------------------------------------------------- #
# Enumerations                                                                 #
# --------------------------------------------------------------------------- #

class PaymentStatus(str, enum.Enum):
    """
    Strict bounded lifecycle states of a failed payment inside the Revora engine.

    State machine transitions:
      AT_RISK → DIAGNOSED → INTERVENTION_ACTIVE → RECOVERED
                                               ↘ ESCALATED_STOPPED
    """
    AT_RISK             = "AT_RISK"             # Ingested, pending decision engine
    DIAGNOSED           = "DIAGNOSED"           # Root cause classified, agent decision pending
    INTERVENTION_ACTIVE = "INTERVENTION_ACTIVE" # Outreach dispatched, awaiting customer action
    RECOVERED           = "RECOVERED"           # Payment successfully captured (terminal)
    ESCALATED_STOPPED   = "ESCALATED_STOPPED"   # Max retries exhausted, escalated (terminal)


class DiagnosedCause(str, enum.Enum):
    """Root-cause classification assigned by the AI diagnostic agent."""
    TEMPORARY_NETWORK_FAILURE       = "TEMPORARY_NETWORK_FAILURE"
    EXPIRED_PAYMENT_METHOD          = "EXPIRED_PAYMENT_METHOD"
    INSUFFICIENT_FUNDS_ADAPTIVE     = "INSUFFICIENT_FUNDS_ADAPTIVE"
    CHECKOUT_ABANDONED              = "CHECKOUT_ABANDONED"
    MANDATE_DECLINED                = "MANDATE_DECLINED"


class RecoveryStrategy(str, enum.Enum):
    """Recovery strategy selected by the AI orchestration agent."""
    SILENT_MANDATE_RETRY        = "SILENT_MANDATE_RETRY"
    SECURE_PAYMENT_LINK         = "SECURE_PAYMENT_LINK"
    UPI_AUTOPAY_MIGRATION       = "UPI_AUTOPAY_MIGRATION"
    ADAPTIVE_DOWNGRADE_OFFER    = "ADAPTIVE_DOWNGRADE_OFFER"
    HINGLISH_VOICE_OUTREACH     = "HINGLISH_VOICE_OUTREACH"


# --------------------------------------------------------------------------- #
# Declarative Base                                                             #
# --------------------------------------------------------------------------- #

class Base(DeclarativeBase):
    """Common declarative base shared by all Revora ORM models."""

    # Shared columns injected into every subclass table
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# --------------------------------------------------------------------------- #
# PaymentEvent                                                                 #
# --------------------------------------------------------------------------- #

class PaymentEvent(Base):
    """
    Represents a single failed payment event ingested from a Razorpay webhook.

    Stores all customer-facing data, the error context returned by Razorpay,
    and the current recovery lifecycle status.

    Phase 8A:
      • razorpay_event_id — Razorpay's native x-razorpay-event-id header value,
        stored with a UNIQUE constraint to provide DB-level idempotency. A
        duplicate webhook delivery will raise IntegrityError on INSERT, which the
        webhook handler catches and turns into a 200 "ignored" response.
      • is_idempotent_lock removed (replaced by the unique constraint above).
      • amount_recovered moved to RecoveryWorkflow for per-workflow accounting.
    """
    __tablename__ = "payment_events"

    # --- Phase 8A: Native Razorpay event ID for DB-level idempotency ---
    razorpay_event_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        unique=True,
        index=True,
    )

    # --- Razorpay identifiers ---
    razorpay_payment_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    razorpay_order_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    razorpay_subscription_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )

    # --- Customer context ---
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    customer_name: Mapped[str] = mapped_column(String(256), nullable=False)
    customer_email: Mapped[str] = mapped_column(String(256), nullable=False)
    customer_contact: Mapped[str] = mapped_column(String(20), nullable=False)

    # --- Payment details ---
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")

    # --- Failure context ---
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # --- Lifecycle status ---
    status: Mapped[PaymentStatus] = mapped_column(
        SAEnum(PaymentStatus, native_enum=False, length=32),
        nullable=False,
        default=PaymentStatus.AT_RISK,
        index=True,
    )

    # --- Raw webhook payload ---
    raw_payload: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Relationships ---
    workflows: Mapped[list["RecoveryWorkflow"]] = relationship(
        back_populates="payment_event", cascade="all, delete-orphan"
    )
    audit_logs: Mapped[list["InterventionAuditLog"]] = relationship(
        back_populates="payment_event", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<PaymentEvent id={self.id!r} "
            f"customer={self.customer_email!r} "
            f"amount={self.amount} {self.currency} "
            f"status={self.status!r}>"
        )


# --------------------------------------------------------------------------- #
# RecoveryWorkflow                                                             #
# --------------------------------------------------------------------------- #

class RecoveryWorkflow(Base):
    """
    Tracks an autonomous AI recovery workflow for a specific PaymentEvent.

    The workflow progresses through numbered steps according to the selected
    strategy, with the AI agent deciding the next action at each step.

    Phase 8A:
      • amount_recovered    — INR amount captured by this workflow (moved from PaymentEvent).
      • intervention_count  — number of discrete outreach pulses dispatched (separate
                              from retry_count which tracks full workflow re-runs).
    """
    __tablename__ = "recovery_workflows"

    # --- Foreign key ---
    payment_event_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("payment_events.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # --- AI diagnosis & strategy ---
    diagnosed_cause: Mapped[DiagnosedCause] = mapped_column(
        SAEnum(DiagnosedCause, native_enum=False, length=48),
        nullable=False,
    )
    strategy: Mapped[RecoveryStrategy] = mapped_column(
        SAEnum(RecoveryStrategy, native_enum=False, length=48),
        nullable=False,
    )

    # --- Step tracking ---
    current_step: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_steps: Mapped[int]    = mapped_column(Integer, nullable=False, default=3)
    retry_count: Mapped[int]  = mapped_column(Integer, nullable=False, default=0)

    # --- Phase 8A: Outreach pulse count (separate from retry_count) ---
    intervention_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- Phase 8A: Revenue recovered by this workflow ---
    amount_recovered: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # --- Scheduling ---
    next_action_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- Active flag ---
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # --- Relationships ---
    payment_event: Mapped["PaymentEvent"] = relationship(back_populates="workflows")
    audit_logs: Mapped[list["InterventionAuditLog"]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<RecoveryWorkflow id={self.id!r} "
            f"strategy={self.strategy!r} "
            f"step={self.current_step}/{self.max_steps} "
            f"retries={self.retry_count} "
            f"interventions={self.intervention_count} "
            f"recovered={self.amount_recovered} "
            f"active={self.is_active}>"
        )


# --------------------------------------------------------------------------- #
# InterventionAuditLog  (renamed from RecoveryAuditLog in Phase 8A)           #
# --------------------------------------------------------------------------- #

class InterventionAuditLog(Base):
    """
    Immutable, structured audit record for every action taken by the recovery engine.

    Phase 8A changes vs RecoveryAuditLog:
      • Table renamed from ``recovery_audit_logs`` → ``intervention_audit_log``.
      • ``action_type`` renamed → ``executed_strategy`` for technical honesty
        (the column stores the strategy that was actually executed, not a generic
        action type string).
      • Dedicated structured columns for the AI and Guardrail decision trail:
          - ai_recommended_strategy: what the LLM agent proposed.
          - ai_confidence:           agent's confidence score (0.0–1.0).
          - ai_reasoning:            agent's reasoning chain (full text).
          - guardrail_decision:      "APPROVED" | "OVERRIDDEN" | "BLOCKED".
      • ``metadata_json`` retained for arbitrary outreach result payloads.
      • ``reasoning`` retained for the combined agent+guardrail reasoning string.
    """
    __tablename__ = "intervention_audit_log"

    # --- Foreign keys ---
    workflow_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("recovery_workflows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    payment_event_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("payment_events.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # --- Phase 8A: Strategy actually executed (renamed from action_type) ---
    executed_strategy: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )

    # --- Phase 8A: Structured AI decision audit ---
    ai_recommended_strategy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ai_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Phase 8A: Guardrail outcome ---
    guardrail_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # --- Combined reasoning (agent + guardrail, for backward compat) ---
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Communication channel ---
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="SYSTEM")

    # --- Arbitrary action metadata ---
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Override timestamp (default via Base.created_at) ---
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # --- Relationships ---
    workflow: Mapped["RecoveryWorkflow"] = relationship(back_populates="audit_logs")
    payment_event: Mapped["PaymentEvent"] = relationship(back_populates="audit_logs")

    def __repr__(self) -> str:
        return (
            f"<InterventionAuditLog id={self.id!r} "
            f"executed_strategy={self.executed_strategy!r} "
            f"guardrail={self.guardrail_decision!r} "
            f"channel={self.channel!r}>"
        )


# --------------------------------------------------------------------------- #
# Backward-compatibility alias                                                 #
# --------------------------------------------------------------------------- #

#: ``RecoveryAuditLog`` is preserved as a module-level alias so that any
#: existing code still referencing the old name continues to work without
#: modification. New code should use ``InterventionAuditLog`` directly.
RecoveryAuditLog = InterventionAuditLog
