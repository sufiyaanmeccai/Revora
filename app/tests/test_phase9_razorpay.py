"""
app/tests/test_phase9_razorpay.py
---------------------------------
Comprehensive unit & integration test suite for Phase 9:
  1. RazorpayService link creation with mocked SDK.
  2. Currency (INR), paise conversion, reference_id, and notify flags validation.
  3. Safe deterministic fallback on missing credentials or is_simulated=True.
  4. Resilience & exception handling (SDK errors, timeouts).
  5. Zero secret leakage (keys never in audit logs or payloads).
  6. Closed-loop correlation with Phase 8C success webhook reconciliation.
"""

from __future__ import annotations

import json
import uuid
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.database import get_db
from app.main import app
from app.models.orm import (
    Base,
    DiagnosedCause,
    InterventionAuditLog,
    PaymentEvent,
    PaymentStatus,
    RecoveryStrategy,
    RecoveryWorkflow,
)
from app.models.schemas import AgentDecision
from app.services.decision_engine import _run_engine
from app.services.razorpay_client import RazorpayService
from app.services.reconciliation import reconcile_payment_success

# --------------------------------------------------------------------------- #
# Isolated test database fixture                                              #
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


# --------------------------------------------------------------------------- #
# 1. RazorpayService Unit Tests (Mocked SDK - Zero Live Network Calls)        #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_create_payment_link_live_success():
    """Verify live payment link creation passes valid payload to Razorpay SDK."""
    service = RazorpayService(key_id="rzp_test_12345", key_secret="secret_abcde")
    assert service.is_configured is True

    event = PaymentEvent(
        id=str(uuid.uuid4()),
        amount=1499.0,
        customer_name="Aarav Patel",
        customer_email="aarav.patel@test.in",
    )

    mock_sdk_response = {
        "id": "plink_LiveTest123",
        "short_url": "https://rzp.io/i/LiveTest123",
        "amount": 149900,
        "currency": "INR",
        "reference_id": str(event.id),
        "status": "created",
    }

    mock_client = MagicMock()
    mock_client.payment_link.create.return_value = mock_sdk_response
    service._client = mock_client

    result = await service.create_payment_link(
        event=event,
        amount=1499.0,
        description="Subscription Recovery Invoice",
        is_simulated=False,
    )

    assert result["is_mock"] is False
    assert result["payment_link_id"] == "plink_LiveTest123"
    assert result["short_url"] == "https://rzp.io/i/LiveTest123"

    # Verify payload passed to SDK
    mock_client.payment_link.create.assert_called_once()
    called_payload = mock_client.payment_link.create.call_args[0][0]
    assert called_payload["amount"] == 149900  # Paise conversion
    assert called_payload["currency"] == "INR"
    assert called_payload["accept_partial"] is False
    assert called_payload["reference_id"] == str(event.id)
    assert called_payload["notify"] == {"sms": False, "email": False}
    assert called_payload["notes"]["event_id"] == str(event.id)


@pytest.mark.asyncio
async def test_create_payment_link_missing_credentials_fallback():
    """Verify missing credentials safely return deterministic mock link."""
    service = RazorpayService(key_id="", key_secret="")
    assert service.is_configured is False

    event = PaymentEvent(id="evt_test_uuid_999", amount=500.0)

    result = await service.create_payment_link(
        event=event,
        amount=500.0,
        is_simulated=False,
    )

    assert result["is_mock"] is True
    assert result["payment_link_id"] == "plink_mock_evt_test"
    assert result["short_url"] == "https://rzp.io/i/mock_evt_test"


@pytest.mark.asyncio
async def test_create_payment_link_simulated_override():
    """Verify is_simulated=True forces mock link even when credentials are configured."""
    service = RazorpayService(key_id="rzp_test_valid", key_secret="secret_valid")
    assert service.is_configured is True

    event = PaymentEvent(id="evt_simulated_123", amount=2000.0)

    result = await service.create_payment_link(
        event=event,
        amount=2000.0,
        is_simulated=True,
    )

    assert result["is_mock"] is True
    assert result["payment_link_id"] == "plink_mock_evt_simu"
    assert result["short_url"] == "https://rzp.io/i/mock_evt_simu"


