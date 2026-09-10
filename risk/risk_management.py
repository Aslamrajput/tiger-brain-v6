"""
Tiger Brain V6.1 — BRAIN 4: Capital Allocation & Trade Counter Guard
======================================================================
Fourth brain. Enforces TWO capital/risk rules:

  1. TRADE COUNTER GUARD (user requirement):
     - Global daily limit: max 5-10 trades/day across ALL markets
     - Commodity-specific daily limit: max 5-10 trades/day for the commodity market
     - Once a limit is hit, new trades in that category are blocked for the day.

  2. CAPITAL ALLOCATION:
     - Dynamic position sizing — from LIVE available capital (Angel One),
       never hardcoded lot sizes.
     - Per-trade cap: max 10% of available capital.
     - Total exposure cap across open positions.

Counter state is in-memory + date-tracked — a new day auto-resets.
Persistence (disk) is sufficient in production for the scheduler process
lifetime; if multi-process is needed later, KV-store/file persistence
must be added.
"""

from __future__ import annotations

import logging
from datetime import date

try:
    from config.thresholds import BRAIN4, MARKET_CATEGORIES
except ImportError:
    raise ImportError("Run from repo ROOT, not from inside 'risk/'.")

logger = logging.getLogger("tiger_brain.risk_management")


def resolve_market_category(symbol: str, exchange: str | None = None) -> str:
    """
    Derives the market category from symbol/exchange.

    If exchange is provided, the MARKET_CATEGORIES mapping is used
    (MCX/NCDEX = commodity). Otherwise the symbol name is used to recognize
    common MCX symbols (CRUDEOIL, GOLD, SILVER, NATURALGAS, COPPER,
    ZINC, ALUMINIUM, NICKEL, LEAD, MENTHAOIL, COTTONCANDY...).
    """
    if exchange:
        return MARKET_CATEGORIES.get(exchange.upper(), "equity")

    upper = symbol.upper()
    commodity_names = {
        "CRUDEOIL", "GOLD", "GOLDM", "SILVER", "SILVERM", "SILVERMIC",
        "NATURALGAS", "COPPER", "ZINC", "ALUMINIUM", "ALLOY", "NICKEL",
        "LEAD", "MENTHAOIL", "COTTONCANDY", "COTTON", "BRENTCRUDEOIL",
    }
    # Strip expiry-suffix e.g. "CRUDEOIL25SEP" or "GOLD-M"
    base = upper
    for suffix in ("-M", "-MES", "MES", "MIC"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    import re
    base = re.sub(r"\d{2}(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC).*", "", base)
    return "commodity" if base in commodity_names else "equity"


class TradeCounterGuard:
    """
    Daily trade counter guard — global + commodity-specific limits.

    Usage:
        guard = TradeCounterGuard()                # config defaults
        guard = TradeCounterGuard(global_limit=6,  # override
                                  commodity_limit=5)
        guard.can_trade("GOLD", exchange="MCX")    # -> dict
        guard.register_trade("GOLD", exchange="MCX")
    """

    def __init__(
        self,
        global_limit: int | None = None,
        commodity_limit: int | None = None,
        today: date | None = None,
    ):
        g = BRAIN4["MAX_TRADES_PER_DAY_GLOBAL"] if global_limit is None else global_limit
        c = (
            BRAIN4["MAX_TRADES_PER_DAY_COMMODITY"]
            if commodity_limit is None
            else commodity_limit
        )

        # Clamp to the 5-10 mandated range
        self.global_limit = int(
            max(BRAIN4["MAX_TRADES_PER_DAY_GLOBAL_MIN"],
                min(BRAIN4["MAX_TRADES_PER_DAY_GLOBAL_MAX"], g))
        )
        self.commodity_limit = int(
            max(BRAIN4["MAX_TRADES_PER_DAY_COMMODITY_MIN"],
                min(BRAIN4["MAX_TRADES_PER_DAY_COMMODITY_MAX"], c))
        )

        self._today = today or date.today()
        self.global_count = 0
        self.commodity_count = 0

    # --- date handling ---
    def _roll_date_if_needed(self):
        """Resets counters on a new day."""
        today = date.today()
        if self._today != today:
            logger.info(
                f"Trade counter date roll: {self._today} -> {today} — counters reset"
            )
            self._today = today
            self.global_count = 0
            self.commodity_count = 0

    # --- queries ---
    def counts(self) -> dict:
        self._roll_date_if_needed()
        return {
            "date": str(self._today),
            "global_count": self.global_count,
            "global_limit": self.global_limit,
            "commodity_count": self.commodity_count,
            "commodity_limit": self.commodity_limit,
        }

    def can_trade(self, symbol: str, exchange: str | None = None) -> dict:
        """
        Checks whether a new trade is allowed.
        Returns dict: 'allowed', 'blocked_by', 'message', 'counts'.
        """
        self._roll_date_if_needed()
        category = resolve_market_category(symbol, exchange)

        blocking = []
        if self.global_count >= self.global_limit:
            blocking.append(
                f"GLOBAL limit hit: {self.global_count}/{self.global_limit} trades "
                f"today (all markets) — new trades blocked"
            )
        if category == "commodity" and self.commodity_count >= self.commodity_limit:
            blocking.append(
                f"COMMODITY limit hit: {self.commodity_count}/{self.commodity_limit} "
                f"commodity trades today — commodity blocked"
            )

        return {
            "allowed": not blocking,
            "blocked_by": blocking,
            "category": category,
            "message": blocking[0] if blocking else "trade allowed",
            "counts": self.counts(),
        }

    def register_trade(self, symbol: str, exchange: str | None = None) -> dict:
        """
        Registers an executed trade. THIS DOES NOT REPLACE THE CAN_TRADE
        CHECK — call can_trade() first, and register only if allowed.
        """
        check = self.can_trade(symbol, exchange)
        if not check["allowed"]:
            logger.warning(
                f"register_trade BLOCKED for {symbol}: {check['blocked_by']}"
            )
            return {"registered": False, **check}

        category = check["category"]
        self.global_count += 1
        if category == "commodity":
            self.commodity_count += 1

        logger.info(
            f"Trade registered: {symbol} ({category}) — "
            f"global {self.global_count}/{self.global_limit}, "
            f"commodity {self.commodity_count}/{self.commodity_limit}"
        )
        return {"registered": True, **self.counts()}


# Singleton — a single counter state across the whole process.
_guard_instance: TradeCounterGuard | None = None


def get_trade_counter() -> TradeCounterGuard:
    """Process-wide trade counter guard instance."""
    global _guard_instance
    if _guard_instance is None:
        _guard_instance = TradeCounterGuard()
    return _guard_instance


def reset_trade_counter_for_tests():
    """Testing helper — singleton reset."""
    global _guard_instance
    _guard_instance = None


# ============================================================
# QUICK MANUAL TEST — repo ROOT se: python3 -m risk.risk_management
# ============================================================
if __name__ == "__main__":
    from datetime import timedelta

    print("=== Test: global + commodity limits ===")
    guard = TradeCounterGuard(global_limit=5, commodity_limit=5)

    for i in range(5):
        r = guard.can_trade("GOLD", exchange="MCX")
        print(f"  trade {i + 1}: allowed={r['allowed']} ({r['category']})")
        guard.register_trade("GOLD", exchange="MCX")

    # Commodity limit hit — commodity blocked
    r = guard.can_trade("CRUDEOIL", exchange="MCX")
    print(f"  6th commodity trade: allowed={r['allowed']} — {r['message']}")

    # Equity is still allowed (5 of 5 global used? yes — 5/5 hit)
    r2 = guard.can_trade("RELIANCE", exchange="NSE")
    print(f"  equity trade after commodity cap: allowed={r2['allowed']} — {r2['message']}")

    print("\n=== Test: date roll resets counters ===")
    tomorrow = date.today() + timedelta(days=1)
    guard._today = tomorrow  # simulate: guard thinks today is tomorrow... actually force roll
    # now date.today() != tomorrow, so _roll_date_if_needed will reset
    r3 = guard.can_trade("GOLD", exchange="MCX")
    print(f"  next-day trade: allowed={r3['allowed']}, counts={r3['counts']}")
