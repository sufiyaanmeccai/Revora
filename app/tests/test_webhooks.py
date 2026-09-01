"""
app/tests/test_webhooks.py
--------------------------
Async pytest tests for the Razorpay webhook ingestion endpoint.

Test matrix:
  1. Valid signature + payment.failed  → 200, PaymentEvent persisted in DB.
  2. Invalid signature                 → 400, no DB write.
  3. Unhandled event type              → 200 "ignored", no DB write.

Strategy:
  • Override the ``get_db`` FastAPI dependency with an isolated async in-memory
    SQLite session so tests never touch the development database.
  • Patch ``app.core.config.settings.RAZORPAY_WEBHOOK_SECRET`` to a known
    test secret so we can generate valid HMAC signatures in-test.
  • Mock ``process_payment_event`` so the background task doesn't attempt
    to open the real engine — decision-engine logic is covered separately
    in ``test_decision_engine.py``.
  • Use ``httpx.AsyncClient`` with ``ASGITransport`` to drive the real FastAPI app.
"""

import hashlib
import hmac
import json
from typing import AsyncGenerator
from unittest.mock import AsyncMock, patch

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
from app.models.orm import Base, PaymentEvent

# ---------------------------------------------------------------------------
# Isolated in-memory test database
# ---------------------------------------------------------------------------
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

TEST_WEBHOOK_SECRET = "revora_test_webhook_secret_abc123"

