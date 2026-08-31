"""
app/api/v1/endpoints/webhooks.py
---------------------------------
Razorpay webhook ingestion endpoint.

Phase 0 stub — full event parsing and recovery orchestration will be
added in Phase 1.
"""

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

router = APIRouter(tags=["Webhooks"])


@router.post(
    "/webhooks/razorpay",
    status_code=status.HTTP_200_OK,
    summary="Razorpay webhook receiver",
    description="Ingests payment lifecycle events from Razorpay.",
)
async def razorpay_webhook(request: Request) -> JSONResponse:
    """Stub receiver — signature verification and event dispatch in Phase 1."""
    return JSONResponse(content={"received": True})
