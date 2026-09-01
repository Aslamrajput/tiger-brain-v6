"""
Tiger Brain V6+V7 — Volatility-Arbitrage Sub-Brain (Section 3.4 / 23)
IV Percentile + event proximity dekh kar BUY-favorable/NO_TRADE/HARD_VETO
decide karta hai. Ye sub-brain sabse zyada NO_TRADE vote deta hai — iska
kaam hi hai khatarnak IV situations mein sabko rokna (Section 5.3 veto power).

⚠️ IV Percentile calculate karne ke liye 60-din ka IV history chahiye
(config.thresholds.IV['PERCENTILE_LOOKBACK_DAYS']). Agar itna data
available nahi hai, function warning ke saath aage badhega par confidence
kam rahega.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

try:
    from config.thresholds import SUBBRAIN_VOL_ARB, IV
except ImportError:
    raise ImportError("Repo ROOT se chalao, 'subbrains/' ke andar se nahi.")


def calculate_iv_percentile(iv_series: pd.Series, lookback: int = None) -> float:
    """Current IV, pichle N din ke IV range mein kis percentile pe hai."""
    lookback = lookback or IV["PERCENTILE_LOOKBACK_DAYS"]
    recent = iv_series.tail(lookback)
    current_iv = iv_series.iloc[-1]
    percentile = (recent < current_iv).sum() / len(recent) * 100
    return percentile


def evaluate(
    iv_series: pd.Series,
    hours_to_next_event: float | None = None,
    put_iv: float | None = None,
    call_iv: float | None = None,
) -> dict:
    """
    Args:
        iv_series: historical IV values (ATM IV ya representative IV),
                   kam se kam 20-30 din ka data
        hours_to_next_event: agla major event (earnings/RBI/budget) kitne
                              ghante door hai. None = koi known event nahi.
        put_iv, call_iv: skew check ke liye (optional)

    Returns:
        dict (Section 4 format) + 'hard_veto': bool — agar True hai to
        Meta-Brain ko YE override karna hai, chahe baaki sub-brains kuch
        bhi bolein (Section 5.3).
    """
    reasoning_tags = []
    conflicting_evidence = []
    hard_veto = False

    if len(iv_series) < 20:
        return {
            "vote": "NO_TRADE", "confidence": 0, "reasoning_tags": [],
            "conflicting_evidence": ["IV history 20 din se kam hai — reliable percentile nahi bana sakte"],
            "regime_fit": 50, "hard_veto": False,
        }

    iv_percentile = calculate_iv_percentile(iv_series)
    cfg = SUBBRAIN_VOL_ARB

    # --- HARD VETO CHECK (sabse pehle, sabse zaroori) ---
    if (
        hours_to_next_event is not None
        and hours_to_next_event <= cfg["HARD_VETO_EVENT_HOURS"]
        and iv_percentile > cfg["HARD_VETO_IV_PERCENTILE_MIN"]
    ):
        hard_veto = True
        reasoning_tags.append(
            f"🚫 HARD VETO: Event {hours_to_next_event:.1f}hr mein hai + "
            f"IV percentile {iv_percentile:.1f} pehle se high — IV crush ka khatra"
        )
        return {
            "vote": "NO_TRADE", "confidence": 100, "reasoning_tags": reasoning_tags,
            "conflicting_evidence": [], "regime_fit": 100, "hard_veto": True,
        }

    # --- Skew anomaly check (caution flag, veto nahi) ---
    if put_iv is not None and call_iv is not None and call_iv > 0:
        skew_pct = ((put_iv - call_iv) / call_iv) * 100
        if skew_pct >= cfg["SKEW_CAUTION_PCT"]:
            conflicting_evidence.append(
                f"⚠️ IV Skew anomaly: Put IV {skew_pct:.1f}% zyada Call IV se — zyada dar ka signal"
            )

    # --- Normal evaluation ---
    if iv_percentile <= cfg["BUY_FAVORABLE_IV_PERCENTILE_MAX"] and (
        hours_to_next_event is not None and hours_to_next_event <= cfg["BUY_FAVORABLE_EVENT_HOURS"]
    ):
        vote = "FAVORABLE"
        confidence = 75.0
        reasoning_tags.append(
            f"IV percentile {iv_percentile:.1f} — kam hai, aur event {hours_to_next_event:.1f}hr "
            "mein hai (sasta premium, favorable buying)"
        )
    elif iv_percentile >= cfg["NO_TRADE_IV_PERCENTILE_MIN"]:
        vote = "NO_TRADE"
        confidence = 80.0
        reasoning_tags.append(
            f"IV percentile {iv_percentile:.1f} — bahut high hai, IV crush ka risk (chahe "
            "direction sahi lage bhi)"
        )
    else:
        vote = "NO_TRADE"
        confidence = 30.0
        conflicting_evidence.append(
            f"IV percentile {iv_percentile:.1f} — na bahut favorable hai na bahut dangerous, "
            "clear edge nahi hai"
        )

    return {
        "vote": vote, "confidence": confidence,
        "reasoning_tags": reasoning_tags,
        "conflicting_evidence": conflicting_evidence,
        "regime_fit": 100 if iv_percentile >= cfg["NO_TRADE_IV_PERCENTILE_MIN"] else 60,
        "hard_veto": hard_veto,
    }


if __name__ == "__main__":
    np.random.seed(5)
    n = 40
    iv_test = pd.Series(np.random.uniform(15, 25, n))
    iv_test.iloc[-1] = 35  # aaj IV spike ho gaya (high percentile banega)

    print("=== Vol-Arb Sub-Brain Test 1: High IV + Event Near (should HARD VETO) ===")
    result1 = evaluate(iv_test, hours_to_next_event=12)
    for k, v in result1.items():
        print(f"{k}: {v}")

    print("\n=== Vol-Arb Sub-Brain Test 2: Low IV + Event Coming (should BUY favorable) ===")
    iv_test2 = pd.Series(np.random.uniform(20, 30, n))
    iv_test2.iloc[-1] = 15  # aaj IV low hai
    result2 = evaluate(iv_test2, hours_to_next_event=36)
    for k, v in result2.items():
        print(f"{k}: {v}")

    print("\n✅ Test complete.")
    print("⚠️ Ye SYNTHETIC data hai, real market data se numbers alag honge.")
  
