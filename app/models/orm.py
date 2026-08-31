"""
app/models/orm.py
-----------------
SQLAlchemy 2.x ORM models for the Revora Revenue Recovery Engine.

Models:
  • PaymentEvent       — captures a failed payment with full customer context.
  • RecoveryWorkflow   — orchestrates an AI-driven recovery strategy for an event.
  • RecoveryAuditLog   — immutable audit trail of every recovery action taken.

All models share a common declarative Base and include:
  • id          — UUID string primary key (generated at Python level).
  • created_at  — server-side timestamp set on INSERT.
  • updated_at  — server-side timestamp updated on each UPDATE.
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
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# --------------------------------------------------------------------------- #
# Enumerations                                                                 #
# --------------------------------------------------------------------------- #

class PaymentStatus(str, enum.Enum):
    """Lifecycle states of a failed payment inside the Revora engine."""
    AT_RISK             = "AT_RISK"
    IN_RECOVERY         = "IN_RECOVERY"
    RECOVERED           = "RECOVERED"
    FAILED_EXHAUSTED    = "FAILED_EXHAUSTED"
    STOPPED_COMPLIANCE  = "STOPPED_COMPLIANCE"


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
    """
    __tablename__ = "payment_events"

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
    audit_logs: Mapped[list["RecoveryAuditLog"]] = relationship(
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
    audit_logs: Mapped[list["RecoveryAuditLog"]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<RecoveryWorkflow id={self.id!r} "
            f"strategy={self.strategy!r} "
            f"step={self.current_step}/{self.max_steps} "
            f"active={self.is_active}>"
        )


# --------------------------------------------------------------------------- #
# RecoveryAuditLog                                                             #
# --------------------------------------------------------------------------- #

class RecoveryAuditLog(Base):
    """
    Immutable audit record for every action taken by the recovery engine.

    Captures the reasoning (from the LLM or rule engine), the communication
    channel used, and arbitrary metadata (e.g., Razorpay payment link IDs,
    WhatsApp delivery receipts, voice call durations).
    """
    __tablename__ = "recovery_audit_logs"

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

    # --- Action descriptor ---
    action_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # --- LLM / rule reasoning ---
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
            f"<RecoveryAuditLog id={self.id!r} "
            f"action={self.action_type!r} "
            f"channel={self.channel!r}>"
        )
