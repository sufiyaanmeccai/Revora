"""
app/api/v1/endpoints/webhooks.py
---------------------------------
Razorpay webhook ingestion endpoint for the Revora Revenue Recovery Engine.

Flow for POST /api/v1/webhooks/razorpay:
  1. Read raw request body (must happen before JSON parsing).
  2. Verify HMAC-SHA256 signature from X-Razorpay-Signature header.
  3. Parse JSON payload.
  4. Dispatch on event type:
       • payment.failed          → extract entity, persist PaymentEvent.
       • subscription.halted     → (same extraction path, future workflow hook).
       • anything else           → acknowledge immediately (avoid Razorpay retries).
  5. Attempt DB INSERT wrapped in IntegrityError catch for duplicate detection.
     - Duplicate razorpay_event_id → 200 {"status": "ignored", "reason": "duplicate_event"}.
     - New event → commit, schedule background task, return 200 immediately.
  6. Decision engine runs as a BackgroundTask AFTER the response is sent.

Phase 8A changes:
  • Accept x-razorpay-event-id header and store it on PaymentEvent.
  • Wrap db.add + commit in try/except sqlalchemy.exc.IntegrityError for
    DB-level idempotency (unique constraint on razorpay_event_id).
  • Endpoint NEVER awaits recovery logic — all recovery is a BackgroundTask.
"""

import json
import logging
import uuid
from typing import Any, Dict, Optional

import sqlalchemy.exc
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.security import verify_webhook_signature
from app.models.orm import PaymentEvent, PaymentStatus
from app.services.decision_engine import process_payment_event

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Webhooks"])

# ---------------------------------------------------------------------------
# Events this handler actively processes (others are acknowledged but ignored)
# ---------------------------------------------------------------------------
HANDLED_EVENTS = {"payment.failed", "subscription.halted"}


