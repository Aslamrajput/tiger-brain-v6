"""
Tiger Memory Core - Tiger's permanent long-term brain
======================================================

Ye module Tiger ki sabhi learned experiences ko ek persistent JSON
file mein store karta hai. Service restart, EC2 reboot, ya code
deploy hone par bhi Tiger ko yaad rahega:

  - Per-symbol win rate + PnL (Tiger knows CRUDEOIL works, BRITANNIA doesn't)
  - Per-regime performance (STRONG_TREND = 65% win rate)
  - Per-time-slot performance (13:00-14:00 = avoid, 17:00-19:00 = good)
  - Per-exit-reason PnL (scalper_stop vs trail)
  - Best/worst setups
  - Auto-blacklist for consistently losing symbols

Memory file: /tmp/tiger_memory.json

Update flow:
  Nightly replay (00:00) -> update_memory_from_trades() -> Tiger wakes up
  smarter next morning.

Read flow:
  pre_market_wake (09:00) -> recall_memory() -> Tiger logs what it remembers
  intraday_scan (every 1 min) -> get_symbol_stats() -> skip blacklisted symbols
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Optional

try:
    from config.thresholds import REPLAY
except ImportError:
    REPLAY = {}

logger = None
try:
    import logging
    logger = logging.getLogger("tiger_brain.tiger_memory")
except Exception:
    pass


MEMORY_FILE = "/tmp/tiger_memory.json"

# Blacklist a symbol after N trades with win rate below threshold
BLACKLIST_MIN_TRADES = 5
BLACKLIST_MAX_WIN_RATE_PCT = 35

# Time slot avoidance
BAD_TIME_SLOT_MIN_TRADES = 4
BAD_TIME_SLOT_MAX_WIN_RATE_PCT = 30

# How many days of history to consider (recent performance matters more)
MEMORY_WINDOW_DAYS = 60


def _empty_memory() -> dict:
    """Fresh memory structure for a newborn Tiger."""
    return {
        "created_at": datetime.now().isoformat(),
        "last_updated": None,
        "total_trades": 0,
        "total_wins": 0,
        "total_pnl": 0.0,
        "symbols": {},
        "regimes": {},
        "time_slots": {},
        "exit_reasons": {},
        "blacklist": [],
        "best_setups": [],
        "daily_history": {},
    }


def load_memory() -> dict:
    """Load Tiger's memory from disk. Returns empty memory if not found."""
    if not os.path.exists(MEMORY_FILE):
        return _empty_memory()
    try:
        with open(MEMORY_FILE, "r") as f:
            mem = json.load(f)
        # Validate structure — if corrupted, start fresh
        for key in _empty_memory():
            if key not in mem:
                mem[key] = _empty_memory()[key]
        return mem
    except (json.JSONDecodeError, OSError):
        return _empty_memory()


def save_memory(mem: dict):
    """Save Tiger's memory to disk."""
    mem["last_updated"] = datetime.now().isoformat()
    try:
        with open(MEMORY_FILE, "w") as f:
            json.dump(mem, f, indent=2, default=str)
    except OSError as exc:
        if logger:
            logger.warning(f"Tiger memory save fail: {exc}")


