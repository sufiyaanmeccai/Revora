"""
app/api/v1/endpoints/health.py
-------------------------------
Health-check endpoint for Revora.

GET /api/v1/health
    Returns a simple JSON payload confirming the service is alive.
"""

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["Health"])


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Service health check",
    description="Returns the operational status of the Revora engine.",
)
async def health_check() -> HealthResponse:
    """Liveness probe — confirms the API is reachable and running."""
    return HealthResponse(
        status="healthy",
        service="revora-engine",
        version="1.0.0",
    )
