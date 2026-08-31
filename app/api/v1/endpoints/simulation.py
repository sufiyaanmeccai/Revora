"""
app/api/v1/endpoints/simulation.py
------------------------------------
Batch simulation endpoint for the Revora Revenue Recovery Engine.

Allows on-demand generation of synthetic failed payment events and kicks off
the decision engine pipeline for each one as a background task.

POST /api/v1/simulation/run?count=N
    Generates N synthetic PaymentEvent records and queues them for recovery.
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.services.decision_engine import process_payment_event
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
