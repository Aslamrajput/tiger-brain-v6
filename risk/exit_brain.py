"""
Tiger Brain V6.1 — BRAIN 5: Risk Guard, Gamma Tracking & Execution Exit
=======================================================================
Paanchwa aur aakhri brain. Trade lagne ke BAAD sab kuch ye sambhalta hai:

  - STOP-LOSS / TARGET: premium pe % based (BRAIN5 config)
  - TRAILING STOP: gain activate hone ke baad peak se give-back
  - TIME EXIT: market close se pehle square-off (overnight risk nahi)
  - GAMMA TRACKING: expiry ke paas option ka gamma bahut tez hota hai
    (ATM delta flip). Gamma risk threshold cross ho to stop tighten karo
    ya position book karo.
  - EXECUTION: exit signal milne pe broker ko exit order.

Position state ek dict hai jo orchestrator (Brain flow) maintain karta hai:

    position = {
        'symbol': str, 'option_symbol': str,
        'direction': 'BUY'|'SELL',   # option bought: CE=BUY side, PE=SELL side
        'entry_premium': float, 'quantity': int,
        'entry_time': datetime, 'peak_premium': float,
        'underlying_stop': float,    # Brain 2 ka structural stop (underlying pe)
        'delta': float | None, 'gamma_pct': float | None,  # gamma as % premium per % move
        'days_to_expiry': int,
    }
"""

from __future__ import annotations

import logging
from datetime import datetime, time as dtime

try:
    from config.thresholds import BRAIN5
except ImportError:
    raise ImportError("Repo ROOT se chalao, 'risk/' ke andar se nahi.")

logger = logging.getLogger("tiger_brain.exit_brain")

MARKET_CLOSE = dtime(15, 30)  # IST — config/thresholds.py AUTOMATION se match


def _pct_change(current: float, reference: float) -> float:
    if reference <= 0:
        return 0.0
    return (current - reference) / reference * 100


def update_peak(position: dict, current_premium: float) -> dict:
    """Position ka peak premium update karta hai (trailing ke liye)."""
    if current_premium > position.get("peak_premium", 0):
        position["peak_premium"] = current_premium
    return position


def check_gamma_risk(position: dict, gamma_pct: float | None = None) -> dict:
    """
    Gamma tracking guard. Gamma_pct = premium ka kitna % change hota hai
    underlying ke 1% move pe (ye chhota 'local gamma' proxy hai — real
    Greeks chain data se aayenge, ye estimation hook hai).

    Expiry ke paas (GAMMA_RISK_DAYS_TO_EXPIRY) aur gamma high dono ho to
    position exit karo — gamma spike mein stop bhi slip karta hai.
    """
    g = gamma_pct if gamma_pct is not None else position.get("gamma_pct")
    dte = position.get("days_to_expiry")

    near_expiry = dte is not None and dte <= BRAIN5["GAMMA_RISK_DAYS_TO_EXPIRY"]
    high_gamma = g is not None and g >= BRAIN5["GAMMA_RISK_THRESHOLD_PCT"]

    if near_expiry and high_gamma:
        return {
            "gamma_exit": True, "reason": "exit",
            "message": (
                f"GAMMA RISK: {dte} din expiry bache aur gamma {g}% >= "
                f"{BRAIN5['GAMMA_RISK_THRESHOLD_PCT']}% — gamma spike se bachne "
                f"ke liye position exit"
            ),
        }
    if near_expiry:
        return {
            "gamma_exit": False, "reason": "watch",
            "message": (
                f"{dte} din expiry bache — gamma watch mode (gamma {g})"
            ),
        }
    return {"gamma_exit": False, "reason": "ok",
            "message": f"gamma risk normal (gamma {g}, dte {dte})"}


