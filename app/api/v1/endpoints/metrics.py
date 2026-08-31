"""
app/api/v1/endpoints/metrics.py
---------------------------------
Recovery metrics API endpoint for the Revora Revenue Recovery Engine.

GET /api/v1/metrics
    Returns aggregated recovery performance statistics validated against
    the RecoverySummaryStats Pydantic schema.
"""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.schemas import RecoverySummaryStats
from app.services.metrics import calculate_recovery_metrics

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/metrics", tags=["Metrics"])


@router.get(
    "",
    response_model=RecoverySummaryStats,
    summary="Recovery metrics dashboard",
    description=(
        "Returns aggregated recovery performance statistics: status counts, "
        "revenue at risk, recovery rate, and per-action-type breakdowns."
    ),
)
async def get_metrics(
    db: AsyncSession = Depends(get_db),
) -> RecoverySummaryStats:
    """
    Aggregate and return recovery engine performance metrics.

    Data sources:
      • PaymentEvent   — status counts and amount sums.
      • RecoveryWorkflow — cause and strategy distribution.
      • RecoveryAuditLog — action-type execution breakdown.
    """
    metrics_dict = await calculate_recovery_metrics(db)
    return RecoverySummaryStats(**metrics_dict)
