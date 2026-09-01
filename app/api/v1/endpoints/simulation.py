"""
app/api/v1/endpoints/simulation.py
------------------------------------
Batch simulation endpoints for the Revora Revenue Recovery Engine.

Endpoints:
  POST /api/v1/simulation/run?count=N
      Generates N synthetic PaymentEvent records and queues them for recovery.

  POST /api/v1/simulation/fast-forward   [Phase 7]
      Probabilistically finalises all INTERVENTION_ACTIVE events using the
      Outcome Simulator (65% RECOVERED, 35% escalate/retry).
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.services.decision_engine import process_payment_event
from app.services.outcome_simulator import simulate_active_interventions
from app.services.simulation import generate_synthetic_batch

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/simulation", tags=["Simulation"])


@router.post(
    "/run",
    summary="Run synthetic batch simulation",
    description=(
        "Generates a batch of synthetic failed PaymentEvent records and "
        "queues each one through the autonomous recovery decision engine."
    ),
)
async def run_simulation(
    background_tasks: BackgroundTasks,
    count: int = Query(
        default=50,
        ge=1,
        le=500,
        description="Number of synthetic failed payments to generate.",
    ),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """
    Batch simulation runner.

    Steps:
      1. Generate ``count`` synthetic PaymentEvent records (committed to DB).
      2. Queue ``process_payment_event`` as a background task for each event.
      3. Return immediately with the list of generated event IDs.

    The decision engine runs asynchronously after the 200 OK is returned,
    mirroring production behaviour where Razorpay webhook events are processed
    without blocking the acknowledgement response.
    """
    logger.info("Simulation run requested: count=%d", count)

    # 1. Insert synthetic events
    event_ids = await generate_synthetic_batch(db, count=count)

    # 2. Queue decision engine for each event (self-managed sessions)
    for event_id in event_ids:
        background_tasks.add_task(process_payment_event, event_id)

    logger.info("Simulation: %d events created, decision engine queued.", count)

    return JSONResponse(
        status_code=200,
        content={
            "status":    "batch_started",
            "count":     count,
            "event_ids": event_ids,
        },
    )


@router.post(
    "/fast-forward",
    summary="Fast-forward outcome simulation",
    description=(
        "Probabilistically resolves all INTERVENTION_ACTIVE payment events. "
        "65% are transitioned to RECOVERED (with amount_recovered set). "
        "35% are retried or escalated based on retry_count vs MAX_RETRIES. "
        "Returns the count of resolved (RECOVERED + ESCALATED_STOPPED) workflows."
    ),
)
async def fast_forward_outcomes(
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """
    Outcome Simulator endpoint (Phase 7).

    Simulates the real-world customer response to recovery outreach by
    probabilistically finalising all INTERVENTION_ACTIVE events.

    Returns:
        JSON with total_processed, recovered, escalated, still_active counts.
    """
    logger.info("Fast-forward outcome simulation requested.")

    result = await simulate_active_interventions(db)

    resolved = result["recovered"] + result["escalated"]

    logger.info(
        "Fast-forward complete: processed=%d resolved=%d (recovered=%d, escalated=%d) still_active=%d",
        result["total_processed"],
        resolved,
        result["recovered"],
        result["escalated"],
        result["still_active"],
    )

    return JSONResponse(
        status_code=200,
        content={
            "status":          "outcomes_simulated",
            "resolved":        resolved,
            "total_processed": result["total_processed"],
            "recovered":       result["recovered"],
            "escalated":       result["escalated"],
            "still_active":    result["still_active"],
        },
    )
