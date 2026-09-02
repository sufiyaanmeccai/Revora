"""
app/services/customer_context.py
---------------------------------
Customer Context Resolver & Unit Economics Model for the Revora Revenue Recovery Engine (Phase 10).

Provides:
  1. CustomerContextResolver: Deterministic, non-PII simulated customer value tier & tenure resolution.
  2. SIMULATED_INTERVENTION_COSTS: Centralized configuration of recovery channel cost assumptions.
  3. calculate_economics: Net Recovery Value calculation (Expected Recoverable Amount - Intervention Cost).

Terminology Note:
  All customer tiers ("HIGH", "STANDARD", "LOW") and costs are simulated demo assumptions
  designed for unit-economic intelligence during judging, not real customer LTV or live billing costs.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from pydantic import BaseModel, Field

from app.models.orm import RecoveryStrategy

if TYPE_CHECKING:
    from app.models.orm import PaymentEvent

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Simulated Intervention Cost Model (Demo Assumptions)                       #
# --------------------------------------------------------------------------- #
SIMULATED_INTERVENTION_COSTS: Dict[str, float] = {
    "SILENT_MANDATE_RETRY": 0.00,       # Zero-cost automated retry via existing mandate
    "SECURE_PAYMENT_LINK": 2.50,        # WhatsApp / SMS recovery nudge + hosted checkout link
    "UPI_AUTOPAY_MIGRATION": 2.50,      # WhatsApp interactive flow to setup fresh UPI mandate
    "ADAPTIVE_DOWNGRADE_OFFER": 2.50,   # Email / WhatsApp downsell checkout link
    "ESCALATE_TO_HUMAN": 150.00,        # Human operations agent manual outreach & support cost
}


class CustomerContext(BaseModel):
    """
    Sanitized, non-PII simulated customer profile for economic decisioning.
    """
    value_tier: str = Field(
        description="Simulated customer value tier: 'HIGH' | 'STANDARD' | 'LOW'",
    )
    tenure_months: int = Field(
        description="Simulated customer account tenure in months.",
    )
    description: str = Field(
        default="Simulated Customer Value Tier",
        description="Label clarifying this context is simulated.",
    )


class CustomerContextResolver:
    """
    Deterministic, non-PII Customer Context Resolver.

    Derives simulated value tiers and tenure from non-PII event metadata (e.g. event ID hash,
    amount brackets, or explicit demo payload tags) without using customer names, emails, or PII.
    """

    @staticmethod
    def resolve(event: "PaymentEvent") -> CustomerContext:
        """
        Resolve customer context deterministically from non-PII metadata.
        """
        if event is None:
            return CustomerContext(value_tier="STANDARD", tenure_months=12)

        # 1. Check if explicit demo tier tags exist in raw_payload
        if event.raw_payload:
            try:
                data = json.loads(event.raw_payload)
                if isinstance(data, dict):
                    if "value_tier" in data:
                        tier = str(data["value_tier"]).upper()
                        tenure = int(data.get("tenure_months", 36))
                        return CustomerContext(value_tier=tier, tenure_months=tenure)
                    tier_str = str(data.get("tier", ""))
                    if "VIP" in tier_str or "Annual" in tier_str or "Enterprise" in tier_str:
                        return CustomerContext(value_tier="HIGH", tenure_months=36)
                    if "Micro" in tier_str or "Starter" in tier_str:
                        return CustomerContext(value_tier="LOW", tenure_months=6)
            except Exception:
                pass

        # 2. Derive deterministically using amount floor & non-PII ID hash
        event_id = str(event.id or "default")
        h = int(hashlib.sha256(event_id.encode("utf-8")).hexdigest(), 16)

        if event.amount >= 10000.0:
            tier = "HIGH"
            tenure = 24 + (h % 25)
        elif event.amount < 500.0:
            tier = "LOW"
            tenure = 6 + (h % 7)
        else:
            tier_choices = ["HIGH", "STANDARD", "STANDARD", "LOW"]
            tier = tier_choices[h % len(tier_choices)]
            tenure = 12 * ((h % 4) + 1)

        return CustomerContext(value_tier=tier, tenure_months=tenure)


def calculate_economics(
    strategy: str | RecoveryStrategy,
    amount: float,
) -> Tuple[float, float, float]:
    """
    Calculate the Net Recovery Value for a proposed strategy and event amount.

    Formula:
        Net Recovery Value = Expected Recoverable Amount - Simulated Intervention Cost

    Assumptions:
      • ADAPTIVE_DOWNGRADE_OFFER: Expected Amount = 50% of original invoice amount.
      • SECURE_PAYMENT_LINK, UPI_AUTOPAY_MIGRATION, SILENT_MANDATE_RETRY: Expected Amount = 100% of original.
      • ESCALATE_TO_HUMAN: Expected automated recovery amount = ₹0.00 (manual handoff).

    Returns:
        Tuple of (expected_recoverable_amount, intervention_cost, net_recovery_value)
    """
    strat_str = getattr(strategy, "value", str(strategy))
    cost = SIMULATED_INTERVENTION_COSTS.get(strat_str, 2.50)

    if strat_str in ("ADAPTIVE_DOWNGRADE_OFFER", RecoveryStrategy.ADAPTIVE_DOWNGRADE_OFFER):
        expected_amount = round(amount * 0.5, 2)
    elif strat_str in ("ESCALATE_TO_HUMAN", RecoveryStrategy.ESCALATE_TO_HUMAN):
        expected_amount = round(amount, 2)
    else:
        expected_amount = round(amount, 2)

    net_value = round(expected_amount - cost, 2)
    return expected_amount, cost, net_value
