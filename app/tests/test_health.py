"""
app/tests/test_health.py
------------------------
Async pytest suite for the /api/v1/health endpoint.

Validates:
  • HTTP 200 status code.
  • Response body conforms to the HealthResponse schema.
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.asyncio
async def test_health_check_status_code() -> None:
    """GET /api/v1/health must return HTTP 200."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/health")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_health_check_schema() -> None:
    """GET /api/v1/health must return the expected JSON schema."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/health")

    body = response.json()
    assert body["status"] == "healthy"
    assert body["service"] == "revora-engine"
    assert body["version"] == "1.0.0"


@pytest.mark.asyncio
async def test_health_check_content_type() -> None:
    """GET /api/v1/health must return JSON content-type."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/health")

    assert "application/json" in response.headers["content-type"]