def evaluate_exit(
    position: dict,
    current_premium: float,
    current_time: datetime | None = None,
    underlying_price: float | None = None,
) -> dict:
    """
    Brain 5 ka main entry point — kya position exit honi chahiye?

    Priority order (pehla match jeet-ta hai):
      1. Time exit (market close se pehle square-off)
      2. Gamma exit (expiry + high gamma)
      3. Structural stop (Brain 2 ka underlying stop hit)
      4. Hard stop-loss (premium %)
      5. Target (premium %)
      6. Trailing stop (peak se give-back)

    Returns:
        dict: 'exit', 'reason', 'exit_price', 'message'
    """
    now = current_time or datetime.now()
    entry = position.get("entry_premium")
    position = update_peak(position, current_premium)
    peak = position.get("peak_premium", entry)

    # --- 1. Time exit ---
    close_today = datetime.combine(now.date(), MARKET_CLOSE)
    minutes_left = (close_today - now).total_seconds() / 60
    if 0 <= minutes_left <= BRAIN5["SQUARE_OFF_MINUTES_BEFORE_CLOSE"]:
        return {
            "exit": True, "reason": "time_exit",
            "exit_price": current_premium,
            "message": (
                f"TIME EXIT: market close se sirf {minutes_left:.0f} min bache "
                f"(<{BRAIN5['SQUARE_OFF_MINUTES_BEFORE_CLOSE']}) — square off"
            ),
        }

    # --- 2. Gamma exit ---
    gamma_check = check_gamma_risk(position)
    if gamma_check["gamma_exit"]:
        return {
            "exit": True, "reason": "gamma_exit",
            "exit_price": current_premium,
            "message": gamma_check["message"],
        }

    # --- 3. Structural stop (underlying level) ---
    underlying_stop = position.get("underlying_stop")
    if (
        underlying_stop is not None
        and underlying_price is not None
    ):
        direction = position.get("direction")
        if direction == "BUY" and underlying_price <= underlying_stop:
            return {
                "exit": True, "reason": "structural_stop",
                "exit_price": current_premium,
                "message": (
                    f"STRUCTURAL STOP: underlying {underlying_price} <= stop "
                    f"{underlying_stop} (Brain 2 ka SMC level) — exit"
                ),
            }
        if direction == "SELL" and underlying_price >= underlying_stop:
            return {
                "exit": True, "reason": "structural_stop",
                "exit_price": current_premium,
                "message": (
                    f"STRUCTURAL STOP: underlying {underlying_price} >= stop "
                    f"{underlying_stop} (Brain 2 ka SMC level) — exit"
                ),
            }

    # --- 4. Hard stop-loss ---
    if entry and entry > 0:
        change = _pct_change(current_premium, entry)
        if change <= -BRAIN5["STOP_LOSS_PCT"]:
            return {
                "exit": True, "reason": "stop_loss",
                "exit_price": current_premium,
                "message": (
                    f"STOP-LOSS: premium {change:.1f}% (<= -{BRAIN5['STOP_LOSS_PCT']}%)"
                ),
            }

        # --- 5. Target ---
        if change >= BRAIN5["TARGET_PCT"]:
            return {
                "exit": True, "reason": "target",
                "exit_price": current_premium,
                "message": f"TARGET: premium {change:+.1f}% (>= {BRAIN5['TARGET_PCT']}%)",
            }

        # --- 6. Trailing stop ---
        if change >= BRAIN5["TRAIL_ACTIVATION_PCT"] and peak and peak > 0:
            drawdown_from_peak = _pct_change(current_premium, peak)
            if drawdown_from_peak <= -BRAIN5["TRAIL_GIVEBACK_PCT"]:
                return {
                    "exit": True, "reason": "trailing_stop",
                    "exit_price": current_premium,
                    "message": (
                        f"TRAILING STOP: peak {peak} se {drawdown_from_peak:.1f}% "
                        f"give-back (<= -{BRAIN5['TRAIL_GIVEBACK_PCT']}%)"
                    ),
                }

    return {
        "exit": False, "reason": "hold",
        "exit_price": None,
        "message": (
            f"hold — premium change {_pct_change(current_premium, entry):.1f}%, "
            f"peak {peak}, gamma: {gamma_check['message']}"
        ),
    }


def execute_exit(position: dict, exit_signal: dict, broker=None) -> dict:
    """
    Exit signal ko broker order mein convert karta hai.
    Broker integration abhi stage5_execution.py wale jaisa hi hai —
    real SmartAPI order flow wahan jayega; ye brain decision layer hai.
    """
    if not exit_signal.get("exit"):
        return {"executed": False, "note": "exit signal nahi tha — kuch nahi kiya"}

    logger.info(
        f"EXIT {position.get('option_symbol')}: {exit_signal['reason']} — "
        f"{exit_signal['message']}"
    )

    # Broker abhi stub hai (repo ki existing honest position): real exit
    # order placement SmartAPI integrate hote hi stage5 se hoga.
    return {
        "executed": True,
        "reason": exit_signal["reason"],
        "exit_price": exit_signal.get("exit_price"),
        "option_symbol": position.get("option_symbol"),
        "quantity": position.get("quantity"),
        "note": "exit decision final — broker order flow stage5_execution se hoga",
    }


# ============================================================
# QUICK MANUAL TEST — repo ROOT se: python3 -m risk.exit_brain
# ============================================================
if __name__ == "__main__":
    from datetime import timedelta

    pos = {
        "symbol": "NIFTY", "option_symbol": "NIFTY25SEP25000CE",
        "direction": "BUY", "entry_premium": 100.0, "quantity": 75,
        "entry_time": datetime.now(), "peak_premium": 100.0,
        "underlying_stop": 24800.0, "delta": 0.55, "gamma_pct": 1.0,
        "days_to_expiry": 5,
    }

    midday = datetime.now().replace(hour=13, minute=0)
    near_close = datetime.now().replace(hour=15, minute=20)

    print("=== Test 1: normal hold ===")
    print(evaluate_exit(pos, 105.0, current_time=midday, underlying_price=25000)["message"])

    print("\n=== Test 2: stop-loss ===")
    print(evaluate_exit(pos, 70.0, current_time=midday, underlying_price=25000)["message"])

    print("\n=== Test 3: target ===")
    print(evaluate_exit(pos, 160.0, current_time=midday, underlying_price=25000)["message"])

    print("\n=== Test 4: time exit ===")
    print(evaluate_exit(pos, 105.0, current_time=near_close, underlying_price=25000)["message"])

    print("\n=== Test 5: gamma exit ===")
    pos_gamma = dict(pos, days_to_expiry=1, gamma_pct=3.0)
    print(evaluate_exit(pos_gamma, 105.0, current_time=midday, underlying_price=25000)["message"])

    print("\n=== Test 6: structural stop ===")
    print(evaluate_exit(pos, 105.0, current_time=midday, underlying_price=24750)["message"])

    print("\n=== Test 7: trailing stop ===")
    pos["peak_premium"] = 160.0  # peaked earlier
    print(evaluate_exit(pos, 132.0, current_time=midday, underlying_price=25000)["message"])
