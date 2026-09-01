"""
app/services/metrics.py
------------------------
Recovery metrics aggregation service for the Revora Revenue Recovery Engine.

Phase 7 changes:
  • Revenue/Events "At Risk" = AT_RISK + DIAGNOSED + INTERVENTION_ACTIVE
    (all non-terminal, in-flight states).
  • Revenue Recovered = sum of amount_recovered where status = RECOVERED.
  • Recovery rate = Recovered / (At Risk + Recovered) * 100 (safe division).
  • New state fields: total_diagnosed, total_intervention, total_escalated.
  • total_in_recovery = DIAGNOSED + INTERVENTION_ACTIVE (for dashboard display).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.orm import (
    InterventionAuditLog,
    PaymentEvent,
    PaymentStatus,
    RecoveryWorkflow,
)

logger = logging.getLogger(__name__)


async def calculate_recovery_metrics(db: AsyncSession) -> Dict[str, Any]:
    """
    Aggregate recovery performance metrics from the database.

    Phase 7 state model:
      In-flight (at risk of loss):  AT_RISK, DIAGNOSED, INTERVENTION_ACTIVE
      Terminal success:              RECOVERED
      Terminal failure:              ESCALATED_STOPPED

    Queries:
      1. PaymentEvent status counts.
      2. Amount sums — uses amount_recovered for RECOVERED events.
      3. RecoveryWorkflow grouped by diagnosed_cause.
      4. RecoveryWorkflow grouped by strategy.
      5. InterventionAuditLog executed_strategy breakdown.

    Returns:
        A dictionary matching the ``RecoverySummaryStats`` schema.
    """
    # ── 1. Status counts ──────────────────────────────────────────────────────
    async def _count_status(status: PaymentStatus) -> int:
        r = await db.execute(
            select(func.count(PaymentEvent.id)).where(PaymentEvent.status == status)
        )
        return r.scalar() or 0

    total_at_risk      = await _count_status(PaymentStatus.AT_RISK)
    total_diagnosed    = await _count_status(PaymentStatus.DIAGNOSED)
    total_intervention = await _count_status(PaymentStatus.INTERVENTION_ACTIVE)
    total_recovered    = await _count_status(PaymentStatus.RECOVERED)
    total_escalated    = await _count_status(PaymentStatus.ESCALATED_STOPPED)

    # Combined "in recovery" for backward-compatible dashboard display
    total_in_recovery = total_diagnosed + total_intervention

    # ── 2. Amount sums ────────────────────────────────────────────────────────
    async def _sum_amount(status: PaymentStatus) -> float:
        r = await db.execute(
            select(func.coalesce(func.sum(PaymentEvent.amount), 0.0)).where(
                PaymentEvent.status == status
            )
        )
        return float(r.scalar() or 0.0)

    async def _sum_recovered_amount() -> float:
        """Sum of amount_recovered for RECOVERED events (actual captured revenue)."""
        r = await db.execute(
            select(func.coalesce(func.sum(RecoveryWorkflow.amount_recovered), 0.0))
            .join(PaymentEvent, RecoveryWorkflow.payment_event_id == PaymentEvent.id)
            .where(PaymentEvent.status == PaymentStatus.RECOVERED)
        )
        return float(r.scalar() or 0.0)

    # Total amount at risk = sum of all in-flight event amounts
    at_risk_amount       = await _sum_amount(PaymentStatus.AT_RISK)
    diagnosed_amount     = await _sum_amount(PaymentStatus.DIAGNOSED)
    intervention_amount  = await _sum_amount(PaymentStatus.INTERVENTION_ACTIVE)
    total_amount_at_risk = at_risk_amount + diagnosed_amount + intervention_amount

    # Actual recovered revenue (from amount_recovered field, not raw amount)
    recovered_amount = await _sum_recovered_amount()

    # ── 3. Recovery rate ──────────────────────────────────────────────────────
    # Recovery rate = Recovered / (All in-flight + Recovered) * 100
    # "All in-flight" counts as denominator so rate reflects true pipeline health
    denominator = (total_at_risk + total_in_recovery + total_recovered)
    recovery_rate_pct = (
        (total_recovered / denominator * 100.0) if denominator > 0 else 0.0
    )

    # ── 4. Cause breakdown (RecoveryWorkflow.diagnosed_cause) ─────────────────
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

    # ── 5. Strategy breakdown (RecoveryWorkflow.strategy) ─────────────────────
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

    # ── 6. Executed-strategy breakdown (InterventionAuditLog.executed_strategy) ────
    action_result = await db.execute(
        select(
            InterventionAuditLog.executed_strategy,
            func.count(InterventionAuditLog.id).label("count"),
        ).group_by(InterventionAuditLog.executed_strategy)
    )
    action_breakdowns: List[Dict[str, Any]] = [
        {"action_type": row.executed_strategy, "count": row.count}
        for row in action_result.fetchall()
        if row.executed_strategy is not None
    ]

    logger.info(
        "Metrics: at_risk=%d | diagnosed=%d | intervention=%d | recovered=%d | "
        "escalated=%d | rate=%.1f%% | amount_at_risk=%.2f | recovered_amount=%.2f INR",
        total_at_risk,
        total_diagnosed,
        total_intervention,
        total_recovered,
        total_escalated,
        recovery_rate_pct,
        total_amount_at_risk,
        recovered_amount,
    )

    return {
        # Phase 7 state counts
        "total_at_risk":        total_at_risk,
        "total_diagnosed":      total_diagnosed,
        "total_intervention":   total_intervention,
        "total_recovered":      total_recovered,
        "total_escalated":      total_escalated,
        # Aggregate for dashboard display (backward-compatible "in recovery" label)
        "total_in_recovery":    total_in_recovery,
        # Revenue metrics
        "recovery_rate_pct":    round(recovery_rate_pct, 2),
        "total_amount_at_risk": round(total_amount_at_risk, 2),
        "recovered_amount":     round(recovered_amount, 2),
        # Breakdowns
        "action_breakdowns":    action_breakdowns,
        "cause_breakdown":      cause_breakdown,
        "strategy_breakdown":   strategy_breakdown,
    }
