"""
app/services/outcome_simulator.py
-----------------------------------
Outcome Simulator for the Revora Revenue Recovery Engine (Phase 7).

Probabilistically finalises recovery workflows that are currently in the
INTERVENTION_ACTIVE state, simulating real-world customer payment behaviour
after an outreach intervention.

Probability model (calibrated against SaaS recovery industry benchmarks):
  • 65% → Payment captured (RECOVERED):
      Simulates the customer clicking the payment link / mandate retry succeeding.
  • 35% → Timeout / non-response (ESCALATED or retry):
      Simulates the customer ignoring the outreach or payment still failing.
      If retry_count < MAX_RETRIES → stay INTERVENTION_ACTIVE (increments retry_count).
      If retry_count >= MAX_RETRIES → ESCALATED_STOPPED (terminal).

This simulator is invoked by the POST /api/v1/simulation/fast-forward endpoint
to demonstrate measurable revenue capture in a single-click demo flow.
"""

from __future__ import annotations

import json
import logging
import random
import uuid
from datetime import datetime, timezone
from typing import Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.policies import MAX_RETRIES
from app.models.orm import (
    InterventionAuditLog,
    PaymentEvent,
    PaymentStatus,
    RecoveryWorkflow,
)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Probability constants                                                        #
# --------------------------------------------------------------------------- #

SUCCESS_PROBABILITY: float = 0.65
"""Probability that an INTERVENTION_ACTIVE event resolves as RECOVERED."""


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #

