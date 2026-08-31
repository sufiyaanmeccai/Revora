"""
app/models/schemas.py
----------------------
Shared Pydantic v2 schemas used across the Revora API.

Phase 0 defines only the foundational response envelopes; domain-specific
models (Payment, Subscription, RecoveryJob, etc.) will be added in Phase 1+.
"""

from datetime import datetime
from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel, Field

DataT = TypeVar("DataT")


class APIResponse(BaseModel, Generic[DataT]):
    """Generic JSON envelope used by all Revora endpoints."""

    success: bool = True
    data: Optional[DataT] = None
    message: str = "OK"
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class ErrorDetail(BaseModel):
    """Structured error payload."""

    code: str
    message: str
    details: Optional[Any] = None