def _get_time_slot(entry_time_str: str) -> str:
    """Convert entry time to a 2-hour slot label.

    09:15 -> '09-11', 13:30 -> '13-15', 17:45 -> '17-19', 21:10 -> '21-23'
    """
    try:
        dt = datetime.fromisoformat(entry_time_str)
        hour = dt.hour
        slot_start = (hour // 2) * 2
        slot_end = slot_start + 2
        return f"{slot_start:02d}-{slot_end:02d}"
    except (ValueError, TypeError):
        return "unknown"


def _is_within_memory_window(entry_time_str: str, window_days: int) -> bool:
    """Check if a trade's entry time is within the memory window."""
    try:
        dt = datetime.fromisoformat(entry_time_str)
        cutoff = datetime.now() - timedelta(days=window_days)
        return dt >= cutoff
    except (ValueError, TypeError):
        return False


def update_memory_from_trades(trades: list) -> dict:
    """Rebuild Tiger's memory from all trade records.

    Called by nightly_replay at 00:00. Scans all closed trades,
    rebuilds per-symbol/regime/time-slot stats, and updates blacklist.

    Returns the updated memory dict.
    """
    mem = load_memory()

    # Keep daily_history from existing memory (don't wipe it)
    existing_daily = mem.get("daily_history", {})

    # Reset accumulators (rebuild from scratch each night)
    symbols = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0,
                                    "best_time": None, "worst_time": None})
    regimes = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    time_slots = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    exit_reasons = defaultdict(lambda: {"count": 0, "pnl": 0.0})
    daily_pnl = {}

    total_trades = 0
    total_wins = 0
    total_pnl = 0.0

    # Match ENTRY records with EXIT records by tradingsymbol
    open_entries = {}
    closed_trades = []

    for t in trades:
        status = t.get("status", "")
        tsym = t.get("tradingsymbol", "")
        if not tsym:
            continue

        if status == "OPEN":
            open_entries[tsym] = t
        elif status == "CLOSED":
            entry = open_entries.get(tsym, {})
            closed_trades.append({
                "symbol": t.get("symbol", tsym),
                "tradingsymbol": tsym,
                "entry_time": entry.get("entry_time", t.get("exit_time", "")),
                "exit_time": t.get("exit_time", ""),
                "pnl": float(t.get("pnl", 0)),
                "exit_reason": t.get("exit_reason", "unknown"),
                "regime": entry.get("regime", "UNKNOWN"),
                "setup_score": entry.get("setup_score", 0),
                "brain_alignment": entry.get("brain_alignment", 0),
                "is_scalper": entry.get("is_scalper", False),
                "exchange": entry.get("exchange", ""),
            })

    # Process each closed trade
    for ct in closed_trades:
        entry_time = ct["entry_time"]
        if not _is_within_memory_window(entry_time, MEMORY_WINDOW_DAYS):
            continue

        pnl = ct["pnl"]
        won = pnl > 0
        symbol = ct["symbol"]
        regime = ct["regime"]
        exit_reason = ct["exit_reason"]
        time_slot = _get_time_slot(entry_time)

        total_trades += 1
        if won:
            total_wins += 1
        total_pnl += pnl

        # Per-symbol stats
        s = symbols[symbol]
        s["trades"] += 1
        s["wins"] += 1 if won else 0
        s["pnl"] += pnl
        s["exchange"] = ct.get("exchange", "")

        # Per-regime stats
        r = regimes[regime]
        r["trades"] += 1
        r["wins"] += 1 if won else 0
        r["pnl"] += pnl

        # Per-time-slot stats
        ts = time_slots[time_slot]
        ts["trades"] += 1
        ts["wins"] += 1 if won else 0
        ts["pnl"] += pnl

        # Per-exit-reason stats
        er = exit_reasons[exit_reason]
        er["count"] += 1
        er["pnl"] += pnl

        # Daily PnL
        try:
            day = datetime.fromisoformat(entry_time).date().isoformat()
            daily_pnl[day] = daily_pnl.get(day, 0) + pnl
        except (ValueError, TypeError):
            pass

    # Compute win rates + avg pnl for each category
    def _finalize(d):
        for key, stats in d.items():
            trades = stats.get("trades", 0)
            stats["win_rate_pct"] = round(
                stats["wins"] / trades * 100, 1) if trades > 0 else 0
            stats["avg_pnl"] = round(
                stats["pnl"] / trades, 1) if trades > 0 else 0
        return dict(d)

    symbols = _finalize(symbols)
    regimes = _finalize(regimes)
    time_slots = _finalize(time_slots)
    exit_reasons = _finalize(exit_reasons)

    # Auto-blacklist: symbols with >= 5 trades and < 35% win rate
    blacklist = []
    for symbol, stats in symbols.items():
        if (stats["trades"] >= BLACKLIST_MIN_TRADES and
                stats["win_rate_pct"] < BLACKLIST_MAX_WIN_RATE_PCT):
            blacklist.append(symbol)
            if logger:
                logger.info(
                    f"🚫 TIGER BLACKLIST: {symbol} — {stats['trades']} trades, "
                    f"{stats['win_rate_pct']}% win rate, ₹{stats['pnl']:.0f} PnL")

    # Best setups: top 5 symbols by PnL
    best_setups = sorted(
        symbols.items(), key=lambda x: x[1]["pnl"], reverse=True
    )[:5]
    best_setups = [
        {"symbol": s, "trades": d["trades"], "win_rate": d["win_rate_pct"],
         "pnl": round(d["pnl"], 0)}
        for s, d in best_setups if d["trades"] > 0
    ]

    # Merge daily history (keep existing + add new)
    for day, pnl in daily_pnl.items():
        existing_daily[day] = round(pnl, 0)
    # Keep only last 60 days
    cutoff_day = (datetime.now() - timedelta(days=MEMORY_WINDOW_DAYS)).date().isoformat()
    existing_daily = {k: v for k, v in existing_daily.items() if k >= cutoff_day}

    # Save updated memory
    mem["total_trades"] = total_trades
    mem["total_wins"] = total_wins
    mem["total_pnl"] = round(total_pnl, 0)
    mem["symbols"] = symbols
    mem["regimes"] = regimes
    mem["time_slots"] = time_slots
    mem["exit_reasons"] = exit_reasons
    mem["blacklist"] = blacklist
    mem["best_setups"] = best_setups
    mem["daily_history"] = existing_daily

    save_memory(mem)
    return mem


