"""
Tiger Brain V18 — PREMIUM DISCOUNT SNIPER (Brain 6)
====================================================
Yeh Tiger ki sabse important brain hai — PREMIUM ko discount pe
kharidti hai aur expensive pe bechti hai. 3x-5x returns ka secret
yahi hai.

CORE PHILOSOPHY:
  - Options premium = Intrinsic + Time Value (IV dependent)
  - Jab IV LOW hota hai → premium SASTA (discount) → BUY
  - Jab IV HIGH hota hai → premium MEHENGA (expensive) → SELL
  - Discount buy + IV expansion sell = 3x-5x returns

HOW IT WORKS:
  1. Track IV (realized vol) per symbol over lookback window
  2. Compute IV Percentile — where does current IV sit vs history?
     - 0-30%   = DEEP DISCOUNT (premium cheapest) → best entry
     - 30-50%  = DISCOUNT (good entry)
     - 50-65%  = FAIR (only if rocket score 90+)
     - 65-100% = EXPENSIVE (block entry, exit if holding)

  3. Strike Selection Matrix:
     - IV <25% → OTM (cheapest, max gamma, IV expand pe sabse zyada % move)
     - IV 25-50% → ATM (balanced)
     - IV >50% → ITM (delta protection, but block if >65%)

  4. IV Expansion Exit:
     - Exit when IV percentile > 70% (premium became expensive = SELL!)
     - This captures the full IV expansion cycle: buy cheap → sell expensive

This brain works WITH Brain 7 (Session Commander) — lunch lull
(1:00-2:30 PM) is the best discount window.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np

logger = logging.getLogger("tiger_brain.premium_brain")


# ============================================================
# IV PERCENTILE THRESHOLDS — premium cheap/expensive levels
# ============================================================
IV_DEEP_DISCOUNT_MAX = 30.0   # <30% = cheapest premium (best entry)
IV_DISCOUNT_MAX = 50.0        # 30-50% = discount (good entry)
IV_FAIR_MAX = 85.0            # 50-85% = fair (V18 loose entry — was 65, too strict)
IV_EXPENSIVE_EXIT = 90.0      # >90% = exit signal (was 70 — only exit truly expensive)

# Lookback window for IV percentile (number of historical IV readings)
IV_PERCENTILE_LOOKBACK = 100  # ~4 trading days of 25 bars/day


# ============================================================
# STRIKE SELECTION — IV-based smart premium buying
# ============================================================
STRIKE_OTM_THRESHOLD = 25.0   # IV percentile < 25% → OTM (cheapest)
STRIKE_ATM_THRESHOLD = 50.0   # IV percentile 25-50% → ATM
# IV percentile > 50% → ITM (but blocked if > 65%)


@dataclass
class PremiumSnapshot:
    """Current premium/IV state of a symbol — Tiger's premium radar."""
    symbol: str
    current_iv: float               # realized vol (annualized)
    iv_percentile: float            # 0-100, where current IV sits in history
    premium_status: str             # DEEP_DISCOUNT / DISCOUNT / FAIR / EXPENSIVE
    recommended_strike: str         # OTM / ATM / ITM
    should_enter: bool              # entry allowed based on IV?
    should_exit: bool               # exit signal (IV too expensive)?
    iv_history_count: int           # how many IV readings we have
    discount_bonus: float           # score bonus for discount entry
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"  Symbol:            {self.symbol}",
            f"  Current IV:        {self.current_iv:.1%}",
            f"  IV Percentile:     {self.iv_percentile:.1f}%",
            f"  Premium Status:    {self.premium_status}",
            f"  Recommended Strike:{self.recommended_strike}",
            f"  Enter? {('YES' if self.should_enter else 'NO')} | "
            f"Exit? {('YES' if self.should_exit else 'NO')}",
            f"  Discount Bonus:    +{self.discount_bonus:.0f}",
        ]
        for n in self.notes:
            lines.append(f"  ⚡ {n}")
        return "\n".join(lines)


