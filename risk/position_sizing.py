"""
Tiger Brain V6+V7 — Risk Management Module (Section 13, 26)
==============================================================
Ye module sabse critical safety layer hai — Blueprint khud kehta hai
(Section 13): "duniya ke 90% retail traders/algos fail isliye nahi
hote ki unka signal galat hai, balki isliye ki risk management kamzor
hota hai."

⚠️ IMPORTANT: `stage4_decision_lock.py` mein basic position-sizing already
hai, par safety circuit-breakers (daily max-loss, consecutive-loss pause)
WAHAN NAHI THE — ye ek gap tha jo abhi is file mein fill ho raha hai.
Jab poora system chalega (automation/scheduler.py mein), ye functions
Stage 4 se PEHLE call hone chahiye — agar circuit breaker already
trigger hai, to Stage 4 tak pahunchna hi nahi chahiye, seedha NO_TRADE.
"""

try:
    from config.thresholds import RISK
except ImportError:
    raise ImportError("Repo ROOT se chalao, 'risk/' ke andar se nahi.")


def check_daily_loss_circuit_breaker(daily_pnl: float, account_capital: float) -> dict:
    """
    Section 13.2 — Daily Max-Loss Circuit Breaker. Agar aaj ka loss capital
    ke tay % se zyada ho gaya hai, poora din ke liye trading band.

    Args:
        daily_pnl: aaj ka profit/loss (negative number = loss)
        account_capital: total account capital

    Returns:
        dict:
            'breaker_triggered': bool
            'daily_loss_pct': float (positive number, jitna % loss hua)
            'limit_pct': float
            'message': str
    """
    if daily_pnl >= 0:
        return {
            "breaker_triggered": False, "daily_loss_pct": 0.0,
            "limit_pct": RISK["DAILY_MAX_LOSS_PCT"],
            "message": "Aaj profit mein hai ya breakeven — koi issue nahi",
        }

    daily_loss_pct = abs(daily_pnl) / account_capital * 100
    triggered = daily_loss_pct >= RISK["DAILY_MAX_LOSS_PCT"]

    return {
        "breaker_triggered": triggered,
        "daily_loss_pct": round(daily_loss_pct, 2),
        "limit_pct": RISK["DAILY_MAX_LOSS_PCT"],
        "message": (
            f"🚫 Daily Max-Loss Circuit Breaker TRIGGERED: {daily_loss_pct:.2f}% loss "
            f">= {RISK['DAILY_MAX_LOSS_PCT']}% limit — aaj ke liye system band, "
            f"koi override nahi (Section 13.2)"
            if triggered else
            f"Daily loss {daily_loss_pct:.2f}% — limit {RISK['DAILY_MAX_LOSS_PCT']}% se kam hai"
        ),
    }


def check_consecutive_loss_pause(recent_trade_pnls: list) -> dict:
    """
    Section 13.3 — Max Consecutive Loss Pause. Agar lagataar N trades
    (config se) loss mein gaye, system ko kuch der pause hona chahiye.

    Args:
        recent_trade_pnls: list of recent trade P&L values, chronological
                            order mein (sabse recent trade sabse aakhir mein)
                            jaise [150, -80, -45, -30] (aakhri 3 loss hain)

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
            "pause_minutes": 0, "message": "Koi trade history nahi hai abhi",
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
            f"🚫 {consecutive_losses} lagataar losses — {RISK['CONSECUTIVE_LOSS_PAUSE_MINUTES']} "
            f"minute ke liye pause (Section 13.3: 'shayad regime classification "
            f"galat ho raha hai, rukkar dobara assess karna better hai')"
            if triggered else
            f"{consecutive_losses} consecutive losses — {RISK['MAX_CONSECUTIVE_LOSSES']} "
            f"ki limit se kam hai"
        ),
    }


def apply_theta_decay_adjustment(position_size_pct: float, days_to_expiry: int) -> dict:
    """
    Section 13.7 — Theta Decay Awareness. Expiry ke jitna paas, utna
    conservative sizing (0 DTE pe size 50% kar dena).

    Args:
        position_size_pct: originally calculated position size %
        days_to_expiry: kitne din baaki hain expiry tak (0 = aaj hi expiry)

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
            f"0 DTE (aaj hi expiry) — size {multiplier}x kiya gaya "
            f"({position_size_pct}% -> {adjusted:.2f}%), theta decay bahut tez hai"
        )
    else:
        multiplier = 1.0
        adjusted = position_size_pct
        message = f"{days_to_expiry} din baaki expiry tak — koi extra theta adjustment nahi"

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
    Ek hi jagah se saare circuit-breakers check karna — Stage 4 se PEHLE
    ye call karna chahiye. Agar koi bhi breaker trigger hai, trading is
    din/is waqt ke liye ruk jaani chahiye, chahe signal kitna bhi strong ho.

    Returns:
        dict:
            'safe_to_trade': bool
            'blocking_reasons': list of str (khaali agar sab theek hai)
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
# Chalane ka tarika: repo ROOT se → python3 -m risk.position_sizing
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

    print("\n✅ Test complete — koi crash nahi hua.")
      
