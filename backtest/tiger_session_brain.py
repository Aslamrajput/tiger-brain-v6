"""
Tiger Brain V18 — SESSION COMMANDER (Brain 7)
===============================================
Yeh Tiger ka time-aware dimag hai — market session ke hisab se
Tiger ka mode change karta hai. 9:15 AM se 11:30 PM tak Tiger
ek 24-hour shikari ban jaata hai.

MARKET TIMELINE (Indian Standard Time):
  9:15-10:30   → MORNING BURST    (index+stock, aggressive opening momentum)
  10:30-13:00  → TREND HUNT       (stock zones, steady quality picks)
  13:00-14:30  → DISCOUNT BUY     (cheap premium accumulation — KEY window!)
  14:30-15:15  → POWER HOUR       (max aggression, ride explosions)
  15:15-17:00  → TRANSITION       (NSE closed, MCX warming up)
  17:00-20:00  → COMMODITY OPEN   (MCX crude/gold/silver start)
  20:00-23:00  → NIGHT RUSH       (international session, biggest commodity moves)
  23:00-23:15  → SQUARE OFF       (close all MCX intraday)

Each session has different parameters:
  - Aggressiveness (score threshold, trade quota)
  - Segment focus (NSE index vs stock vs MCX commodity)
  - Capital allocation %
  - Strike preference (OTM/ATM/ITM)
  - Risk tolerance

"BINA SHIKAR LIYE GHAR NAHI" rule:
  - If no NSE trade by 15:15 → switch to MCX aggressively
  - If no MCX trade by 20:00 → aggressive scan, lower score threshold
  - Min 3 trades/day guaranteed (Tiger always hunts)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import time

logger = logging.getLogger("tiger_brain.session_brain")


# ============================================================
# SESSION DEFINITIONS — Tiger's 7 hunting modes
# ============================================================
SESSION_MORNING_BURST = "MORNING_BURST"
SESSION_TREND_HUNT = "TREND_HUNT"
SESSION_DISCOUNT_BUY = "DISCOUNT_BUY"
SESSION_POWER_HOUR = "POWER_HOUR"
SESSION_COMMODITY_OPEN = "COMMODITY_OPEN"
SESSION_NIGHT_RUSH = "NIGHT_RUSH"
SESSION_SQUARE_OFF = "SQUARE_OFF"
SESSION_OFF_HOURS = "OFF_HOURS"


@dataclass
class SessionConfig:
    """Configuration for a market session — Tiger's mode parameters."""
    name: str
    label: str
    time_start: time
    time_end: time
    segment_focus: str          # "nse" / "mcx" / "both"
    score_threshold: float      # minimum score to enter in this session
    max_trades_this_session: int # trade quota for this session
    capital_allocation_pct: float  # % of available capital for this session
    preferred_strike: str       # "OTM" / "ATM" / "ITM" / "any"
    aggressiveness: str         # "LOW" / "MEDIUM" / "HIGH" / "MAX"
    description: str
    notes: list[str] = field(default_factory=list)

    def is_active(self, t: time) -> bool:
        """Check if a given time falls in this session."""
        return self.time_start <= t < self.time_end


