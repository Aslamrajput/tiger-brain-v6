"""
Tiger Brain V6+V7 — Breakout Sub-Brain (Section 3.3 / 23)
Compression regime ke baad range-boundary cross + volume/OI confirm hone
par active hota hai.

⚠️ OI data abhi connect nahi hai — same pattern jaisa trend_follow.py aur
mean_reversion.py mein — oi_new_buildup_confirmed=None pe factor skip
hoke weight redistribute hota hai.
"""

import numpy as np
import pandas as pd

try:
    from config.thresholds import SUBBRAIN_BREAKOUT, REGIME
    from subbrains.trend_follow import has_volume_data, redistribute_weight
except ImportError:
    raise ImportError("Repo ROOT se chalao, 'subbrains/' ke andar se nahi.")


def calculate_bollinger_bands(df: pd.DataFrame, period: int = 20, std_dev: int = 2):
    close = df["close"]
    sma = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()
    upper = sma + std_dev * std
    lower = sma - std_dev * std
    width = (upper - lower) / sma
    return upper, lower, width


def evaluate(
    df: pd.DataFrame,
    current_regime: str,
    oi_new_buildup_confirmed: bool | None = None,
) -> dict:
    cfg = SUBBRAIN_BREAKOUT
    reasoning_tags = []
    conflicting_evidence = []

    regime_fit_map = {
        "STRONG_TREND": 20, "WEAK_TREND": 25, "RANGE": 15,
        "COMPRESSION": 100, "HIGH_VOL": 40, "LOW_VOL": 20, "NORMAL": 30,
    }
    regime_fit = regime_fit_map.get(current_regime, 30)

    if len(df) < 30:
        return {
            "vote": "NO_TRADE", "confidence": 0, "reasoning_tags": [],
            "conflicting_evidence": ["Insufficient data — kam se kam 30 rows chahiye"],
            "regime_fit": regime_fit,
            "data_available": False,
        }

    upper_band, lower_band, bb_width = calculate_bollinger_bands(df)

    latest_close = df["close"].iloc[-1]
    latest_upper = upper_band.iloc[-1]
    latest_lower = lower_band.iloc[-1]
    latest_width = bb_width.iloc[-1]
    avg_width_20 = bb_width.tail(20).mean()

    volume_available = has_volume_data(df)
    volume_avg_20 = df["volume"].tail(20).mean() if volume_available else 0.0
    latest_volume = df["volume"].iloc[-1] if volume_available else 0.0
    volume_multiplier = latest_volume / volume_avg_20 if volume_avg_20 > 0 else 0

    scores = {}

    # 1. Compression-to-expansion check
    if avg_width_20 > 0:
        width_change_pct = ((latest_width - avg_width_20) / avg_width_20) * 100
    else:
        width_change_pct = 0

    was_compressed = current_regime == "COMPRESSION"
    if was_compressed and width_change_pct > 0:
        direction = 1.0 if latest_close > upper_band.iloc[-2] else (
            -1.0 if latest_close < lower_band.iloc[-2] else 0.0
        )
        scores["compression_to_expansion"] = direction
        if direction != 0:
            reasoning_tags.append(f"Compression ke baad expansion — width {width_change_pct:.1f}% badha")
        else:
            conflicting_evidence.append("Expansion dikha par range boundary cross nahi hua")
    else:
        scores["compression_to_expansion"] = 0.0
        conflicting_evidence.append("Compression regime nahi tha — breakout setup weak")

    # 2. Range boundary cross (volume spike ke saath)
    if latest_close > latest_upper:
        boundary_direction = 1.0
        reasoning_tags.append("Price upper Bollinger Band se upar cross hua")
    elif latest_close < latest_lower:
        boundary_direction = -1.0
        reasoning_tags.append("Price lower Bollinger Band se neeche cross hua")
    else:
        boundary_direction = 0.0
        conflicting_evidence.append("Price abhi bhi band ke andar hai — koi clear cross nahi")
    scores["range_boundary_clarity"] = boundary_direction

    # 3. Volume spike confirmation
    weights = dict(cfg["CONFIDENCE_WEIGHTS"])
    if not volume_available:
        redistribute_weight(weights, "volume_spike")
        conflicting_evidence.append(
            "⚠️ Volume data available nahi tha (index spot candles) — "
            "breakout confirmation factor skip, weight redistribute."
        )
    elif volume_multiplier >= cfg["MIN_VOLUME_MULTIPLIER"]:
        scores["volume_spike"] = boundary_direction if boundary_direction != 0 else 1.0
        reasoning_tags.append(f"Volume {volume_multiplier:.1f}x avg — breakout confirm")
    else:
        scores["volume_spike"] = 0.0
        conflicting_evidence.append(
            f"Volume sirf {volume_multiplier:.1f}x avg — {cfg['MIN_VOLUME_MULTIPLIER']}x se kam, "
            "false breakout (fakeout) ka risk"
        )

    # 4. OI confirm (agar data available)
    if oi_new_buildup_confirmed is None:
        redistribute_weight(weights, "oi_confirm")
        conflicting_evidence.append(
            "⚠️ OI data available nahi tha — is factor ko skip karke baaki "
            "weights proportionally badhaye gaye hain (known gap, jaisa "
            "trend_follow.py mein bhi hai)."
        )
    else:
        direction = boundary_direction if boundary_direction != 0 else 1.0
        scores["oi_confirm"] = direction if oi_new_buildup_confirmed else 0.0
        if oi_new_buildup_confirmed:
            reasoning_tags.append("OI new buildup usi direction mein — genuine breakout ka sign")
        else:
            conflicting_evidence.append("OI buildup confirm nahi hua — fakeout ho sakta hai")

    weighted_sum = sum(scores.get(k, 0.0) * weights.get(k, 0.0) for k in weights)
    confidence = round(abs(weighted_sum) * 100, 1)

    if confidence < 40:
        vote = "NO_TRADE"
    elif weighted_sum > 0:
        vote = "BUY"
    else:
        vote = "SELL"

    return {
        "vote": vote, "confidence": confidence,
        "reasoning_tags": reasoning_tags,
        "conflicting_evidence": conflicting_evidence,
        "regime_fit": regime_fit,
        "data_available": True,
    }


if __name__ == "__main__":
    np.random.seed(3)
    n = 60
    dates = pd.date_range("2025-01-01", periods=n, freq="D")

    # Pehle 50 din tight range (compression), phir sudden breakout upar
    base_price = 100
    close_prices = base_price + np.random.normal(0, 0.3, n)
    close_prices[-5:] += np.linspace(0, 8, 5)  # sudden breakout end mein

    df_test = pd.DataFrame(index=dates)
    df_test["close"] = close_prices
    df_test["open"] = df_test["close"].shift(1).fillna(base_price)
    df_test["high"] = df_test[["open", "close"]].max(axis=1) + np.random.uniform(0.1, 0.3, n)
    df_test["low"] = df_test[["open", "close"]].min(axis=1) - np.random.uniform(0.1, 0.3, n)
    df_test["volume"] = np.random.randint(100000, 200000, n)
    df_test.loc[df_test.index[-1], "volume"] = 500000  # breakout volume spike

    print("=== Breakout Sub-Brain Test (Synthetic Compression+Breakout Data) ===\n")
    result = evaluate(df_test, current_regime="COMPRESSION", oi_new_buildup_confirmed=None)
    for key, value in result.items():
        print(f"{key}: {value}")

    print("\n✅ Test complete.")
    print("⚠️ Ye SYNTHETIC data hai, real market data se numbers alag honge.")
  