async def simulate_active_interventions(db: AsyncSession) -> Dict[str, int]:
    """
    Probabilistically finalise all INTERVENTION_ACTIVE payment events.

    For each event:
      • 65%: RECOVERED — set workflow.amount_recovered = event.amount, write success audit.
      • 35%: Failure/Timeout — increment retry_count.
             If < MAX_RETRIES: stay INTERVENTION_ACTIVE (will be retried next fast-forward).
             If >= MAX_RETRIES: ESCALATED_STOPPED, write escalation audit.

    Args:
        db: Active async database session (caller manages lifecycle).

    Returns:
        Dict with keys:
          "total_processed"   — events evaluated
          "recovered"         — events transitioned to RECOVERED
          "escalated"         — events transitioned to ESCALATED_STOPPED
          "still_active"      — events kept in INTERVENTION_ACTIVE (more retries pending)
    """
    # ── 1. Query all INTERVENTION_ACTIVE events with their workflows ──────────
    events_result = await db.execute(
        select(PaymentEvent).where(
            PaymentEvent.status == PaymentStatus.INTERVENTION_ACTIVE
        )
    )
    events: list[PaymentEvent] = list(events_result.scalars().all())

    if not events:
        logger.info("Outcome simulator: no INTERVENTION_ACTIVE events to process.")
        return {"total_processed": 0, "recovered": 0, "escalated": 0, "still_active": 0}

    logger.info(
        "Outcome simulator: processing %d INTERVENTION_ACTIVE events.", len(events)
    )

    counts = {"recovered": 0, "escalated": 0, "still_active": 0}

    for event in events:
        # ── 2. Fetch the associated active workflow ───────────────────────────
        wf_result = await db.execute(
            select(RecoveryWorkflow).where(
                RecoveryWorkflow.payment_event_id == event.id,
                RecoveryWorkflow.is_active.is_(True),
            )
        )
        workflow: RecoveryWorkflow | None = wf_result.scalar_one_or_none()

        if workflow is None:
            logger.warning(
                "Outcome simulator: no active workflow found for event %s — skipping.",
                event.id,
            )
            continue

        # ── 3. Roll the probability dice ──────────────────────────────────────
        roll = random.random()
        now  = datetime.now(timezone.utc)

        if roll < SUCCESS_PROBABILITY:
            # ── SUCCESS PATH (65%) ────────────────────────────────────────────
            event.status              = PaymentStatus.RECOVERED
            workflow.amount_recovered = event.amount
            workflow.is_active        = False
            workflow.resolved_at      = now

            audit = InterventionAuditLog(
                id=str(uuid.uuid4()),
                workflow_id=workflow.id,
                payment_event_id=event.id,
                executed_strategy="OUTCOME_SIMULATED_SUCCESS",
                ai_recommended_strategy=workflow.strategy.value,
                ai_confidence=1.0,
                ai_reasoning=f"Simulated payment.captured webhook received for {workflow.strategy.value}.",
                guardrail_decision="APPROVED",
                reasoning=(
                    "Simulated payment.captured webhook received. "
                    f"Customer responded to {workflow.strategy.value} outreach. "
                    f"Amount captured: ₹{event.amount:.2f}. "
                    "Event transitioned to RECOVERED."
                ),
                channel="SYSTEM",
                metadata_json=json.dumps({
                    "simulation_roll":      round(roll, 4),
                    "success_probability":  SUCCESS_PROBABILITY,
                    "outcome":              "RECOVERED",
                    "amount_recovered":     event.amount,
                    "strategy":             workflow.strategy.value,
                    "retry_count_at_close": workflow.retry_count,
                }),
                timestamp=now,
            )
            db.add(audit)
            counts["recovered"] += 1

            logger.info(
                "Outcome simulator: event %s → RECOVERED (₹%.2f captured).",
                event.id,
                event.amount,
            )

        else:
            # ── FAILURE / TIMEOUT PATH (35%) ──────────────────────────────────
            workflow.retry_count += 1

            if workflow.retry_count >= MAX_RETRIES:
                # Hard stop — max retries exhausted
                event.status       = PaymentStatus.ESCALATED_STOPPED
                workflow.is_active = False
                workflow.resolved_at = now

                audit = InterventionAuditLog(
                    id=str(uuid.uuid4()),
                    workflow_id=workflow.id,
                    payment_event_id=event.id,
                    executed_strategy="ESCALATED_STOPPED",
                    ai_recommended_strategy=workflow.strategy.value,
                    ai_confidence=1.0,
                    ai_reasoning=f"Recovery attempts exhausted ({workflow.retry_count}/{MAX_RETRIES}).",
                    guardrail_decision="APPROVED",
                    reasoning=(
                        f"Recovery attempts exhausted: retry_count={workflow.retry_count} "
                        f">= MAX_RETRIES={MAX_RETRIES}. "
                        "Customer did not respond to any outreach. "
                        "Event transitioned to ESCALATED_STOPPED for manual review."
                    ),
                    channel="SYSTEM",
                    metadata_json=json.dumps({
                        "simulation_roll":     round(roll, 4),
                        "success_probability": SUCCESS_PROBABILITY,
                        "outcome":             "ESCALATED_STOPPED",
                        "retry_count":         workflow.retry_count,
                        "max_retries":         MAX_RETRIES,
                        "strategy":            workflow.strategy.value,
                    }),
                    timestamp=now,
                )
                db.add(audit)
                counts["escalated"] += 1

                logger.info(
                    "Outcome simulator: event %s → ESCALATED_STOPPED (retries=%d).",
                    event.id,
                    workflow.retry_count,
                )

            else:
                # Still within retry budget — keep INTERVENTION_ACTIVE
                audit = InterventionAuditLog(
                    id=str(uuid.uuid4()),
                    workflow_id=workflow.id,
                    payment_event_id=event.id,
                    executed_strategy="OUTCOME_SIMULATED_FAILURE",
                    ai_recommended_strategy=workflow.strategy.value,
                    ai_confidence=1.0,
                    ai_reasoning=f"Simulated outreach timeout. retry_count now {workflow.retry_count}/{MAX_RETRIES}.",
                    guardrail_decision="APPROVED",
                    reasoning=(
                        f"Simulated outreach timeout. Customer did not respond. "
                        f"retry_count now {workflow.retry_count}/{MAX_RETRIES}. "
                        "Event remains INTERVENTION_ACTIVE for next retry window."
                    ),
                    channel="SYSTEM",
                    metadata_json=json.dumps({
                        "simulation_roll":     round(roll, 4),
                        "success_probability": SUCCESS_PROBABILITY,
                        "outcome":             "TIMEOUT_RETRY",
                        "retry_count":         workflow.retry_count,
                        "max_retries":         MAX_RETRIES,
                        "strategy":            workflow.strategy.value,
                    }),
                    timestamp=now,
                )
                db.add(audit)
                counts["still_active"] += 1

                logger.info(
                    "Outcome simulator: event %s timeout (retry %d/%d), stays INTERVENTION_ACTIVE.",
                    event.id,
                    workflow.retry_count,
                    MAX_RETRIES,
                )

    await db.commit()

    result = {
        "total_processed": len(events),
        **counts,
    }

    logger.info(
        "Outcome simulator complete: total=%d recovered=%d escalated=%d still_active=%d",
        result["total_processed"],
        result["recovered"],
        result["escalated"],
        result["still_active"],
    )

    return result
