"""
app/tests/test_phase11_audit_export.py
--------------------------------------
Comprehensive test suite for Phase 11:
Audit Export, Reproducibility & Compliance Inspection.

Test Coverage:
  1. GET /api/v1/audit/export CSV formatting and headers.
  2. Empty database CSV generation (header-only).
  3. Populated database CSV row serialization (strategies, costs, net values).
  4. PII isolation (verifies customer names, emails, phones never leak into audit CSV).
"""

from __future__ import annotations

import csv
import io
import uuid
from typing import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.api.v1.endpoints.audit import CSV_HEADERS
from app.core.database import get_db
from app.main import app
from app.models.orm import (
    Base,
    InterventionAuditLog,
    PaymentEvent,
    PaymentStatus,
    RecoveryStrategy,
)

# --------------------------------------------------------------------------- #
# In-memory test database                                                     #
# --------------------------------------------------------------------------- #
TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

_test_engine = create_async_engine(
    TEST_DB_URL,
    echo=False,
    connect_args={"check_same_thread": False},
)

_TestSession: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=_test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


@pytest.fixture(autouse=True)
async def setup_db():
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    async with _TestSession() as session:
        yield session


@pytest.fixture
def override_db(db_session: AsyncSession):
    async def _override():
        yield db_session

    app.dependency_overrides[get_db] = _override
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_audit_export_csv_headers_empty_state(override_db):
    """GET /api/v1/audit/export on empty database returns 200 with exact CSV headers."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/v1/audit/export")

    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert 'attachment; filename="revora_audit_export.csv"' in resp.headers["content-disposition"]

    reader = csv.reader(io.StringIO(resp.text))
    rows = list(reader)
    assert len(rows) == 1
    assert rows[0] == CSV_HEADERS


@pytest.mark.asyncio
async def test_audit_export_populated_rows(override_db, db_session: AsyncSession):
    """GET /api/v1/audit/export returns full append-only audit trail with economic columns."""
    event_id = str(uuid.uuid4())
    wf_id = str(uuid.uuid4())

    log1 = InterventionAuditLog(
        workflow_id=wf_id,
        payment_event_id=event_id,
        executed_strategy=RecoveryStrategy.SECURE_PAYMENT_LINK.value,
        ai_recommended_strategy="SECURE_PAYMENT_LINK",
        ai_confidence=0.95,
        guardrail_decision="APPROVED",
        channel="WHATSAPP",
        intervention_cost=2.50,
        net_recovery_value=4997.50,
        reasoning="VIP customer recovery link dispatched.",
        metadata_json='{"tier": "HIGH", "tenure": 24}',
    )
    log2 = InterventionAuditLog(
        workflow_id=wf_id,
        payment_event_id=event_id,
        executed_strategy=RecoveryStrategy.SILENT_MANDATE_RETRY.value,
        ai_recommended_strategy="ADAPTIVE_DOWNGRADE_OFFER",
        ai_confidence=0.80,
        guardrail_decision="OVERRIDDEN (RULE1B_TICKET_SIZE_BLOCK)",
        channel="SYSTEM",
        intervention_cost=0.00,
        net_recovery_value=199.00,
        reasoning="Micro ticket downgrade overridden to silent mandate retry.",
        metadata_json='{"tier": "LOW", "tenure": 6}',
    )

    db_session.add(log1)
    db_session.add(log2)
    await db_session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/v1/audit/export")

    assert resp.status_code == 200
    reader = csv.reader(io.StringIO(resp.text))
    rows = list(reader)
    assert len(rows) == 3  # Header + 2 data rows

    header = rows[0]
    strat_idx = header.index("executed_strategy")
    cost_idx = header.index("intervention_cost")
    net_idx = header.index("net_recovery_value")

    assert rows[1][strat_idx] == "SECURE_PAYMENT_LINK"
    assert rows[1][cost_idx] == "2.50"
    assert rows[1][net_idx] == "4997.50"

    assert rows[2][strat_idx] == "SILENT_MANDATE_RETRY"
    assert rows[2][cost_idx] == "0.00"
    assert rows[2][net_idx] == "199.00"


@pytest.mark.asyncio
async def test_audit_export_zero_pii_leakage(override_db, db_session: AsyncSession):
    """Assert customer personal data and metadata_json never leak into exported CSV."""
    assert "metadata_json" not in CSV_HEADERS

    event = PaymentEvent(
        id=str(uuid.uuid4()),
        customer_id="cust_pii_test",
        customer_name="Confidential Customer",
        customer_email="topsecret@enterprise.com",
        customer_contact="+919876543210",
        amount=15000.0,
        status=PaymentStatus.AT_RISK,
    )
    db_session.add(event)

    # Log with outreach metadata containing sensitive contact details in DB
    log = InterventionAuditLog(
        workflow_id=str(uuid.uuid4()),
        payment_event_id=event.id,
        executed_strategy="SECURE_PAYMENT_LINK",
        ai_recommended_strategy="SECURE_PAYMENT_LINK",
        ai_confidence=0.99,
        guardrail_decision="APPROVED",
        channel="WHATSAPP",
        intervention_cost=2.50,
        net_recovery_value=14997.50,
        reasoning="Payment link generated.",
        metadata_json='{"recipient": "+919876543210", "message": "Hi Confidential Customer", "email": "topsecret@enterprise.com"}',
    )
    db_session.add(log)
    await db_session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/v1/audit/export")

    csv_text = resp.text
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)

    # Confirm metadata_json is absent from header
    assert "metadata_json" not in rows[0]

    # Confirm PII is never exposed in output
    assert "Confidential Customer" not in csv_text
    assert "topsecret@enterprise.com" not in csv_text
    assert "+919876543210" not in csv_text
    assert "SECURE_PAYMENT_LINK" in csv_text
    assert "14997.50" in csv_text
