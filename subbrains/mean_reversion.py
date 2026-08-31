"""
Tiger Brain V6+V7 — Mean-Reversion Sub-Brain
===============================================
Blueprint Section 3.2 aur 23 ka implementation. Ye sub-brain "Range" ya
"Low Volatility" regime mein active hota hai — RSI extremes aur VWAP se
extreme distance dekh kar reversal (bounce) predict karta hai.

⚠️ IMPORTANT — Supply/Demand zones abhi nahi hain:
Blueprint mein "fresh Supply-Demand zone" ek factor hai (30% weight), par
zone-detection ek complex separate module hai (price-action based zone
identification) jo abhi nahi bana hai. Isliye:
  - `near_fresh_zone` parameter caller ko manually pass karna hai (True/
    False/None) jab tak zone-detection module nahi banta
  - Jaisa trend_follow.py mein OI ke saath kiya, yahan bhi missing factor
    ka weight baaki factors mein proportionally redistribute hota hai,
    aur conflicting_evidence mein clearly warning aati hai
"""

import numpy as np
import pandas as pd

try:
    from config.thresholds import SUBBRAIN_MEAN_REVERSION
    from subbrains.trend_follow import (
        calculate_vwap,
        has_volume_data,
        redistribute_weight,
    )
except ImportError:
    raise ImportError(
        "Imports fail hue. Is script ko repo ke ROOT folder se chalao, "
        "'subbrains/' folder ke andar se seedha mat chalao."
    )


# ============================================================
# SUPPORTING INDICATOR — RSI
# ============================================================

