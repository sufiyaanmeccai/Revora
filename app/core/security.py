"""
app/core/security.py
--------------------
Security utilities for the Revora Revenue Recovery Engine.

Functions:
  • verify_webhook_signature  — HMAC-SHA256 verification for Razorpay webhooks
                                (timing-safe via hmac.compare_digest).
  • verify_razorpay_webhook_signature — convenience wrapper that reads the
                                        secret directly from app settings.
"""

import hashlib
import hmac
import logging

logger = logging.getLogger(__name__)


def verify_webhook_signature(
    payload_body: bytes,
    signature: str,
    secret: str,
) -> bool:
    """
    Cryptographically verify a Razorpay webhook payload.

    Razorpay computes:
        HMAC-SHA256(webhook_secret, raw_request_body)
    and sends the hex digest as the ``X-Razorpay-Signature`` header.

    Args:
        payload_body: Raw (undecoded) request body bytes.
        signature:    Hex digest from the ``X-Razorpay-Signature`` header.
        secret:       Razorpay webhook secret configured for this endpoint.

    Returns:
        ``True`` if the computed digest matches ``signature``, ``False`` otherwise.

    Security:
        Uses ``hmac.compare_digest`` to compare strings in constant time,
        preventing timing-based side-channel attacks.
    """
    if not secret:
        logger.warning(
            "RAZORPAY_WEBHOOK_SECRET is empty — all signatures will be rejected."
        )
        return False

    expected_digest = hmac.new(
        secret.encode("utf-8"),
        payload_body,
        hashlib.sha256,
    ).hexdigest()

    try:
        return hmac.compare_digest(expected_digest, signature)
    except (TypeError, ValueError) as exc:
        logger.error("Signature comparison failed: %s", exc)
        return False


def verify_razorpay_webhook_signature(body: bytes, received_signature: str) -> bool:
    """
    Convenience wrapper: verify a webhook using the secret from app settings.

    Kept for backward compatibility with the Phase 0 stub.
    Prefer ``verify_webhook_signature`` when the secret must be injected
    (e.g., in tests or multi-tenant scenarios).

    Args:
        body:               Raw request body bytes.
        received_signature: X-Razorpay-Signature header value.

    Returns:
        True if the HMAC-SHA256 digest matches, False otherwise.
    """
    from app.core.config import settings  # local import avoids circular deps

    return verify_webhook_signature(body, received_signature, settings.RAZORPAY_WEBHOOK_SECRET)
