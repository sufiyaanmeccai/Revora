"""
app/services/metrics.py
------------------------
Recovery metrics aggregation service for the Revora Revenue Recovery Engine.

Computes dashboard-ready metrics by aggregating across PaymentEvent statuses,
RecoveryWorkflow diagnoses/strategies, and RecoveryAuditLog action types.

Returned dict matches the ``RecoverySummaryStats`` Pydantic schema so it can
be serialised directly by the /api/v1/metrics endpoint.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.orm import (
    PaymentEvent,
    PaymentStatus,
    RecoveryAuditLog,
    RecoveryWorkflow,
)

logger = logging.getLogger(__name__)


async def calculate_recovery_metrics(db: AsyncSession) -> Dict[str, Any]:
    """
    Aggregate recovery performance metrics from the database.

    Queries:
      1. PaymentEvent status counts and amount sums.
      2. RecoveryWorkflow grouped by diagnosed_cause.
      3. RecoveryWorkflow grouped by strategy.
      4. RecoveryAuditLog action-type breakdown.

    Returns:
        A dictionary matching the ``RecoverySummaryStats`` schema, extended
        with ``cause_breakdown`` and ``strategy_breakdown`` dicts for
        internal use (filtered by Pydantic on the API layer).
    """
    # ── 1. Status counts ─────────────────────────────────────────────────────
    async def _count_status(status: PaymentStatus) -> int:
        r = await db.execute(
            select(func.count(PaymentEvent.id)).where(PaymentEvent.status == status)
        )
        return r.scalar() or 0

    async def _sum_amount(status: PaymentStatus) -> float:
        r = await db.execute(
            select(func.coalesce(func.sum(PaymentEvent.amount), 0.0)).where(
                PaymentEvent.status == status
            )
        )
        return float(r.scalar() or 0.0)

    total_at_risk     = await _count_status(PaymentStatus.AT_RISK)
    total_in_recovery = await _count_status(PaymentStatus.IN_RECOVERY)
    total_recovered   = await _count_status(PaymentStatus.RECOVERED)
    total_failed      = await _count_status(PaymentStatus.FAILED_EXHAUSTED)
    total_stopped     = await _count_status(PaymentStatus.STOPPED_COMPLIANCE)

    total_amount_at_risk = await _sum_amount(PaymentStatus.AT_RISK)
    recovered_amount     = await _sum_amount(PaymentStatus.RECOVERED)

    # ── 2. Recovery rate ─────────────────────────────────────────────────────
    denominator = total_recovered + total_failed
    recovery_rate_pct = (total_recovered / denominator * 100.0) if denominator > 0 else 0.0

    # ── 3. Cause breakdown (RecoveryWorkflow.diagnosed_cause) ────────────────
    cause_result = await db.execute(
        select(
            RecoveryWorkflow.diagnosed_cause,
            func.count(RecoveryWorkflow.id).label("count"),
        ).group_by(RecoveryWorkflow.diagnosed_cause)
    )
    cause_breakdown: Dict[str, int] = {
        row.diagnosed_cause: row.count
        for row in cause_result.fetchall()
        if row.diagnosed_cause is not None
    }

    # ── 4. Strategy breakdown (RecoveryWorkflow.strategy) ────────────────────
    strategy_result = await db.execute(
        select(
            RecoveryWorkflow.strategy,
            func.count(RecoveryWorkflow.id).label("count"),
        ).group_by(RecoveryWorkflow.strategy)
    )
    strategy_breakdown: Dict[str, int] = {
        row.strategy: row.count
        for row in strategy_result.fetchall()
        if row.strategy is not None
    }

    # ── 5. Action-type breakdown (RecoveryAuditLog.action_type) ──────────────
    action_result = await db.execute(
        select(
            RecoveryAuditLog.action_type,
            func.count(RecoveryAuditLog.id).label("count"),
        ).group_by(RecoveryAuditLog.action_type)
    )
    action_breakdowns: List[Dict[str, Any]] = [
        {"action_type": row.action_type, "count": row.count}
        for row in action_result.fetchall()
        if row.action_type is not None
    ]

    logger.info(
        "Metrics: at_risk=%d | in_recovery=%d | recovered=%d | failed=%d | "
        "rate=%.1f%% | amount_at_risk=%.2f INR",
        total_at_risk,
        total_in_recovery,
        total_recovered,
        total_failed,
        recovery_rate_pct,
        total_amount_at_risk,
    )

    return {
        # RecoverySummaryStats fields
        "total_at_risk":        total_at_risk,
        "total_in_recovery":    total_in_recovery,
        "total_recovered":      total_recovered,
        "total_failed":         total_failed,
        "total_stopped":        total_stopped,
        "recovery_rate_pct":    round(recovery_rate_pct, 2),
        "total_amount_at_risk": round(total_amount_at_risk, 2),
        "recovered_amount":     round(recovered_amount, 2),
        "action_breakdowns":    action_breakdowns,
        # Extended fields (filtered by Pydantic on the API response layer)
        "cause_breakdown":      cause_breakdown,
        "strategy_breakdown":   strategy_breakdown,
    }