class PremiumDiscountTracker:
    """
    Tracks IV history per symbol and computes IV percentile.

    Maintains a rolling window of IV readings per symbol. When asked,
    computes where the current IV sits relative to history (percentile).

    Usage:
        tracker = PremiumDiscountTracker()
        tracker.update("RELIANCE", iv=0.25)
        snapshot = tracker.evaluate("RELIANCE", iv=0.25)
        if snapshot.should_enter:
            # enter trade — premium is at discount!
    """

    def __init__(self, lookback: int = IV_PERCENTILE_LOOKBACK):
        self._iv_history: dict[str, deque] = {}
        self._lookback = lookback

    def update(self, symbol: str, iv: float) -> None:
        """Record a new IV reading for a symbol."""
        if symbol not in self._iv_history:
            self._iv_history[symbol] = deque(maxlen=self._lookback)
        self._iv_history[symbol].append(iv)

    def _compute_percentile(self, symbol: str, current_iv: float) -> float:
        """Compute IV percentile (0-100) — where current IV sits in history."""
        history = self._iv_history.get(symbol, deque(maxlen=self._lookback))
        if len(history) < 5:
            # Not enough history — assume middle (50th percentile)
            return 50.0
        arr = np.array(history)
        count_below = int(np.sum(arr < current_iv))
        return round(count_below / len(arr) * 100, 1)

    def evaluate(
        self,
        symbol: str,
        current_iv: float,
        setup_score: float = 0.0,
        is_holding: bool = False,
    ) -> PremiumSnapshot:
        """
        Evaluate premium status for a symbol.

        Args:
            symbol: stock/index/commodity symbol
            current_iv: current realized vol (annualized, e.g. 0.25 = 25%)
            setup_score: rocket score from scoring engine (for fair-zone override)
            is_holding: are we already holding a position? (affects exit logic)

        Returns:
            PremiumSnapshot with entry/exit/strike recommendations
        """
        percentile = self._compute_percentile(symbol, current_iv)
        history_count = len(self._iv_history.get(symbol, []))

        # --- Classify premium status ---
        if percentile < IV_DEEP_DISCOUNT_MAX:
            status = "DEEP_DISCOUNT"
        elif percentile < IV_DISCOUNT_MAX:
            status = "DISCOUNT"
        elif percentile < IV_FAIR_MAX:
            status = "FAIR"
        else:
            status = "EXPENSIVE"

        # --- Strike selection ---
        if percentile < STRIKE_OTM_THRESHOLD:
            strike = "OTM"
        elif percentile < STRIKE_ATM_THRESHOLD:
            strike = "ATM"
        else:
            strike = "ITM"

        # --- Entry decision (V18 LOOSE — never block on IV) ---
        # V18 style: the IV filter is advisory only — it NEVER blocks entry.
        # The premium brain scores discount entries (bonus) but does not
        # gate them. This ensures the bot takes trades in any IV regime.
        # V19 exit logic (IV expansion sell) is retained separately below.
        should_enter = True
        discount_bonus = 0.0
        notes = []

        if status == "DEEP_DISCOUNT":
            discount_bonus = 10.0
            notes.append("DEEP DISCOUNT! Premium sabse sasta — best entry window")
            notes.append(f"IV {current_iv:.1%} at {percentile:.0f}th percentile → OTM strike for max gamma")
        elif status == "DISCOUNT":
            discount_bonus = 5.0
            notes.append(f"Discount premium — IV at {percentile:.0f}th percentile, good entry")
        elif status == "FAIR":
            discount_bonus = 0.0
            notes.append(f"Fair IV ({percentile:.0f}th pct) — V18 loose entry allowed")
        else:  # EXPENSIVE
            discount_bonus = 0.0
            notes.append(f"EXPENSIVE IV at {percentile:.0f}th pct — entry allowed (V18 loose)")

        # --- Exit decision (if holding) ---
        should_exit = False
        if is_holding and percentile > IV_EXPENSIVE_EXIT:
            should_exit = True
            notes.append(f"IV EXPANDED to {percentile:.0f}th percentile — SELL! Premium now expensive")

        return PremiumSnapshot(
            symbol=symbol,
            current_iv=current_iv,
            iv_percentile=percentile,
            premium_status=status,
            recommended_strike=strike,
            should_enter=should_enter,
            should_exit=should_exit,
            iv_history_count=history_count,
            discount_bonus=discount_bonus,
            notes=notes,
        )


# ============================================================
# STRIKE COMPUTATION — IV-based smart strike selection
# ============================================================
def compute_premium_strike(
    underlying: float,
    iv_percentile: float,
    direction: str,
    is_call: bool,
) -> tuple[int, str]:
    """
    Compute the optimal strike based on IV percentile.

    When IV is LOW (discount), use OTM — cheaper premium, higher gamma.
    When IV is moderate, use ATM — balanced.
    When IV is HIGH, use ITM — delta protection (but typically blocked).

    Args:
        underlying: current underlying price
        iv_percentile: 0-100, where current IV sits in history
        direction: "BUY" (call) or "SELL" (put)
        is_call: True for CE, False for PE

    Returns:
        (strike, strike_kind) — the strike price and its classification
    """
    if iv_percentile < STRIKE_OTM_THRESHOLD:
        # OTM — cheaper, more gamma, IV expansion = max % gain
        if is_call:
            strike = round(underlying * 1.02)  # 2% OTM for calls
        else:
            strike = round(underlying * 0.98)  # 2% OTM for puts
        return (strike, "OTM")
    elif iv_percentile < STRIKE_ATM_THRESHOLD:
        # ATM — balanced delta and theta
        strike = round(underlying)
        return (strike, "ATM")
    else:
        # ITM — high delta (but typically blocked by premium brain)
        if is_call:
            strike = round(underlying * 0.99)  # 1% ITM for calls
        else:
            strike = round(underlying * 1.01)  # 1% ITM for puts
        return (strike, "ITM")


# ============================================================
# PREMIUM EXIT CHECK — IV expansion sell signal
# ============================================================
def check_premium_exit(
    pos: dict,
    current_iv: float,
    tracker: PremiumDiscountTracker,
) -> dict:
    """
    Check if a position should exit due to IV expansion (premium expensive).

    Called from the exit loop alongside check_intraday_exit(). If IV has
    expanded beyond the expensive threshold, this signals a sell — the
    premium is now expensive and should be captured.

    Args:
        pos: position dict (must have 'symbol', 'entry_iv')
        current_iv: current realized vol for the symbol
        tracker: PremiumDiscountTracker instance

    Returns:
        dict with 'exit' (bool), 'exit_premium' (not set here), 'reason'
    """
    symbol = pos.get("symbol", "")
    entry_iv = pos.get("iv", pos.get("entry_iv", 0.20))

    snapshot = tracker.evaluate(symbol, current_iv, is_holding=True)

    if snapshot.should_exit:
        logger.info(
            f"PREMIUM EXIT signal: {symbol} IV expanded "
            f"{entry_iv:.1%} → {current_iv:.1%} "
            f"({snapshot.iv_percentile:.0f}th pct) — sell expensive premium!"
        )
        return {
            "exit": True,
            "exit_premium": None,  # filled by caller with actual premium
            "reason": f"iv_expansion_exit({snapshot.iv_percentile:.0f}pct)",
        }

    return {"exit": False, "reason": ""}
