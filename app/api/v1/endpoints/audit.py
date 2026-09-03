"""
app/api/v1/endpoints/audit.py
------------------------------
Audit Log Export Endpoints for the Revora Revenue Recovery Engine (Phase 11).

Provides a read-only CSV export of the InterventionAuditLog table for compliance,
hackathon evaluation, and operational auditing without exposing PII or secret credentials.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Any, List

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.orm import InterventionAuditLog

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/audit", tags=["Auditability & Compliance"])

CSV_HEADERS: List[str] = [
    "id",
    "timestamp",
    "workflow_id",
    "payment_event_id",
    "executed_strategy",
    "ai_recommended_strategy",
    "ai_confidence",
    "guardrail_decision",
    "channel",
    "intervention_cost",
    "net_recovery_value",
    "reasoning",
]


@router.get(
    "/export",
    summary="Export Append-Only Audit Trail as CSV",
    response_class=Response,
    responses={
        200: {
            "content": {"text/csv": {}},
            "description": "Returns a full CSV export of the append-only InterventionAuditLog table.",
        }
    },
)
async def export_audit_logs(
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Generate and stream a deterministic CSV export of all intervention audit records.

    Features:
      • Non-PII & Secret-Free: Exports strategy routing, AI confidence, guardrail outcomes,
        and unit economic metadata without exposing customer PII or API credentials.
      • Deterministic Column Ordering: Matches standard compliance audit schemas.
      • Append-Only History: Reflects the immutable sequence of engine actions.
    """
    result = await db.execute(
        select(InterventionAuditLog).order_by(InterventionAuditLog.timestamp.asc())
    )
    logs = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL)

    # Write header row
    writer.writerow(CSV_HEADERS)

    # Write data rows
    for log in logs:
        ts_str = log.timestamp.isoformat() if log.timestamp else ""
        conf_str = f"{log.ai_confidence:.4f}" if log.ai_confidence is not None else ""
        cost_str = f"{log.intervention_cost:.2f}" if log.intervention_cost is not None else "0.00"
        net_str = f"{log.net_recovery_value:.2f}" if log.net_recovery_value is not None else "0.00"

        writer.writerow([
            log.id,
            ts_str,
            log.workflow_id,
            log.payment_event_id,
            log.executed_strategy or "",
            log.ai_recommended_strategy or "",
            conf_str,
            log.guardrail_decision or "",
            log.channel or "SYSTEM",
            cost_str,
            net_str,
            log.reasoning or "",
        ])

    csv_content = output.getvalue()
    logger.info("Exported %d audit log records as CSV.", len(logs))

    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="revora_audit_export.csv"',
            "Cache-Control": "no-store",
        },
        status_code=status.HTTP_200_OK,
    )
