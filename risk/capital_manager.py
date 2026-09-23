"""
Tiger V19 — Dynamic Capital Management Module
=============================================

Pre-order RMS check that prevents margin rejection before an order
reaches Angel One.  This module is an ADVISOR, not a gatekeeper.  Tiger
owns the account money — it decides how much to deploy.  CapitalManager
only answers one question: "Is there enough REAL money in the account?"

  GATE 0 — Max open positions (safety: too many concurrent trades)
  GATE 1 — Live funds fetch (RMS API) — real available cash
  GATE 2 — Real money check: available - deployed >= trade cost?

Everything else (conviction tier, allocation %, Fund Brain risk guideline)
is ADVISORY — logged for Tiger's information, never blocks a trade.

The money is Tiger's.  Fund Brain advises.  CapitalManager checks reality.
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
        open_position_count: int = 0,
        market: str = "",
        market_deployed_cost: float = 0.0,
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
            open_position_count: number of currently OPEN positions
                (used by the MAX_OPEN_POSITIONS gate).

        Returns:
            CapitalCheck with allowed/blocked + allocation details.
        """
        # GATE 0: Max open positions (CAPITAL FIX — quality over quantity)
        max_open = BRAIN4.get("MAX_OPEN_POSITIONS", 0)
        if max_open > 0 and open_position_count >= max_open:
            return CapitalCheck(
                allowed=False,
                reason=(
                    f"BLOCKED: {open_position_count} open positions "
                    f">= MAX_OPEN_POSITIONS {max_open} — wait for exit"
                ),
                available_funds=0.0,
                deployed_capital=open_positions_cost,
                blocked=True,
            )

        # GATE 1: Live funds
        available = self.fetch_live_funds()
        if available <= 0:
            return CapitalCheck(
                allowed=False,
                reason="BLOCKED: RMS returned 0 available funds",
                available_funds=0.0,
                blocked=True,
            )

        # === 50/50 NSE/MCX CAPITAL SPLIT (PERMANENT FIX) ===
        # Each market gets its own 50% budget. NSE never blocks MCX, MCX never
        # blocks NSE. market_deployed_cost = capital already deployed in THIS
        # market only (not the other market's positions).
        if market and market_deployed_cost >= 0:
            split_pct = BRAIN4.get("MARKET_CAPITAL_SPLIT_PCT", 50.0) / 100.0
            market_budget = available * split_pct
            market_free = market_budget - market_deployed_cost
            free_disposable = market_free
            logger.info(
                "💰 50/50 SPLIT [%s]: budget ₹%.0f, deployed ₹%.0f, "
                "free ₹%.0f (total balance ₹%.0f)",
                market, market_budget, market_deployed_cost,
                market_free, available,
            )
        else:
            # Fallback: old behavior (total balance minus all deployed)
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

        # GATE 3: Conviction tier — ADVISORY ONLY (never blocks).
        # Tiger decides.  We log the tier so Tiger knows the conviction level,
        # but we do NOT block on low conviction.  That's Tiger's call.
        tier, multiplier = self.conviction_tier(setup_score, brain_alignment)
        if multiplier <= 0:
            logger.info(
                "💡 ADVISORY: low conviction (score %.0f, brains %d/7, tier=%s) "
                "— Tiger decides whether to proceed",
                setup_score, brain_alignment, tier)

        # REAL MONEY CHECK: can the account actually afford this trade?
        # If trade_cost > free_disposable → not enough REAL money → block.
        # If trade_cost <= free_disposable → ALLOW, Tiger takes what it needs.
        allocated = free_disposable  # Tiger deploys whatever is needed
        if trade_cost_estimate > 0 and trade_cost_estimate > free_disposable:
            return CapitalCheck(
                allowed=False,
                reason=(
                    f"BLOCKED: Not enough real money — trade cost "
                    f"{trade_cost_estimate:,.0f} > available "
                    f"{free_disposable:,.0f} (balance ₹{available:,.0f}, "
                    f"deployed ₹{open_positions_cost:,.0f})"
                ),
                available_funds=available,
                deployed_capital=open_positions_cost,
                free_disposable=free_disposable,
                allocated_capital=allocated,
                conviction_tier=tier,
                conviction_multiplier=multiplier,
                blocked=True,
            )

        # NOTE: Old min_allocation and trade_cost > allocated gates removed.
        # The REAL MONEY CHECK above already handles both cases:
        #   - trade_cost > free_disposable → blocked (not enough real money)
        #   - trade_cost <= free_disposable → allowed (Tiger takes what it needs)
        # But if a minimum allocation is specified and free_disposable can't
        # meet it, that's also not enough real money → block.
        if min_allocation > 0 and free_disposable < min_allocation:
            return CapitalCheck(
                allowed=False,
                reason=(
                    f"BLOCKED: Not enough real money — available "
                    f"{free_disposable:,.0f} < minimum {min_allocation:,.0f} "
                    f"required (balance ₹{available:,.0f}, "
                    f"deployed ₹{open_positions_cost:,.0f})"
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
