"""
app/services/razorpay_client.py
--------------------------------
Razorpay API client for the Revora Revenue Recovery Engine.

Phase 4 implements a production-structured stub that mirrors the exact
interface a live Razorpay Payment Links API call would expose.

In production, replace the stub body of ``generate_payment_link`` with
an authenticated ``httpx.AsyncClient`` call to:
  POST https://api.razorpay.com/v1/payment_links

The method signature and return type remain unchanged — callers are
completely insulated from the underlying HTTP mechanics.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Base URL used by the stub (and eventually the live API)
# ---------------------------------------------------------------------------
_RAZORPAY_LINK_BASE = "https://rzp.io/i"


class RazorpayService:
    """
    Async Razorpay client encapsulating Payment Links generation.

    All methods are async-first so swapping the stub for a live
    ``httpx.AsyncClient`` is a drop-in change requiring no call-site edits.
    """

    async def generate_payment_link(
        self,
        amount: float,
        customer_name: str,
        customer_email: str,
        reference_id: str,
    ) -> str:
        """
        Generate a Razorpay Payment Link for a given amount and customer.

        Args:
            amount:        Amount in INR (will be converted to paise for the API).
            customer_name: Full name of the customer.
            customer_email: Email address of the customer.
            reference_id:  Internal reference (e.g. PaymentEvent ID) for
                           idempotency and traceability.

        Returns:
            A short-form Razorpay payment link URL.

        Production note:
            Replace the stub body with an authenticated POST to
            ``https://api.razorpay.com/v1/payment_links`` using
            Basic Auth (key_id:key_secret) and a JSON body that includes
            ``amount`` (in paise), ``currency``, ``customer``, and
            ``reference_id``.
        """
        # ── Stub implementation ──────────────────────────────────────────────
        # Returns a deterministic mock URL that is safe to use in tests and
        # local development without real Razorpay credentials.
        link = f"{_RAZORPAY_LINK_BASE}/mock_{reference_id}"

        logger.info(
            "RazorpayService.generate_payment_link | "
            "customer=%r | amount=%.2f INR | ref=%r | link=%s",
            customer_email,
            amount,
            reference_id,
            link,
        )
        return link

    async def health(self) -> dict:
        """Return a health-check dict for the Razorpay integration."""
        return {
            "integration": "razorpay",
            "mode": "stub",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
