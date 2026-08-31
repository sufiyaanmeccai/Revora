"""
app/api/v1/endpoints/recovery.py
----------------------------------
Recovery orchestration endpoints.

Phase 0 stub — AI-driven retry scheduling and customer communication
pipelines will be implemented in Phase 2+.
"""

from fastapi import APIRouter

router = APIRouter(tags=["Recovery"])


@router.get(
    "/recovery/status",
    summary="Recovery engine status",
    description="Returns the current state of the autonomous recovery pipeline.",
)
async def recovery_status() -> dict:
    """Stub — returns a placeholder until the agent pipeline is wired in."""
    return {"pipeline": "pending", "message": "Recovery engine initialising — Phase 2."}
