"""
Tiger V19 — Dynamic Capital Management Module
=============================================

Pre-order RMS check that prevents margin rejection before an order
reaches Angel One.  Three gates:

  GATE 1 — Live funds fetch (RMS API)
      Calls broker.get_balance() which hits rmsLimit().  Returns the
      real available cash.  Zero funds = hard block.

  GATE 2 — Disposable capital check
      If positions are already open and no free disposable capital
      remains for a new position, the order is BLOCKED immediately.
      A secondary trade is allowed only when sufficient separate capital
      is available without affecting the primary running trade.

  GATE 3 — Conviction-based dynamic allocation (RULE A)
      When all 7 brains are aligned and sure, capital is allocated
      dynamically based on available margin and conviction tier.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from config.thresholds import BRAIN4

logger = logging.getLogger("tiger_brain.capital_manager")


@dataclass
class CapitalCheck:
    """Result of a pre-order capital check."""
    allowed: bool
    reason: str
    available_funds: float = 0.0
    deployed_capital: float = 0.0
    free_disposable: float = 0.0
    allocated_capital: float = 0.0
    allocation_pct: float = 0.0
    conviction_tier: str = "NONE"
    conviction_multiplier: float = 0.0
    blocked: bool = False


class CapitalManager:
    """Pre-order RMS capital gate with conviction-based dynamic sizing.

    Usage in TigerBrainAutomation._place_live_orders::

        cm = CapitalManager(broker)
        check = cm.check_and_allocate(
            setup_score=85.0,
            brain_alignment=7,        # all 7 brains aligned
            trade_cost_estimate=12000,
            open_positions_cost=45000,
        )
        if not check.allowed:
            logger.warning("Order blocked: %s", check.reason)
            continue
        # use check.allocated_capital for sizing
    """

    def __init__(self, broker):
        self.broker = broker

    # ----------------------------------------------------------
    # GATE 1: Live funds fetch
    # ----------------------------------------------------------
    def fetch_live_funds(self) -> float:
        """Fetch real available cash from Angel One RMS API.

        Returns 0.0 on failure (caller must block the order).
        """
        try:
            funds = self.broker.get_balance()
            if funds <= 0:
                logger.warning(
                    "RMS returned zero/negative available cash: %.2f", funds)
            return funds
        except Exception as exc:
            logger.error("RMS funds fetch failed: %s", exc)
            return 0.0

    # ----------------------------------------------------------
    # GATE 2: Disposable capital check
    # ----------------------------------------------------------
    @staticmethod
    def compute_free_disposable(
        available_funds: float,
        open_positions_cost: float,
    ) -> float:
        """Compute free disposable capital after subtracting deployed margin.

        If open positions already consume all available funds, the
        returned value is <= 0, meaning no secondary trade can be placed
        without risking margin rejection.
        """
        return available_funds - open_positions_cost

    # ----------------------------------------------------------
    # GATE 3: Conviction-based dynamic allocation (RULE A)
    # ----------------------------------------------------------
    @staticmethod
    def conviction_tier(
        setup_score: float,
        brain_alignment: int,
    ) -> tuple[str, float]:
        """Determine conviction tier from 7-brain alignment and score.

        RULE A (Sure Shot): All 7 brains aligned + score >= 90 → 100%
        allocation of available margin.  Lower tiers scale down.

        Returns (tier_name, allocation_multiplier).
        """
        all_aligned = brain_alignment >= 7
        strong_alignment = brain_alignment >= 6

        if all_aligned and setup_score >= BRAIN4["CONFIDENCE_TIER_ROCKET_MIN"]:
            return "SURE_SHOT", BRAIN4["CONFIDENCE_ROCKET_PCT"] / 100.0
        if strong_alignment and setup_score >= BRAIN4["CONFIDENCE_TIER_STRONG_MIN"]:
            return "STRONG", BRAIN4["CONFIDENCE_STRONG_PCT"] / 100.0
        if setup_score >= BRAIN4["CONFIDENCE_TIER_DECENT_MIN"]:
            return "DECENT", BRAIN4["CONFIDENCE_DECENT_PCT"] / 100.0
        return "WEAK", 0.0

    # ----------------------------------------------------------
    # MAIN: check_and_allocate
    # ----------------------------------------------------------
    def check_and_allocate(
        self,
        setup_score: float,
        brain_alignment: int,
        trade_cost_estimate: float,
        open_positions_cost: float = 0.0,
        min_allocation: float = 0.0,
    ) -> CapitalCheck:
        """Run all three gates and return allocation decision.

        Args:
            setup_score: 0-100 score from the 7-brain scoring engine.
            brain_alignment: number of brains in agreement (1-7).
            trade_cost_estimate: estimated cost of the new position
                (qty * premium, or one_lot_cost for sizing).
            open_positions_cost: total deployed capital in running trades.
            min_allocation: minimum capital required for the trade to
                make sense (e.g., one lot cost). If allocated < this,
                the trade is blocked.

        Returns:
            CapitalCheck with allowed/blocked + allocation details.
        """
        # GATE 1: Live funds
        available = self.fetch_live_funds()
        if available <= 0:
            return CapitalCheck(
                allowed=False,
                reason="BLOCKED: RMS returned 0 available funds",
                available_funds=0.0,
                blocked=True,
            )

        # GATE 2: Disposable capital (RULE B — Order Blocking)
        free_disposable = self.compute_free_disposable(
            available, open_positions_cost)

        if free_disposable <= 0:
            return CapitalCheck(
                allowed=False,
                reason=(
                    f"BLOCKED: No free disposable capital "
                    f"(available {available:,.0f}, deployed "
                    f"{open_positions_cost:,.0f}) — margin rejection risk"
                ),
                available_funds=available,
                deployed_capital=open_positions_cost,
                free_disposable=free_disposable,
                blocked=True,
            )

        # GATE 3: Conviction-based allocation (RULE A)
        tier, multiplier = self.conviction_tier(setup_score, brain_alignment)
        if multiplier <= 0:
            return CapitalCheck(
                allowed=False,
                reason=(
                    f"BLOCKED: Conviction too low (score {setup_score:.0f}, "
                    f"brains {brain_alignment}/7, tier={tier}) — no allocation"
                ),
                available_funds=available,
                deployed_capital=open_positions_cost,
                free_disposable=free_disposable,
                conviction_tier=tier,
                conviction_multiplier=multiplier,
                blocked=True,
            )

        # Dynamic allocation: multiplier of available margin, capped by
        # free disposable to ensure primary trades are never affected.
        allocated = min(
            available * multiplier,
            free_disposable,
        )

        # If a minimum allocation is required and we can't meet it, block
        if min_allocation > 0 and allocated < min_allocation:
            return CapitalCheck(
                allowed=False,
                reason=(
                    f"BLOCKED: Allocated {allocated:,.0f} < minimum "
                    f"{min_allocation:,.0f} required for this position"
                ),
                available_funds=available,
                deployed_capital=open_positions_cost,
                free_disposable=free_disposable,
                allocated_capital=allocated,
                conviction_tier=tier,
                conviction_multiplier=multiplier,
                blocked=True,
            )

        # If the trade cost exceeds what we allocated, block
        if trade_cost_estimate > 0 and trade_cost_estimate > allocated:
            return CapitalCheck(
                allowed=False,
                reason=(
                    f"BLOCKED: Trade cost {trade_cost_estimate:,.0f} > "
                    f"allocated {allocated:,.0f} (tier={tier})"
                ),
                available_funds=available,
                deployed_capital=open_positions_cost,
                free_disposable=free_disposable,
                allocated_capital=allocated,
                conviction_tier=tier,
                conviction_multiplier=multiplier,
                blocked=True,
            )

        allocation_pct = (allocated / available * 100) if available > 0 else 0.0

        logger.info(
            "Capital CHECK PASS: tier=%s score=%.0f brains=%d/7 | "
            "available=%.0f deployed=%.0f free=%.0f allocated=%.0f (%.1f%%)",
            tier, setup_score, brain_alignment,
            available, open_positions_cost, free_disposable,
            allocated, allocation_pct,
        )

        return CapitalCheck(
            allowed=True,
            reason=f"ALLOWED: tier={tier}, allocated {allocation_pct:.1f}% of margin",
            available_funds=available,
            deployed_capital=open_positions_cost,
            free_disposable=free_disposable,
            allocated_capital=allocated,
            allocation_pct=allocation_pct,
            conviction_tier=tier,
            conviction_multiplier=multiplier,
        )
