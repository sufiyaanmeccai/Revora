"""
app/services/razorpay_client.py
--------------------------------
Razorpay API client for the Revora Revenue Recovery Engine (Phase 9).

Integrates the official Razorpay Python SDK (``razorpay``) to generate real
Razorpay Test Mode payment links for SECURE_PAYMENT_LINK and ADAPTIVE_DOWNGRADE_OFFER
strategies when credentials exist.

Features:
  • Real Test Mode Payment Links when RAZORPAY_KEY_ID & RAZORPAY_KEY_SECRET exist.
  • Zero-PII safe minimal payloads:
      - amount: paise integer (int(round(amount * 100)))
      - currency: "INR"
      - accept_partial: False
      - reference_id: str(event.id) (UUID primary key for Phase 8C webhook reconciliation)
      - customer: {"name": "Revora Customer", "email": "customer@revora.internal"}
      - notify: {"sms": False, "email": False} (Revora handles customer touchpoints)
      - notes: {"event_id": str(event.id), "reference_id": str(event.id)}
  • Safe deterministic fallback when keys are missing, API times out/fails, or is_simulated=True.
  • Secrets are NEVER printed, logged, or leaked.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import razorpay

from app.core.config import settings
from app.models.orm import PaymentEvent

logger = logging.getLogger(__name__)

_RAZORPAY_LINK_BASE = "https://rzp.io/i"


class RazorpayService:
    """
    Razorpay client encapsulating Payment Links generation (Phase 9).
    """

    def __init__(
        self,
        key_id: Optional[str] = None,
        key_secret: Optional[str] = None,
    ) -> None:
        self._key_id = (key_id if key_id is not None else settings.RAZORPAY_KEY_ID or "").strip()
        self._key_secret = (key_secret if key_secret is not None else settings.RAZORPAY_KEY_SECRET or "").strip()
        self._client: Optional[razorpay.Client] = None
        if self.is_configured:
            try:
                self._client = razorpay.Client(auth=(self._key_id, self._key_secret))
            except Exception as exc:
                logger.warning("Failed to initialize Razorpay Client: %s", type(exc).__name__)
                self._client = None

    @property
    def is_configured(self) -> bool:
        """Return True if valid non-empty credentials are configured."""
        return bool(self._key_id and self._key_secret)

    def _generate_mock_link(self, reference_id: str) -> Dict[str, Any]:
        """Generate a deterministic mock payment link dictionary."""
        ref = (reference_id or "mock").strip()
        short_ref = ref[:8] if len(ref) >= 8 else ref
        return {
            "payment_link_id": f"plink_mock_{short_ref}",
            "short_url": f"{_RAZORPAY_LINK_BASE}/mock_{short_ref}",
            "is_mock": True,
        }

    async def create_payment_link(
        self,
        event: PaymentEvent,
        amount: float,
        description: str = "Payment Recovery - Plan Invoice",
        is_simulated: bool = False,
    ) -> Dict[str, Any]:
        """
        Generate a Razorpay Payment Link for a failed PaymentEvent.

        If credentials exist and is_simulated is False, creates a real link via Razorpay SDK.
        Otherwise, or on any error/timeout, falls back to deterministic mock link.

        Returns:
            Dict containing {"payment_link_id": str, "short_url": str, "is_mock": bool}.
        """
        ref_id = str(event.id)

        if is_simulated or not self.is_configured:
            logger.info(
                "RazorpayService: Using mock link (is_simulated=%s, configured=%s, ref=%s)",
                is_simulated,
                self.is_configured,
                ref_id[:8],
            )
            return self._generate_mock_link(ref_id)

        # Real Test Mode Link Creation
        amount_paise = int(round(amount * 100))
        payload = {
            "amount": amount_paise,
            "currency": "INR",
            "accept_partial": False,
            "reference_id": ref_id,
            "description": description or "Payment Recovery - Plan Invoice",
            "customer": {
                "name": "Revora Customer",
                "email": "customer@revora.internal",
            },
            "notify": {
                "sms": False,
                "email": False,
            },
            "notes": {
                "event_id": ref_id,
                "reference_id": ref_id,
            },
        }

        try:
            client = self._client or razorpay.Client(auth=(self._key_id, self._key_secret))
            res = await asyncio.to_thread(client.payment_link.create, payload)

            link_id = res.get("id") or f"plink_{ref_id[:8]}"
            short_url = res.get("short_url") or f"{_RAZORPAY_LINK_BASE}/{link_id}"

            logger.info(
                "RazorpayService: Created live Payment Link id=%s ref=%s amount_paise=%d",
                link_id,
                ref_id[:8],
                amount_paise,
            )
            return {
                "payment_link_id": link_id,
                "short_url": short_url,
                "is_mock": False,
            }

        except Exception as exc:
            logger.warning(
                "Razorpay API link creation failed: %s. Falling back to mock link for ref=%s",
                type(exc).__name__,
                ref_id[:8],
            )
            return self._generate_mock_link(ref_id)

    async def generate_payment_link(
        self,
        amount: float,
        customer_name: str,
        customer_email: str,
        reference_id: str,
        is_simulated: bool = False,
    ) -> str:
        """
        Backward-compatible helper returning just the URL string.
        """
        mock_event = PaymentEvent(id=reference_id, amount=amount)
        result = await self.create_payment_link(
            event=mock_event,
            amount=amount,
            description="Payment Recovery - Plan Invoice",
            is_simulated=is_simulated,
        )
        return result["short_url"]

    async def health(self) -> dict:
        """Return a health-check dict for the Razorpay integration."""
        return {
            "integration": "razorpay",
            "mode": "live" if self.is_configured else "mock",
            "configured": self.is_configured,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def __repr__(self) -> str:
        return f"<RazorpayService configured={self.is_configured}>"