def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Relative Strength Index — 0-100 scale, 30 se neeche oversold,
    70 se upar overbought (Section 3.2/23 ka main tool).
    """
    close = df["close"]
    delta = close.diff()

    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)

    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    # Jab avg_loss 0 ho (pure uptrend), RSI 100 hona chahiye
    rsi = rsi.where(avg_loss != 0, 100)
    return rsi


# ============================================================
# MEAN-REVERSION SUB-BRAIN — MAIN EVALUATION FUNCTION
# ============================================================

def evaluate(
    df: pd.DataFrame,
    current_regime: str,
    near_fresh_zone: bool | None = None,
) -> dict:
    """
    Mean-Reversion Sub-Brain ka vote deta hai — Section 4 ka Voting
    Table format follow karta hai.

    Args:
        df: OHLCV DataFrame (kam se kam 30+ rows)
        current_regime: regime/classifier.py se aaya regime string
        near_fresh_zone: True/False agar Supply-Demand zone data available
                          hai, None agar abhi nahi hai (factor skip hoga)

    Returns:
        dict (Section 4 Voting Table format):
            'vote', 'confidence', 'reasoning_tags', 'conflicting_evidence',
            'regime_fit'
    """
    cfg = SUBBRAIN_MEAN_REVERSION
    reasoning_tags = []
    conflicting_evidence = []

    # --- Regime Fit Check ---
    # Ye sub-brain sirf Range/Low-Vol regime mein hi fit hai
    regime_fit_map = {
        "STRONG_TREND": 10,
        "WEAK_TREND": 30,
        "RANGE": 100,
        "COMPRESSION": 40,
        "HIGH_VOL": 25,
        "LOW_VOL": 80,
        "NORMAL": 40,
    }
    regime_fit = regime_fit_map.get(current_regime, 30)

    if len(df) < 30:
        return {
            "vote": "NO_TRADE",
            "confidence": 0,
            "reasoning_tags": [],
            "conflicting_evidence": ["Insufficient data — kam se kam 30 rows chahiye"],
            "regime_fit": regime_fit,
            "data_available": False,
        }

    # --- Indicator Calculations ---
    rsi_series = calculate_rsi(df)
    vwap_series = calculate_vwap(df)

    latest_close = df["close"].iloc[-1]
    latest_rsi = rsi_series.iloc[-1]
    latest_vwap = vwap_series.iloc[-1]

    vwap_distance_pct = ((latest_close - latest_vwap) / latest_vwap) * 100

    # --- Scoring har factor ka ---
    scores = {}

    # 1. RSI extreme
    if latest_rsi <= cfg["RSI_OVERSOLD"]:
        scores["rsi_extreme"] = 1.0  # oversold -> bounce upar expect (BUY bias)
        reasoning_tags.append(f"RSI {latest_rsi:.1f} — oversold zone (bounce expected)")
    elif latest_rsi >= cfg["RSI_OVERBOUGHT"]:
        scores["rsi_extreme"] = -1.0  # overbought -> drop expect (SELL bias)
        reasoning_tags.append(f"RSI {latest_rsi:.1f} — overbought zone (pullback expected)")
    else:
        scores["rsi_extreme"] = 0.0
        conflicting_evidence.append(f"RSI {latest_rsi:.1f} — neutral zone, extreme nahi hai")

    # 2. VWAP extreme distance (reversal trigger)
    if abs(vwap_distance_pct) >= cfg["MIN_VWAP_EXTREME_DISTANCE_PCT"]:
        # Bahut door VWAP se upar hai -> wapas neeche aane ki probability (SELL bias)
        # Bahut door VWAP se neeche hai -> wapas upar aane ki probability (BUY bias)
        vwap_direction = -1.0 if vwap_distance_pct > 0 else 1.0
        scores["vwap_distance"] = vwap_direction
        reasoning_tags.append(
            f"Price VWAP se {abs(vwap_distance_pct):.2f}% door — extreme distance, "
            f"reversal probability badhi"
        )
    else:
        scores["vwap_distance"] = 0.0
        conflicting_evidence.append(
            f"VWAP distance sirf {abs(vwap_distance_pct):.2f}% — extreme nahi hai"
        )

    # 3. Zone freshness (agar data available hai)
    weights = dict(cfg["CONFIDENCE_WEIGHTS"])
    if near_fresh_zone is None:
        redistribute_weight(weights, "zone_freshness")
        conflicting_evidence.append(
            "⚠️ Supply-Demand zone data available nahi tha — is factor ko "
            "skip karke baaki weights proportionally badhaye gaye hain. "
            "Zone-detection module abhi nahi bana hai (known gap)."
        )
    else:
        # Zone direction RSI/VWAP jo bhi bias bata rahe hain, unko confirm/reject karta hai
        primary_direction = scores["rsi_extreme"] if scores["rsi_extreme"] != 0 else scores["vwap_distance"]
        scores["zone_freshness"] = primary_direction if near_fresh_zone else 0.0
        if near_fresh_zone:
            reasoning_tags.append("Fresh Supply/Demand zone ke paas — reversal ka support")
        else:
            conflicting_evidence.append("Koi fresh zone nearby nahi hai")

    # 4. Volume (simple check — bahut low volume pe reversal trust nahi karna)
    volume_available = has_volume_data(df)
    volume_avg_20 = df["volume"].tail(20).mean() if volume_available else 0.0
    latest_volume = df["volume"].iloc[-1] if volume_available else 0.0
    volume_ratio = latest_volume / volume_avg_20 if volume_avg_20 > 0 else 0

    if not volume_available:
        redistribute_weight(weights, "volume")
        conflicting_evidence.append(
            "⚠️ Volume data available nahi tha (index spot candles) — "
            "factor skip, weight redistribute."
        )
    elif volume_ratio >= 0.5:  # bahut kam nahi hai
        primary_direction = scores["rsi_extreme"] if scores["rsi_extreme"] != 0 else scores["vwap_distance"]
        scores["volume"] = primary_direction * min(volume_ratio, 1.5) / 1.5
        reasoning_tags.append(f"Volume {volume_ratio:.1f}x avg — reasonable support")
    else:
        scores["volume"] = 0.0
        conflicting_evidence.append(f"Volume sirf {volume_ratio:.1f}x avg — bahut kam, weak signal")

    # --- Weighted Confidence Score Calculate Karna ---
    weighted_sum = sum(scores.get(k, 0.0) * weights.get(k, 0.0) for k in weights)
    confidence = round(abs(weighted_sum) * 100, 1)

    # --- Final Vote Decide Karna ---
    if confidence < 40:
        vote = "NO_TRADE"
    elif weighted_sum > 0:
        vote = "BUY"
    else:
        vote = "SELL"

    return {
        "vote": vote,
        "confidence": confidence,
        "reasoning_tags": reasoning_tags,
        "conflicting_evidence": conflicting_evidence,
        "regime_fit": regime_fit,
        "data_available": True,
    }


# ============================================================
# QUICK MANUAL TEST — synthetic data se
# Chalane ka tarika: repo ROOT se → python3 -m subbrains.mean_reversion
# ============================================================
if __name__ == "__main__":
    np.random.seed(11)
    n = 60
    dates = pd.date_range("2025-01-01", periods=n, freq="D")

    # Synthetic RANGE-BOUND data with a dip at the end (oversold bounce setup)
    base_price = 100
    range_noise = np.sin(np.linspace(0, 6 * np.pi, n)) * 2 + np.random.normal(0, 0.3, n)
    close_prices = base_price + range_noise
    close_prices[-3:] -= 4  # end mein sudden dip — oversold banane ke liye

    df_test = pd.DataFrame(index=dates)
    df_test["close"] = close_prices
    df_test["open"] = df_test["close"].shift(1).fillna(base_price)
    df_test["high"] = df_test[["open", "close"]].max(axis=1) + np.random.uniform(0.1, 0.4, n)
    df_test["low"] = df_test[["open", "close"]].min(axis=1) - np.random.uniform(0.1, 0.4, n)
    df_test["volume"] = np.random.randint(150000, 300000, n)

    print("=== Mean-Reversion Sub-Brain Test (Synthetic Range+Dip Data) ===\n")

    print("--- Without zone data ---")
    result = evaluate(df_test, current_regime="RANGE", near_fresh_zone=None)
    for key, value in result.items():
        print(f"{key}: {value}")

    print("\n--- With fresh zone confirmed ---")
    result2 = evaluate(df_test, current_regime="RANGE", near_fresh_zone=True)
    for key, value in result2.items():
        print(f"{key}: {value}")

    print("\n✅ Test complete — koi crash nahi hua.")
    print("⚠️ Ye SYNTHETIC data hai, real market data se numbers alag honge.")
  