# ============================================================
# SESSION SCHEDULE — full day hunting plan (IST times)
# ============================================================
SESSION_SCHEDULE: list[SessionConfig] = [
    SessionConfig(
        name=SESSION_MORNING_BURST,
        label="🔥 MORNING BURST",
        time_start=time(9, 15),
        time_end=time(10, 30),
        segment_focus="nse",
        score_threshold=78.0,        # standard rocket gate
        max_trades_this_session=5,   # aggressive — opening momentum
        capital_allocation_pct=35.0,
        preferred_strike="ITM",      # fast delta, ride opening burst
        aggressiveness="HIGH",
        description="Opening volatility burst — ride index/stock explosions",
        notes=[
            "First 75 mins = biggest moves of the day",
            "Index options focus (NIFTY/BANKNIFTY momentum)",
            "ORB + zone touch = strongest signals",
        ],
    ),
    SessionConfig(
        name=SESSION_TREND_HUNT,
        label="📈 TREND HUNT",
        time_start=time(10, 30),
        time_end=time(13, 0),
        segment_focus="nse",
        score_threshold=80.0,        # slightly higher — quality over quantity
        max_trades_this_session=3,
        capital_allocation_pct=20.0,
        preferred_strike="ATM",      # balanced — trends develop slowly
        aggressiveness="MEDIUM",
        description="Steady trend hunting — stock zone bounces, quality picks",
        notes=[
            "Volatility settles, trends emerge",
            "Stock-specific S/D zone bounces",
            "ATM strikes — balanced delta/theta",
        ],
    ),
    SessionConfig(
        name=SESSION_DISCOUNT_BUY,
        label="💰 DISCOUNT BUY",
        time_start=time(13, 0),
        time_end=time(14, 30),
        segment_focus="nse",
        score_threshold=75.0,        # lower — IV discount compensates
        max_trades_this_session=3,
        capital_allocation_pct=15.0,
        preferred_strike="OTM",      # cheapest premium, max gamma
        aggressiveness="MEDIUM",
        description="Lunch lull = IV LOWEST = premium DISCOUNT → buy cheap!",
        notes=[
            "KEY SESSION: IV at daily low = premium cheapest",
            "Pre-position for power hour explosion",
            "OTM strikes = max gamma, IV expand = 3x-5x",
            "Brain 6 (Premium) is most active here",
        ],
    ),
    SessionConfig(
        name=SESSION_POWER_HOUR,
        label="⚡ POWER HOUR",
        time_start=time(14, 30),
        time_end=time(15, 15),
        segment_focus="nse",
        score_threshold=78.0,
        max_trades_this_session=3,
        capital_allocation_pct=20.0,
        preferred_strike="ITM",      # fast delta, ride explosion
        aggressiveness="MAX",
        description="Institutional repositioning — biggest NSE moves of day",
        notes=[
            "Last 45 mins = institutional power hour",
            "Max aggression — ride closing explosions",
            "Discount buys from lunch now paying off!",
        ],
    ),
    SessionConfig(
        name=SESSION_COMMODITY_OPEN,
        label="🛢️ COMMODITY OPEN",
        time_start=time(17, 0),
        time_end=time(20, 0),
        segment_focus="mcx",
        score_threshold=76.0,
        max_trades_this_session=5,
        capital_allocation_pct=80.0,
        preferred_strike="ATM",
        aggressiveness="HIGH",
        description="MCX evening session — crude/gold/silver full throttle",
        notes=[
            "NSE closed, MCX takes over",
            "Crude oil big moves on US data",
            "ATM strikes — commodity premiums fair",
            "80% capital when confident (fully sure)",
        ],
    ),
    SessionConfig(
        name=SESSION_NIGHT_RUSH,
        label="🌙 NIGHT RUSH",
        time_start=time(20, 0),
        time_end=time(23, 0),
        segment_focus="mcx",
        score_threshold=75.0,        # slightly lower — night = big moves
        max_trades_this_session=5,   # aggressive — biggest commodity moves
        capital_allocation_pct=80.0,
        preferred_strike="ATM",
        aggressiveness="HIGH",
        description="International session — gold/silver/crude biggest moves",
        notes=[
            "US/Europe markets active = max commodity volatility",
            "Gold/Silver 50-200% moves possible",
            "Tiger's night hunting mode — full throttle",
            "80% capital when confident (fully sure)",
        ],
    ),
    SessionConfig(
        name=SESSION_SQUARE_OFF,
        label="🔒 SQUARE OFF",
        time_start=time(23, 0),
        time_end=time(23, 30),
        segment_focus="mcx",
        score_threshold=999.0,       # no new entries
        max_trades_this_session=0,
        capital_allocation_pct=0.0,
        preferred_strike="ATM",
        aggressiveness="LOW",
        description="MCX square-off — close all intraday positions",
        notes=["No new entries — close everything by 23:15"],
    ),
]


# ============================================================
# SESSION DETECTION — what session is Tiger in right now?
# ============================================================
def get_current_session(t: time, segment: str = "nse") -> SessionConfig | None:
    """
    Determine which session is active at a given time.

    Args:
        t: IST time (datetime.time)
        segment: "nse" or "mcx" — filters which sessions apply

    Returns:
        SessionConfig for the active session, or None if off-hours
    """
    for session in SESSION_SCHEDULE:
        if not session.is_active(t):
            continue
        # Filter by segment
        if segment == "mcx" and session.segment_focus == "nse":
            # MCX trades in morning too (9:00-11:30), but our NSE sessions
            # overlap. For MCX in morning, use COMMODITY_OPEN params.
            if session.name in (SESSION_MORNING_BURST, SESSION_TREND_HUNT,
                                SESSION_DISCOUNT_BUY, SESSION_POWER_HOUR):
                # MCX morning = treat as commodity open
                continue
        if segment == "nse" and session.segment_focus == "mcx":
            # NSE is closed during MCX evening sessions
            if session.name in (SESSION_COMMODITY_OPEN, SESSION_NIGHT_RUSH,
                                SESSION_SQUARE_OFF):
                continue
        return session

    # Off-hours
    return None


def get_session_score_threshold(t: time, segment: str = "nse") -> float:
    """Get the minimum score threshold for the current session."""
    session = get_current_session(t, segment)
    if session is None:
        return 999.0  # off-hours — no entries
    return session.score_threshold


def get_session_trade_quota(t: time, segment: str = "nse") -> int:
    """Get the max trades allowed in the current session."""
    session = get_current_session(t, segment)
    if session is None:
        return 0
    return session.max_trades_this_session


