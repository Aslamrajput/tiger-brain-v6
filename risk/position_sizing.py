"""
Tiger Brain V6+V7 — Risk Management Module (Section 13, 26)
==============================================================
This module is the most critical safety layer — the Blueprint itself states
(Section 13): "90% of retail traders/algos fail not because their signal is
wrong, but because their risk management is weak."

⚠️ IMPORTANT: `stage4_decision_lock.py` already has basic position-sizing,
but the safety circuit-breakers (daily max-loss, consecutive-loss pause)
were NOT there — this was a gap being filled in this file now.
When the full system runs (automation/scheduler.py), these functions
should be called BEFORE Stage 4 — if a circuit breaker is already
triggered, the flow should not even reach Stage 4; it should go straight
to NO_TRADE.
"""

try:
    from config.thresholds import RISK
except ImportError:
    raise ImportError("Run from repo ROOT, not from inside 'risk/'.")


def check_daily_loss_circuit_breaker(daily_pnl: float, account_capital: float) -> dict:
    """
    Section 13.2 — Daily Max-Loss Circuit Breaker. If today's loss exceeds a
    set % of capital, trading is halted for the rest of the day.

    Args:
        daily_pnl: today's profit/loss (negative number = loss)
        account_capital: total account capital

    Returns:
        dict:
            'breaker_triggered': bool
            'daily_loss_pct': float (positive number, the % loss)
            'limit_pct': float
            'message': str
    """
    if daily_pnl >= 0:
        return {
            "breaker_triggered": False, "daily_loss_pct": 0.0,
            "limit_pct": RISK["DAILY_MAX_LOSS_PCT"],
            "message": "Today is in profit or breakeven — no issue",
        }

    daily_loss_pct = abs(daily_pnl) / account_capital * 100
    triggered = daily_loss_pct >= RISK["DAILY_MAX_LOSS_PCT"]

    return {
        "breaker_triggered": triggered,
        "daily_loss_pct": round(daily_loss_pct, 2),
        "limit_pct": RISK["DAILY_MAX_LOSS_PCT"],
        "message": (
            f"🚫 Daily Max-Loss Circuit Breaker TRIGGERED: {daily_loss_pct:.2f}% loss "
            f">= {RISK['DAILY_MAX_LOSS_PCT']}% limit — system halted for today, "
            f"no override (Section 13.2)"
            if triggered else
            f"Daily loss {daily_loss_pct:.2f}% — below the {RISK['DAILY_MAX_LOSS_PCT']}% limit"
        ),
    }


def check_consecutive_loss_pause(recent_trade_pnls: list) -> dict:
    """
    Section 13.3 — Max Consecutive Loss Pause. If N consecutive trades
    (from config) end in loss, the system should pause for a while.

    Args:
        recent_trade_pnls: list of recent trade P&L values, in chronological
                            order (most recent trade last)
                            e.g. [150, -80, -45, -30] (last 3 are losses)

    Returns:
        dict:
            'pause_triggered': bool
            'consecutive_losses': int
            'pause_minutes': int
            'message': str
    """
    if not recent_trade_pnls:
        return {
            "pause_triggered": False, "consecutive_losses": 0,
            "pause_minutes": 0, "message": "No trade history yet",
        }

    consecutive_losses = 0
    for pnl in reversed(recent_trade_pnls):
        if pnl < 0:
            consecutive_losses += 1
        else:
            break

    triggered = consecutive_losses >= RISK["MAX_CONSECUTIVE_LOSSES"]

    return {
        "pause_triggered": triggered,
        "consecutive_losses": consecutive_losses,
        "pause_minutes": RISK["CONSECUTIVE_LOSS_PAUSE_MINUTES"] if triggered else 0,
        "message": (
            f"🚫 {consecutive_losses} consecutive losses — pausing for {RISK['CONSECUTIVE_LOSS_PAUSE_MINUTES']} "
            f"minutes (Section 13.3: 'the regime classification may be wrong; "
            f"it is better to stop and re-assess')"
            if triggered else
            f"{consecutive_losses} consecutive losses — below the "
            f"{RISK['MAX_CONSECUTIVE_LOSSES']} limit"
        ),
    }


def apply_theta_decay_adjustment(position_size_pct: float, days_to_expiry: int) -> dict:
    """
    Section 13.7 — Theta Decay Awareness. The closer to expiry, the more
    conservative the sizing (reduce size to 50% on 0 DTE).

    Args:
        position_size_pct: originally calculated position size %
        days_to_expiry: days remaining to expiry (0 = expiry today)

    Returns:
        dict:
            'adjusted_size_pct': float
            'multiplier_applied': float
            'message': str
    """
    if days_to_expiry == 0:
        multiplier = RISK["THETA_DECAY_0DTE_SIZE_MULTIPLIER"]
        adjusted = position_size_pct * multiplier
        message = (
            f"0 DTE (expiry today) — size scaled to {multiplier}x "
            f"({position_size_pct}% -> {adjusted:.2f}%), theta decay is very rapid"
        )
    else:
        multiplier = 1.0
        adjusted = position_size_pct
        message = f"{days_to_expiry} days to expiry — no extra theta adjustment"

    return {
        "adjusted_size_pct": round(adjusted, 2),
        "multiplier_applied": multiplier,
        "message": message,
    }


def pre_trade_safety_check(
    daily_pnl: float,
    account_capital: float,
    recent_trade_pnls: list,
) -> dict:
    """
    Checks all circuit-breakers from one place — call this BEFORE Stage 4.
    If any breaker is triggered, trading should halt for the day/moment,
    no matter how strong the signal is.

    Returns:
        dict:
            'safe_to_trade': bool
            'blocking_reasons': list of str (empty if all clear)
    """
    blocking_reasons = []

    daily_check = check_daily_loss_circuit_breaker(daily_pnl, account_capital)
    if daily_check["breaker_triggered"]:
        blocking_reasons.append(daily_check["message"])

    consecutive_check = check_consecutive_loss_pause(recent_trade_pnls)
    if consecutive_check["pause_triggered"]:
        blocking_reasons.append(consecutive_check["message"])

    return {
        "safe_to_trade": len(blocking_reasons) == 0,
        "blocking_reasons": blocking_reasons,
    }


# ============================================================
# QUICK MANUAL TEST
# How to run: from repo ROOT → python3 -m risk.position_sizing
# ============================================================
if __name__ == "__main__":
    print("=== Test 1: Daily Loss Circuit Breaker — normal day ===")
    print(check_daily_loss_circuit_breaker(daily_pnl=-1000, account_capital=50000))

    print("\n=== Test 2: Daily Loss Circuit Breaker — triggered ===")
    print(check_daily_loss_circuit_breaker(daily_pnl=-3500, account_capital=50000))

    print("\n=== Test 3: Consecutive Loss Pause — 3 losses in a row ===")
    print(check_consecutive_loss_pause([200, -100, -150, -80]))

    print("\n=== Test 4: Theta Decay — 0 DTE ===")
    print(apply_theta_decay_adjustment(position_size_pct=3.0, days_to_expiry=0))

    print("\n=== Test 5: Combined Pre-Trade Safety Check ===")
    result = pre_trade_safety_check(
        daily_pnl=-3500, account_capital=50000, recent_trade_pnls=[-100, -150, -80]
    )
    print(result)

    print("\n✅ Test complete — no crash occurred.")
      
