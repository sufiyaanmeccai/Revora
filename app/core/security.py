"""
app/core/security.py
--------------------
Security utilities for Revora.

Phase 0 stub — webhook signature verification and token helpers will be
implemented in subsequent phases.
"""

import hashlib
import hmac

from app.core.config import settings


def verify_razorpay_webhook_signature(body: bytes, received_signature: str) -> bool:
    """
    Verify a Razorpay webhook payload against the configured webhook secret.

    Args:
        body:               Raw request body bytes.
        received_signature: X-Razorpay-Signature header value.

    Returns:
        True if the HMAC-SHA256 digest matches, False otherwise.
    """
    expected = hmac.new(
        settings.RAZORPAY_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, received_signature)