def get_session_capital_pct(t: time, segment: str = "nse") -> float:
    """Get the capital allocation % for the current session."""
    session = get_current_session(t, segment)
    if session is None:
        return 0.0
    return session.capital_allocation_pct


def get_session_preferred_strike(t: time, segment: str = "nse") -> str:
    """Get the preferred strike type for the current session."""
    session = get_current_session(t, segment)
    if session is None:
        return "ATM"
    return session.preferred_strike


# ============================================================
# "BINA SHIKAR LIYE GHAR NAHI" — Tiger's hunting guarantee
# ============================================================
@dataclass
class HuntStatus:
    """Tracks Tiger's daily hunt progress — ensures Tiger never goes home empty."""
    date: str
    nse_trades: int = 0
    mcx_trades: int = 0
    total_trades: int = 0
    nse_profit: float = 0.0
    mcx_profit: float = 0.0
    last_session_checked: str = ""

    @property
    def is_empty_hunt(self) -> bool:
        """True if Tiger hasn't caught any prey yet."""
        return self.total_trades == 0

    @property
    def needs_aggressive_hunt(self) -> bool:
        """True if Tiger is running late and needs to hunt harder."""
        return self.total_trades < 3

    def record_trade(self, segment: str, pnl: float = 0.0) -> None:
        """Record a completed trade."""
        if segment == "commodity":
            self.mcx_trades += 1
            self.mcx_profit += pnl
        else:
            self.nse_trades += 1
            self.nse_profit += pnl
        self.total_trades += 1

    def summary(self) -> str:
        return (
            f"NSE: {self.nse_trades} trades (₹{self.nse_profit:+,.0f}) | "
            f"MCX: {self.mcx_trades} trades (₹{self.mcx_profit:+,.0f}) | "
            f"Total: {self.total_trades}"
        )


def should_force_hunt(
    t: time,
    hunt: HuntStatus,
    segment: str = "nse",
) -> tuple[bool, float, str]:
    """
    "Bina shikar liye ghar nahi" — should Tiger lower standards to guarantee a trade?

    Rules:
      - If NSE session ending (14:30+) and < 3 NSE trades → lower threshold
      - If MCX night rush (20:00+) and < 3 total trades → lower threshold hard
      - If MCX near close (22:00+) and 0 trades → force at least 1 trade

    Args:
        t: current IST time
        hunt: HuntStatus for today
        segment: "nse" or "mcx"

    Returns:
        (force_hunt, adjusted_threshold, reason)
    """
    # NSE power hour with few trades → moderate relaxation
    if segment == "nse" and time(14, 30) <= t < time(15, 15):
        if hunt.nse_trades < 2:
            return (True, 72.0, f"force_hunt: NSE closing soon, only {hunt.nse_trades} NSE trades")

    # MCX night rush with few trades → aggressive relaxation
    if segment == "mcx" and time(20, 0) <= t < time(22, 0):
        if hunt.total_trades < 3:
            return (True, 70.0, f"force_hunt: night rush, only {hunt.total_trades} total trades — HUNT!")

    # MCX near close with 0 trades → force minimum 1 trade
    if segment == "mcx" and time(22, 0) <= t < time(23, 0):
        if hunt.total_trades == 0:
            return (True, 65.0, "force_hunt: ZERO trades today — Tiger MUST hunt before close!")

    # MCX near close with very few trades → still try
    if segment == "mcx" and time(22, 30) <= t < time(23, 0):
        if hunt.total_trades < 2:
            return (True, 68.0, f"force_hunt: only {hunt.total_trades} trades — last chance!")

    return (False, 0.0, "")


# ============================================================
# DAILY SESSION REPORT — for backtest output
# ============================================================
def get_session_label(t: time, segment: str = "nse") -> str:
    """Get a human-readable label for the current session."""
    session = get_current_session(t, segment)
    if session is None:
        if time(15, 15) <= t < time(17, 0):
            return "⏸️ TRANSITION (NSE closed, MCX warming)"
        return "😴 OFF HOURS"
    return session.label


def print_session_schedule() -> str:
    """Print the full daily hunting schedule — for pre-market announcement."""
    lines = []
    lines.append("=" * 72)
    lines.append("  🐅 TIGER SESSION COMMANDER — DAILY HUNT SCHEDULE")
    lines.append("=" * 72)
    for s in SESSION_SCHEDULE:
        lines.append(
            f"  {s.label:25s} {s.time_start.strftime('%H:%M')}-{s.time_end.strftime('%H:%M')}  "
            f"| Score≥{s.score_threshold:.0f} | Max {s.max_trades_this_session} trades | "
            f"{s.capital_allocation_pct:.0f}% capital | {s.preferred_strike}"
        )
        for note in s.notes:
            lines.append(f"    ⚡ {note}")
    lines.append("-" * 72)
    lines.append("  🐅 BINA SHIKAR LIYE GHAR NAHI — min 3 trades/day guaranteed!")
    lines.append("=" * 72)
    return "\n".join(lines)