@pytest.mark.asyncio
async def test_create_payment_link_sdk_exception_resilience():
    """Verify that SDK errors/timeouts gracefully fall back to mock link without raising."""
    service = RazorpayService(key_id="rzp_test_err", key_secret="secret_err")
    mock_client = MagicMock()
    mock_client.payment_link.create.side_effect = Exception("504 Gateway Timeout from Razorpay API")
    service._client = mock_client

    event = PaymentEvent(id="evt_timeout_456", amount=750.0)

    result = await service.create_payment_link(
        event=event,
        amount=750.0,
        is_simulated=False,
    )

    assert result["is_mock"] is True
    assert "mock" in result["short_url"]


# --------------------------------------------------------------------------- #
# 2. Security & Zero Secret Leakage Tests                                      #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_secret_never_leaked_in_repr_or_logs(db_session: AsyncSession):
    """Assert key_secret is NEVER present in repr, logs, or audit metadata."""
    secret = "super_confidential_secret_xyz123"
    key_id = "rzp_test_secretkey"

    service = RazorpayService(key_id=key_id, key_secret=secret)
    repr_str = repr(service)
    assert secret not in repr_str
    assert key_id not in repr_str

    event = PaymentEvent(
        id=str(uuid.uuid4()),
        customer_id="cust_sec",
        customer_name="Security Test",
        customer_email="security@corp.in",
        customer_contact="+919876543210",
        amount=3000.0,
        status=PaymentStatus.AT_RISK,
    )
    db_session.add(event)
    await db_session.commit()

    mock_decision = AgentDecision(
        recommended_strategy="SECURE_PAYMENT_LINK",
        confidence_score=0.90,
        reasoning="Test secure payment link.",
        requires_consent=False,
    )

    with patch("app.services.decision_engine.analyze_failure_context", new=AsyncMock(return_value=mock_decision)), \
         patch("app.services.razorpay_client.settings") as mock_settings:
        mock_settings.RAZORPAY_KEY_ID = key_id
        mock_settings.RAZORPAY_KEY_SECRET = secret

        await _run_engine(db_session, event.id, is_simulated=False)

    # Check audit log metadata for leaks
    audit_res = await db_session.execute(
        select(InterventionAuditLog).where(InterventionAuditLog.payment_event_id == event.id)
    )
    log = audit_res.scalar_one()
    meta_text = str(log.metadata_json)

    assert secret not in meta_text
    assert secret not in (log.reasoning or "")


# --------------------------------------------------------------------------- #
# 3. Closed-Loop Reconciliation Correlation Test                              #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_payment_link_closed_loop_reconciliation(db_session: AsyncSession):
    """
    Verify full lifecycle:
      1. Create failed event.
      2. Decision engine generates payment link (with reference_id = event.id).
      3. Success webhook arrives with reference_id.
      4. Closed-loop reconciliation completes with exact accounting.
    """
    event = PaymentEvent(
        id=str(uuid.uuid4()),
        customer_id="cust_closed_loop",
        customer_name="Meera Iyer",
        customer_email="meera@domain.in",
        customer_contact="+919876543299",
        amount=8000.0,
        currency="INR",
        error_code="BAD_REQUEST_ERROR",
        error_reason="insufficient_funds",
        status=PaymentStatus.AT_RISK,
    )
    db_session.add(event)
    await db_session.commit()

    # Step 1: Decision engine executes ADAPTIVE_DOWNGRADE_OFFER
    mock_decision = AgentDecision(
        recommended_strategy="ADAPTIVE_DOWNGRADE_OFFER",
        confidence_score=0.95,
        reasoning="Adaptive downsell on high ticket.",
        requires_consent=True,
    )

    with patch("app.services.decision_engine.analyze_failure_context", new=AsyncMock(return_value=mock_decision)):
        await _run_engine(db_session, event.id, is_simulated=True)

    # Step 2: Customer pays the link -> triggers reconciliation with reference_id
    recon_result = await reconcile_payment_success(
        payment_identifier=event.id,
        amount_paid=4000.0,
        db=db_session,
        source="WEBHOOK_PAYMENT_LINK_PAID",
    )

    assert recon_result is not None
    assert recon_result["status"] == "success"
    assert recon_result["amount_recovered"] == 4000.0  # Exactly 50%
    assert recon_result["downgrade_applied"] is True

    # Step 3: Verify DB state
    fetched_event = await db_session.get(PaymentEvent, event.id)
    assert fetched_event.status == PaymentStatus.RECOVERED