WEBHOOK_URL = "/api/v1/webhooks/razorpay"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
async def setup_test_db():
    """Create tables before each test, drop after."""
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield a session from the test engine for direct DB assertions."""
    async with _TestSession() as session:
        yield session


@pytest.fixture
def override_get_db(db_session: AsyncSession):
    """
    Override the FastAPI ``get_db`` dependency so the webhook handler uses the
    same in-memory SQLite session as our assertions.
    """
    async def _override():
        yield db_session

    app.dependency_overrides[get_db] = _override
    yield
    app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sign(payload: dict | bytes, secret: str = TEST_WEBHOOK_SECRET) -> str:
    """Compute HMAC-SHA256 hex digest exactly as Razorpay does."""
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _payment_failed_payload(
    payment_id: str = "pay_TestPF001",
    amount_paise: int = 49900,
    email: str = "test.customer@revora.ai",
) -> dict:
    """Return a minimal but realistic payment.failed Razorpay payload."""
    return {
        "entity": "event",
        "account_id": "acc_TestRevora",
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "entity": "payment",
                    "amount": amount_paise,
                    "currency": "INR",
                    "status": "failed",
                    "order_id": "order_TestORD001",
                    "subscription_id": "sub_TestSUB001",
                    "email": email,
                    "contact": "+919876543210",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_description": "Your payment failed due to low balance.",
                    "error_source": "customer",
                    "error_reason": "low_balance",
                    "notes": {
                        "customer_id": "cust_001",
                        "customer_name": "Arjun Mehta",
                    },
                }
            }
        },
    }


def _authorized_payload() -> dict:
    """Return a payment.authorized event — not in HANDLED_EVENTS."""
    return {
        "entity": "event",
        "account_id": "acc_TestRevora",
        "event": "payment.authorized",
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_AUTH001",
                    "amount": 49900,
                    "currency": "INR",
                    "status": "authorized",
                }
            }
        },
    }


# ---------------------------------------------------------------------------
# Test 1: Valid signature + payment.failed → 200 + PaymentEvent persisted
# ---------------------------------------------------------------------------

async def test_valid_signature_payment_failed_persists_event(
    override_get_db, db_session: AsyncSession
) -> None:
    """
    A correctly-signed ``payment.failed`` webhook must:
      • Return HTTP 200 with ``{"status": "success"}``.
      • Persist exactly one PaymentEvent row in the database.
      • Map all fields correctly (amount, email, error codes, status=AT_RISK).
    """
    payload = _payment_failed_payload()
    body = json.dumps(payload).encode()
    signature = _sign(body)

    with patch("app.api.v1.endpoints.webhooks.settings") as mock_settings, \
         patch(
             "app.api.v1.endpoints.webhooks.process_payment_event",
             new_callable=AsyncMock,
         ) as mock_engine:
        mock_settings.RAZORPAY_WEBHOOK_SECRET = TEST_WEBHOOK_SECRET

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                WEBHOOK_URL,
                content=body,
                headers={
                    "content-type": "application/json",
                    "x-razorpay-signature": signature,
                },
            )

    # HTTP assertion
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "success"
    assert "event_id" in data

    # Decision engine must have been scheduled exactly once
    mock_engine.assert_called_once()

    # DB assertion — exactly one PaymentEvent must have been inserted
    result = await db_session.execute(select(PaymentEvent))
    events = result.scalars().all()
    assert len(events) == 1, f"Expected 1 PaymentEvent, found {len(events)}"

    ev = events[0]
    assert ev.razorpay_payment_id  == "pay_TestPF001"
    assert ev.razorpay_order_id    == "order_TestORD001"
    assert ev.amount               == pytest.approx(499.00)      # 49900 paise → INR
    assert ev.currency             == "INR"
    assert ev.customer_email       == "test.customer@revora.ai"
    assert ev.customer_contact     == "+919876543210"
    assert ev.error_code           == "BAD_REQUEST_ERROR"
    assert ev.error_reason         == "low_balance"
    assert ev.status.value         == "AT_RISK"


# ---------------------------------------------------------------------------
# Test 2: Invalid signature → 400, no DB write
# ---------------------------------------------------------------------------

async def test_invalid_signature_returns_400_no_db_write(
    override_get_db, db_session: AsyncSession
) -> None:
    """
    A request with a tampered / wrong signature must:
      • Return HTTP 400 Bad Request.
      • NOT write any PaymentEvent to the database.
    """
    payload = _payment_failed_payload()
    body = json.dumps(payload).encode()

    with patch("app.api.v1.endpoints.webhooks.settings") as mock_settings:
        mock_settings.RAZORPAY_WEBHOOK_SECRET = TEST_WEBHOOK_SECRET

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                WEBHOOK_URL,
                content=body,
                headers={
                    "content-type": "application/json",
                    "x-razorpay-signature": "deadbeefdeadbeef_this_is_wrong",
                },
            )

    assert response.status_code == 400, response.text
    assert "Invalid" in response.json().get("detail", "")

    # DB must be empty
    result = await db_session.execute(select(PaymentEvent))
    assert result.scalars().all() == []


# ---------------------------------------------------------------------------
# Test 3: Unhandled event type → 200 "ignored", no DB write
# ---------------------------------------------------------------------------

async def test_unhandled_event_type_returns_200_ignored(
    override_get_db, db_session: AsyncSession
) -> None:
    """
    An event type NOT in HANDLED_EVENTS (e.g. ``payment.authorized``) must:
      • Return HTTP 200 with ``{"status": "ignored"}``.
      • NOT write any PaymentEvent to the database.
    """
    payload = _authorized_payload()
    body = json.dumps(payload).encode()
    signature = _sign(body)

    with patch("app.api.v1.endpoints.webhooks.settings") as mock_settings:
        mock_settings.RAZORPAY_WEBHOOK_SECRET = TEST_WEBHOOK_SECRET

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                WEBHOOK_URL,
                content=body,
                headers={
                    "content-type": "application/json",
                    "x-razorpay-signature": signature,
                },
            )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "ignored"
    assert data["event"]  == "payment.authorized"

    # DB must still be empty
    result = await db_session.execute(select(PaymentEvent))
    assert result.scalars().all() == []


# ---------------------------------------------------------------------------
# Test 4: Idempotency — duplicate x-razorpay-event-id returns 200 duplicate_event
# ---------------------------------------------------------------------------

async def test_idempotency_duplicate_event_id_returns_200_duplicate_event(
    override_get_db, db_session: AsyncSession
) -> None:
    """
    Sending the exact same x-razorpay-event-id twice must:
      • Return HTTP 200 with status='success' on first receipt.
      • Return HTTP 200 with status='ignored' and reason='duplicate_event' on second receipt.
      • Retain exactly ONE PaymentEvent in the database (no duplicate records).
      • Only queue the decision engine background task on the first call.
    """
    payload = _payment_failed_payload(payment_id="pay_Idempotent001")
    body = json.dumps(payload).encode()
    signature = _sign(body)
    event_id_header = "evt_razorpay_duplicate_check_999"

    with patch("app.api.v1.endpoints.webhooks.settings") as mock_settings, \
         patch(
             "app.api.v1.endpoints.webhooks.process_payment_event",
             new_callable=AsyncMock,
         ) as mock_engine:
        mock_settings.RAZORPAY_WEBHOOK_SECRET = TEST_WEBHOOK_SECRET

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # First delivery
            response1 = await client.post(
                WEBHOOK_URL,
                content=body,
                headers={
                    "content-type": "application/json",
                    "x-razorpay-signature": signature,
                    "x-razorpay-event-id": event_id_header,
                },
            )

            # Second delivery with identical event ID
            response2 = await client.post(
                WEBHOOK_URL,
                content=body,
                headers={
                    "content-type": "application/json",
                    "x-razorpay-signature": signature,
                    "x-razorpay-event-id": event_id_header,
                },
            )

    # First response checks
    assert response1.status_code == 200, response1.text
    data1 = response1.json()
    assert data1["status"] == "success"
    assert "event_id" in data1

    # Second response checks (idempotent ignore)
    assert response2.status_code == 200, response2.text
    data2 = response2.json()
    assert data2["status"] == "ignored"
    assert data2["reason"] == "duplicate_event"

    # Background decision engine must only have been queued once
    assert mock_engine.call_count == 1

    # Exactly 1 PaymentEvent record in DB
    result = await db_session.execute(select(PaymentEvent))
    events = result.scalars().all()
    assert len(events) == 1
    assert events[0].razorpay_event_id == event_id_header

