"""
app/tests/test_outreach.py
--------------------------
Async pytest tests for the Revora OutreachService and RazorpayService.

Coverage:
  RazorpayService:
    1. generate_payment_link returns the expected mock URL format.

  OutreachService — send_whatsapp_recovery:
    2. Returned dict has correct status and channel.
    3. Message contains the customer name and amount.
    4. Payment link appears in the message body.

  OutreachService — trigger_hinglish_voice:
    5. Script contains the customer name.
    6. Script contains the amount.
    7. Script contains the Hinglish greeting "Namaste".
    8. Returned dict has status="call_initiated" and a populated script_length.

  OutreachService — execute_adaptive_downgrade:
    9.  Returned dict has status="downgrade_offer_sent" and channel="email".
    10. new_amount is exactly half of the original amount.
    11. currency is preserved from the event.
"""

import uuid

import pytest

from app.models.orm import PaymentEvent, PaymentStatus
from app.services.outreach import OutreachService
from app.services.razorpay_client import RazorpayService


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _make_event(
    amount: float = 999.0,
    customer_name: str = "Priya Sharma",
    customer_email: str = "priya@revora.ai",
    customer_contact: str = "+919876543210",
) -> PaymentEvent:
    """Construct a lightweight PaymentEvent (no DB required)."""
    return PaymentEvent(
        id=str(uuid.uuid4()),
        customer_id="cust_test_001",
        customer_name=customer_name,
        customer_email=customer_email,
        customer_contact=customer_contact,
        amount=amount,
        currency="INR",
        error_code="BAD_REQUEST_ERROR",
        error_reason="low_balance",
        status=PaymentStatus.AT_RISK,
    )


MOCK_LINK = "https://rzp.io/i/mock_ref123"


# ---------------------------------------------------------------------------
# RazorpayService tests
# ---------------------------------------------------------------------------

async def test_generate_payment_link_returns_mock_url() -> None:
    """generate_payment_link must return a URL containing the reference_id."""
    service = RazorpayService()
    link = await service.generate_payment_link(
        amount=499.0,
        customer_name="Arjun Mehta",
        customer_email="arjun@revora.ai",
        reference_id="ref123",
    )
    assert link.startswith("https://rzp.io/i/mock_")
    assert "ref123" in link


# ---------------------------------------------------------------------------
# OutreachService — send_whatsapp_recovery
# ---------------------------------------------------------------------------

async def test_whatsapp_recovery_status_and_channel() -> None:
    """send_whatsapp_recovery must return status=delivered, channel=whatsapp."""
    service = OutreachService()
    event   = _make_event()
    result  = await service.send_whatsapp_recovery(event, MOCK_LINK)

    assert result["status"]  == "delivered"
    assert result["channel"] == "whatsapp"


async def test_whatsapp_message_contains_customer_name() -> None:
    """The generated WhatsApp message must address the customer by name."""
    service = OutreachService()
    event   = _make_event(customer_name="Kiran Bose")
    result  = await service.send_whatsapp_recovery(event, MOCK_LINK)

    assert "Kiran Bose" in result["message"]


async def test_whatsapp_message_contains_amount() -> None:
    """The generated WhatsApp message must include the formatted amount."""
    service = OutreachService()
    event   = _make_event(amount=1499.50)
    result  = await service.send_whatsapp_recovery(event, MOCK_LINK)

    assert "1499.50" in result["message"]


async def test_whatsapp_message_contains_payment_link() -> None:
    """The generated WhatsApp message must embed the payment link."""
    service = OutreachService()
    event   = _make_event()
    result  = await service.send_whatsapp_recovery(event, MOCK_LINK)

    assert MOCK_LINK in result["message"]
    assert result["payment_link"] == MOCK_LINK


# ---------------------------------------------------------------------------
# OutreachService — trigger_hinglish_voice
# ---------------------------------------------------------------------------

async def test_hinglish_script_contains_namaste() -> None:
    """Voice script must open with 'Namaste'."""
    service = OutreachService()
    event   = _make_event()
    result  = await service.trigger_hinglish_voice(event, MOCK_LINK)

    assert "Namaste" in result["script"]


async def test_hinglish_script_contains_customer_name() -> None:
    """Voice script must address the customer by name."""
    service = OutreachService()
    event   = _make_event(customer_name="Rahul Verma")
    result  = await service.trigger_hinglish_voice(event, MOCK_LINK)

    assert "Rahul Verma" in result["script"]


async def test_hinglish_script_contains_amount() -> None:
    """Voice script must include the correct payment amount."""
    service = OutreachService()
    event   = _make_event(amount=2499.0)
    result  = await service.trigger_hinglish_voice(event, MOCK_LINK)

    assert "2499.00" in result["script"]


async def test_hinglish_call_status_and_script_length() -> None:
    """trigger_hinglish_voice must return status=call_initiated with a positive script_length."""
    service = OutreachService()
    event   = _make_event()
    result  = await service.trigger_hinglish_voice(event, MOCK_LINK)

    assert result["status"]        == "call_initiated"
    assert result["channel"]       == "voice"
    assert result["script_length"] == len(result["script"])
    assert result["script_length"] > 50    # non-trivial script


# ---------------------------------------------------------------------------
# OutreachService — execute_adaptive_downgrade
# ---------------------------------------------------------------------------

async def test_adaptive_downgrade_status_and_channel() -> None:
    """execute_adaptive_downgrade must return status=downgrade_offer_sent, channel=email."""
    service = OutreachService()
    event   = _make_event(amount=1000.0)
    result  = await service.execute_adaptive_downgrade(event)

    assert result["status"]  == "downgrade_offer_sent"
    assert result["channel"] == "email"


async def test_adaptive_downgrade_halves_amount() -> None:
    """new_amount must be exactly half of the original amount."""
    service = OutreachService()
    event   = _make_event(amount=1000.0)
    result  = await service.execute_adaptive_downgrade(event)

    assert result["new_amount"]      == pytest.approx(500.0)
    assert result["original_amount"] == pytest.approx(1000.0)


async def test_adaptive_downgrade_preserves_currency() -> None:
    """execute_adaptive_downgrade must preserve the original currency code."""
    service = OutreachService()
    event   = _make_event(amount=499.0)
    event.currency = "INR"
    result  = await service.execute_adaptive_downgrade(event)

    assert result["currency"] == "INR"
