"""
app/services/outreach.py
-------------------------
Outreach orchestration service for the Revora Revenue Recovery Engine.

Implements the customer-facing execution layer for each recovery strategy:

  ┌──────────────────────────────┬───────────────────────────────┐
  │ Strategy                     │ Outreach method               │
  ├──────────────────────────────┼───────────────────────────────┤
  │ SECURE_PAYMENT_LINK          │ send_whatsapp_recovery        │
  │ UPI_AUTOPAY_MIGRATION        │ send_whatsapp_recovery        │
  │ HINGLISH_VOICE_OUTREACH      │ trigger_hinglish_voice        │
  │ ADAPTIVE_DOWNGRADE_OFFER     │ execute_adaptive_downgrade    │
  │ SILENT_MANDATE_RETRY         │ (no external outreach)        │
  └──────────────────────────────┴───────────────────────────────┘

All methods return a structured result dict that is stored verbatim as
``metadata_json`` in the second ``RecoveryAuditLog`` entry, providing a
complete, queryable audit trail of every customer touchpoint.

Phase 4 implementations are production-structured stubs.  In Phase 5,
replace the mock dispatch bodies with:
  • WhatsApp: Interakt / Gupshup / Meta Cloud API call.
  • Voice:    Sarvam AI TTS + Exotel dial-out.
  • Email:    SendGrid / AWS SES.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict

from app.models.orm import PaymentEvent

logger = logging.getLogger(__name__)


class OutreachService:
    """
    Async customer outreach service.

    Each method generates the customer-facing message / action, dispatches it
    (or stubs the dispatch), and returns a structured result dict for auditing.
    """

    # -----------------------------------------------------------------------
    # WhatsApp recovery nudge
    # -----------------------------------------------------------------------
    async def send_whatsapp_recovery(
        self,
        event: PaymentEvent,
        payment_link: str,
    ) -> Dict[str, Any]:
        """
        Send a WhatsApp recovery message with a Razorpay payment link.

        Message template:
            "Hi {name}, your payment of ₹{amount} failed.
             Tap here to securely update your card or pay via UPI: {link}"

        Args:
            event:        The failed ``PaymentEvent`` ORM record.
            payment_link: Pre-generated Razorpay payment link URL.

        Returns:
            Structured dispatch result dict (stored in audit metadata_json).
        """
        message = (
            f"Hi {event.customer_name}, your payment of ₹{event.amount:.2f} failed. "
            f"Tap here to securely update your card or pay via UPI: {payment_link}"
        )

        logger.info(
            "OutreachService.send_whatsapp_recovery | "
            "customer=%r | contact=%r | link=%s",
            event.customer_email,
            event.customer_contact,
            payment_link,
        )

        # ── Stub: simulate a successful WhatsApp delivery ────────────────────
        return {
            "status":       "delivered",
            "channel":      "whatsapp",
            "recipient":    event.customer_contact or event.customer_email,
            "message":      message,
            "payment_link": payment_link,
            "timestamp":    datetime.now(timezone.utc).isoformat(),
        }

    # -----------------------------------------------------------------------
    # Hinglish voice call
    # -----------------------------------------------------------------------
    async def trigger_hinglish_voice(
        self,
        event: PaymentEvent,
        payment_link: str,
    ) -> Dict[str, Any]:
        """
        Initiate a Hinglish IVR / voice call to the customer.

        Script template (Hinglish — Hindi + English blend):
            "Namaste {name}. Aapka ₹{amount} ka recent subscription payment
             fail ho gaya hai. Humne aapke WhatsApp par ek secure Razorpay
             link bheja hai. Please us link se payment complete karein. Shukriya."

        Args:
            event:        The failed ``PaymentEvent`` ORM record.
            payment_link: Pre-generated Razorpay payment link URL.

        Returns:
            Structured dispatch result dict (stored in audit metadata_json).
        """
        script = (
            f"Namaste {event.customer_name}. "
            f"Aapka ₹{event.amount:.2f} ka recent subscription payment fail ho gaya hai. "
            f"Humne aapke WhatsApp par ek secure Razorpay link bheja hai. "
            f"Please us link se payment complete karein. Shukriya."
        )

        logger.info(
            "OutreachService.trigger_hinglish_voice | "
            "customer=%r | contact=%r | script_length=%d",
            event.customer_email,
            event.customer_contact,
            len(script),
        )

        # ── Stub: simulate a successful call initiation ──────────────────────
        return {
            "status":        "call_initiated",
            "channel":       "voice",
            "recipient":     event.customer_contact or event.customer_email,
            "script":        script,
            "script_length": len(script),
            "payment_link":  payment_link,
            "timestamp":     datetime.now(timezone.utc).isoformat(),
        }

    # -----------------------------------------------------------------------
    # Adaptive downgrade offer
    # -----------------------------------------------------------------------
    async def execute_adaptive_downgrade(
        self,
        event: PaymentEvent,
    ) -> Dict[str, Any]:
        """
        Generate and dispatch an adaptive plan downgrade offer to the customer.

        Calculates a reduced amount (50% of the original) and simulates
        sending a multi-plan checkout email so the customer can continue on
        a lower tier without churning.

        Args:
            event: The failed ``PaymentEvent`` ORM record.

        Returns:
            Structured dispatch result dict (stored in audit metadata_json).
        """
        new_amount = round(event.amount / 2, 2)

        logger.info(
            "OutreachService.execute_adaptive_downgrade | "
            "customer=%r | original=%.2f INR | offer=%.2f INR",
            event.customer_email,
            event.amount,
            new_amount,
        )

        # ── Stub: simulate a downgrade offer email dispatch ──────────────────
        return {
            "status":         "downgrade_offer_sent",
            "channel":        "email",
            "recipient":      event.customer_email,
            "original_amount": event.amount,
            "new_amount":     new_amount,
            "currency":       event.currency,
            "timestamp":      datetime.now(timezone.utc).isoformat(),
        }
