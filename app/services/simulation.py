"""
app/services/simulation.py
---------------------------
Synthetic batch generation service for the Revora Revenue Recovery Engine.

Generates realistic batches of failed PaymentEvent records for:
  • Buildathon demonstration of batch recovery capability.
  • Load testing the decision engine pipeline.
  • Dashboard population during demo sessions.

Each generated record has randomised error profile, customer identity,
and payment amount drawn from a curated set of realistic Indian profiles.
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timezone
from typing import List

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.orm import PaymentEvent, PaymentStatus

# ---------------------------------------------------------------------------
# Randomisation pools
# ---------------------------------------------------------------------------

_FAILURE_PROFILES = [
    # (error_code, error_reason, weight)  — higher weight = more frequent
    ("GATEWAY_ERROR",     "timeout",            3),
    ("GATEWAY_ERROR",     "bank_offline",        2),
    ("BAD_REQUEST_ERROR", "expired_card",        3),
    ("BAD_REQUEST_ERROR", "card_declined",       4),
    ("BAD_REQUEST_ERROR", "insufficient_funds",  5),
    ("BAD_REQUEST_ERROR", "low_balance",         4),
    ("BAD_REQUEST_ERROR", "invalid_upi_pin",     3),
    ("BAD_REQUEST_ERROR", "invalid_card",        2),
    ("BAD_REQUEST_ERROR", "do_not_honour",       2),
    ("UNKNOWN",           "mandate_declined",    2),
]

# Unpack into parallel lists for random.choices
_CODES, _REASONS, _WEIGHTS = zip(*_FAILURE_PROFILES)

_FIRST_NAMES = [
    "Priya", "Arjun", "Rahul", "Sneha", "Kiran",
    "Ananya", "Vikram", "Pooja", "Rajesh", "Meera",
    "Suresh", "Divya", "Aditya", "Nisha", "Rohan",
    "Shreya", "Amit", "Kavya", "Sanjay", "Lakshmi",
]
_LAST_NAMES = [
    "Sharma", "Mehta", "Verma", "Iyer", "Patel",
    "Nair", "Singh", "Gupta", "Reddy", "Bose",
    "Joshi", "Agarwal", "Pillai", "Das", "Malhotra",
]
_DOMAINS = ["gmail.com", "yahoo.co.in", "outlook.com", "hotmail.com", "rediffmail.com"]

# Ticket-size bands: (min_inr, max_inr, weight)
_AMOUNT_BANDS = [
    (99,    499,   3),   # low ticket
    (500,  1999,   3),   # mid ticket
    (2000, 12000,  2),   # high ticket
]
_AMT_MIN, _AMT_MAX, _AMT_W = zip(*_AMOUNT_BANDS)


def _random_amount() -> float:
    """Draw a random INR amount from the weighted ticket-size bands."""
    band = random.choices(range(len(_AMOUNT_BANDS)), weights=list(_AMT_W), k=1)[0]
    return round(random.uniform(_AMT_MIN[band], _AMT_MAX[band]), 2)


def _random_profile() -> tuple[str, str]:
    """Return a (error_code, error_reason) pair from the weighted pool."""
    idx = random.choices(range(len(_CODES)), weights=list(_WEIGHTS), k=1)[0]
    return _CODES[idx], _REASONS[idx]


def _random_customer() -> tuple[str, str, str, str]:
    """Return (customer_id, name, email, contact)."""
    first = random.choice(_FIRST_NAMES)
    last  = random.choice(_LAST_NAMES)
    name  = f"{first} {last}"
    uid   = uuid.uuid4().hex[:8]
    email = f"{first.lower()}.{last.lower()}{random.randint(1, 99)}@{random.choice(_DOMAINS)}"
    contact = f"+91{random.randint(7000000000, 9999999999)}"
    return f"cust_{uid}", name, email, contact


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def generate_synthetic_batch(
    db: AsyncSession,
    count: int = 50,
) -> List[str]:
    """
    Generate and persist ``count`` synthetic failed PaymentEvent records.

    Each event is independently randomised across:
      • Error profile (code + reason) — weighted toward common real-world failures.
      • Amount — sampled from three ticket-size bands (low / mid / high).
      • Customer identity — random Indian first name + last name combinations.

    Args:
        db:    Async database session (caller is responsible for lifecycle).
        count: Number of records to generate (default 50, capped by caller).

    Returns:
        List of generated PaymentEvent IDs (strings) in insertion order.
    """
    event_ids: List[str] = []

    for i in range(count):
        error_code, error_reason = _random_profile()
        amount                   = _random_amount()
        customer_id, name, email, contact = _random_customer()

        event = PaymentEvent(
            id=str(uuid.uuid4()),
            razorpay_event_id=f"evt_SIM{uuid.uuid4().hex[:10].upper()}",
            razorpay_payment_id=f"pay_SIM{uuid.uuid4().hex[:10].upper()}",
            razorpay_order_id=f"order_SIM{uuid.uuid4().hex[:10].upper()}",
            razorpay_subscription_id=f"sub_SIM{uuid.uuid4().hex[:8].upper()}",
            customer_id=customer_id,
            customer_name=name,
            customer_email=email,
            customer_contact=contact,
            amount=amount,
            currency="INR",
            error_code=error_code,
            error_description=f"Simulated failure: {error_reason.replace('_', ' ')}.",
            error_source="simulation",
            error_reason=error_reason,
            status=PaymentStatus.AT_RISK,
            raw_payload=f'{{"event":"payment.failed","simulation":true,"index":{i}}}',
        )
        db.add(event)
        event_ids.append(event.id)

    await db.commit()
    return event_ids