# ---------------------------------------------------------------------------
# Helper: extract PaymentEvent fields from a Razorpay payment entity
# ---------------------------------------------------------------------------
def _build_payment_event(
    payment_entity: Dict[str, Any],
    raw_body: str,
    razorpay_event_id: Optional[str] = None,
) -> PaymentEvent:
    """
    Map a Razorpay ``payment`` entity dict to a ``PaymentEvent`` ORM instance.

    Phase 8A: accepts ``razorpay_event_id`` from the x-razorpay-event-id header
    and stores it on the model for DB-level idempotency enforcement.

    Customer identity is extracted from:
      1. Top-level fields on the entity (``email``, ``contact``).
      2. The ``notes`` dict (if the merchant embeds custom fields there).
      3. Safe defaults when the field is absent.
    """
    notes: Dict[str, Any] = payment_entity.get("notes") or {}

    # -- Amount: Razorpay sends paise (integer), convert to INR (float) ------
    amount_paise: int = payment_entity.get("amount", 0)
    amount_inr: float = amount_paise / 100.0

    # -- Customer fields -------------------------------------------------------
    customer_id: str = (
        payment_entity.get("customer_id")
        or notes.get("customer_id")
        or f"cust_{uuid.uuid4().hex[:8]}"          # generate if missing
    )
    customer_name: str = (
        notes.get("customer_name")
        or notes.get("name")
        or "Unknown Customer"
    )
    customer_email: str = (
        payment_entity.get("email")
        or notes.get("email")
        or "unknown@revora.ai"
    )
    customer_contact: str = (
        payment_entity.get("contact")
        or notes.get("contact")
        or ""
    )

    return PaymentEvent(
        id=str(uuid.uuid4()),
        # Phase 8A: native event ID for idempotency
        razorpay_event_id=razorpay_event_id,
        razorpay_payment_id=payment_entity.get("id"),
        razorpay_order_id=payment_entity.get("order_id"),
        razorpay_subscription_id=payment_entity.get("subscription_id"),
        customer_id=customer_id,
        customer_name=customer_name,
        customer_email=customer_email,
        customer_contact=customer_contact,
        amount=amount_inr,
        currency=payment_entity.get("currency", "INR"),
        error_code=payment_entity.get("error_code") or "UNKNOWN",
        error_description=payment_entity.get("error_description") or "",
        error_source=payment_entity.get("error_source") or "",
        error_reason=payment_entity.get("error_reason") or "",
        status=PaymentStatus.AT_RISK,
        raw_payload=raw_body,
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------
@router.post(
    "/webhooks/razorpay",
    status_code=status.HTTP_200_OK,
    summary="Razorpay webhook receiver",
    description=(
        "Ingests Razorpay payment lifecycle events. "
        "Verifies HMAC-SHA256 signature before processing. "
        "Duplicate events (same x-razorpay-event-id) are deduplicated via "
        "DB unique constraint and return 200 immediately."
    ),
)
async def razorpay_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_razorpay_signature: str = Header(
        ...,
        alias="x-razorpay-signature",
        description="HMAC-SHA256 hex digest sent by Razorpay.",
    ),
    x_razorpay_event_id: Optional[str] = Header(
        default=None,
        alias="x-razorpay-event-id",
        description="Razorpay's unique event ID for idempotency deduplication.",
    ),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """
    Razorpay webhook ingestion handler (Phase 8A).

    Steps:
      1. Read raw body.
      2. Verify signature (400 on failure).
      3. Parse event type.
      4. For payment.failed / subscription.halted: build PaymentEvent.
      5. INSERT with IntegrityError guard:
           - IntegrityError (duplicate razorpay_event_id) → rollback, 200 "ignored".
           - Success → commit, queue BackgroundTask, return 200 immediately.

    The endpoint NEVER awaits recovery logic — the HTTP 200 is sent before
    any outreach or AI decision-making runs, preventing Razorpay gateway timeouts.
    """
    # ------------------------------------------------------------------ #
    # 1. Read raw body                                                     #
    # ------------------------------------------------------------------ #
    body: bytes = await request.body()

    # ------------------------------------------------------------------ #
    # 2. Verify HMAC-SHA256 signature                                      #
    # ------------------------------------------------------------------ #
    if not verify_webhook_signature(body, x_razorpay_signature, settings.RAZORPAY_WEBHOOK_SECRET):
        logger.warning(
            "Razorpay webhook signature verification FAILED. "
            "Received signature: %s",
            x_razorpay_signature,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook signature.",
        )

    # ------------------------------------------------------------------ #
    # 3. Parse JSON                                                        #
    # ------------------------------------------------------------------ #
    try:
        payload: Dict[str, Any] = json.loads(body)
    except json.JSONDecodeError as exc:
        logger.error("Webhook payload is not valid JSON: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payload is not valid JSON.",
        )

    event_type: str = payload.get("event", "")
    logger.info("Received Razorpay webhook event: %r (event_id=%s)", event_type, x_razorpay_event_id)

    # ------------------------------------------------------------------ #
    # 4. Dispatch: ignore unhandled events with a quick 200               #
    # ------------------------------------------------------------------ #
    if event_type not in HANDLED_EVENTS:
        logger.debug("Unhandled event type %r — acknowledging without processing.", event_type)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "ignored", "event": event_type},
        )

    # ------------------------------------------------------------------ #
    # 5. Extract payment entity                                            #
    # ------------------------------------------------------------------ #
    try:
        payment_entity: Dict[str, Any] = payload["payload"]["payment"]["entity"]
    except (KeyError, TypeError) as exc:
        logger.error(
            "Malformed payload for event %r — missing payment entity: %s",
            event_type,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed payload: payment entity not found.",
        )

    # ------------------------------------------------------------------ #
    # 6. Build PaymentEvent ORM instance                                  #
    # ------------------------------------------------------------------ #
    event = _build_payment_event(
        payment_entity,
        body.decode("utf-8", errors="replace"),
        razorpay_event_id=x_razorpay_event_id,
    )

    # ------------------------------------------------------------------ #
    # 7. INSERT with DB-level idempotency guard                           #
    #                                                                     #
    # The UNIQUE constraint on razorpay_event_id means that a duplicate   #
    # webhook delivery will raise IntegrityError on commit. We catch it,  #
    # rollback, and return 200 so Razorpay stops retrying.               #
    # ------------------------------------------------------------------ #
    try:
        db.add(event)
        await db.commit()
        await db.refresh(event)
    except sqlalchemy.exc.IntegrityError:
        await db.rollback()
        logger.info(
            "Duplicate webhook event detected and deduplicated: event_id=%s",
            x_razorpay_event_id,
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "ignored", "reason": "duplicate_event"},
        )

    logger.info(
        "PaymentEvent persisted: id=%s razorpay_event_id=%s customer=%s amount=%.2f INR status=%s",
        event.id,
        x_razorpay_event_id,
        event.customer_email,
        event.amount,
        event.status,
    )

    # ------------------------------------------------------------------ #
    # 8. Schedule decision engine as a background task                    #
    # The HTTP 200 response is sent to Razorpay BEFORE this runs.        #
    # The engine opens its own session — safe after this request closes.  #
    # ------------------------------------------------------------------ #
    background_tasks.add_task(process_payment_event, event.id)
    logger.info("Decision engine queued for PaymentEvent '%s'.", event.id)

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "success", "event_id": event.id},
    )