def recall_memory() -> str:
    """Generate Tiger's morning recall message.

    Called at pre_market_wake (09:00). Tiger logs what it remembers
    so the user can see Tiger is learning.

    Returns the recall string (also logged by caller).
    """
    mem = load_memory()

    if mem["total_trades"] == 0:
        return "📋 Tiger Memory: No trade history yet — first day learning."

    win_rate = round(
        mem["total_wins"] / mem["total_trades"] * 100, 1
    ) if mem["total_trades"] > 0 else 0

    lines = [
        "=" * 60,
        "🐅 TIGER MEMORY RECALLED:",
        f"   Last {MEMORY_WINDOW_DAYS} days: {mem['total_trades']} trades, "
        f"{mem['total_wins']} wins ({win_rate}%), net ₹{mem['total_pnl']:+,.0f}",
    ]

    # Best symbols
    if mem.get("best_setups"):
        best = mem["best_setups"][0]
        lines.append(
            f"   Best symbol: {best['symbol']} "
            f"({best['win_rate']}% win, ₹{best['pnl']:+.0f} PnL)")

    # Blacklist
    if mem.get("blacklist"):
        bl = ", ".join(mem["blacklist"][:5])
        lines.append(f"   🚫 Avoid: {bl} (auto-blacklisted, <35% win rate)")

    # Best time slot
    time_slots = mem.get("time_slots", {})
    if time_slots:
        best_slot = max(time_slots.items(), key=lambda x: x[1].get("pnl", 0))
        worst_slot = min(time_slots.items(), key=lambda x: x[1].get("pnl", 0))
        if best_slot[1].get("trades", 0) >= 3:
            lines.append(
                f"   Best time: {best_slot[0]} "
                f"({best_slot[1].get('win_rate_pct', 0)}% win, "
                f"₹{best_slot[1].get('pnl', 0):+.0f})")
        if worst_slot[1].get("trades", 0) >= BAD_TIME_SLOT_MIN_TRADES and \
                worst_slot[1].get("win_rate_pct", 0) < BAD_TIME_SLOT_MAX_WIN_RATE_PCT:
            lines.append(
                f"   ⚠️ Avoid time: {worst_slot[0]} "
                f"({worst_slot[1].get('win_rate_pct', 0)}% win, "
                f"₹{worst_slot[1].get('pnl', 0):+.0f})")

    # Best regime
    regimes = mem.get("regimes", {})
    if regimes:
        best_regime = max(regimes.items(), key=lambda x: x[1].get("pnl", 0))
        if best_regime[1].get("trades", 0) >= 3:
            lines.append(
                f"   Best regime: {best_regime[0]} "
                f"({best_regime[1].get('win_rate_pct', 0)}% win)")

    lines.append("=" * 60)
    return "\n".join(lines)


def is_symbol_blacklisted(symbol: str) -> bool:
    """Check if Tiger's memory has blacklisted this symbol."""
    mem = load_memory()
    return symbol in mem.get("blacklist", [])


def get_symbol_stats(symbol: str) -> Optional[dict]:
    """Get Tiger's memory stats for a specific symbol.

    Returns None if no history. Used by intraday_scan to decide
    whether to trade this symbol.
    """
    mem = load_memory()
    return mem.get("symbols", {}).get(symbol)


def is_bad_time_slot(entry_time: datetime = None) -> tuple[bool, str]:
    """Check if current time is a historically bad slot.

    Returns (is_bad, slot_label). If is_bad=True, Tiger should
    reduce position size or skip trading.
    """
    mem = load_memory()
    entry_time = entry_time or datetime.now()
    slot = _get_time_slot(entry_time.isoformat())
    stats = mem.get("time_slots", {}).get(slot)
    if not stats:
        return False, slot
    if (stats.get("trades", 0) >= BAD_TIME_SLOT_MIN_TRADES and
            stats.get("win_rate_pct", 0) < BAD_TIME_SLOT_MAX_WIN_RATE_PCT):
        return True, slot
    return False, slot


def get_memory_summary() -> dict:
    """Get a compact summary of Tiger's memory for logging/debugging."""
    mem = load_memory()
    return {
        "total_trades": mem.get("total_trades", 0),
        "total_wins": mem.get("total_wins", 0),
        "total_pnl": mem.get("total_pnl", 0),
        "blacklist_count": len(mem.get("blacklist", [])),
        "symbols_tracked": len(mem.get("symbols", {})),
        "last_updated": mem.get("last_updated"),
    }
