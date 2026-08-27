"""
Tiger Brain V6+V7 — Range-Scalp Sub-Brain (Section 3.5 / 23)
Range regime mein, liquidity achi ho tabhi active. Range ke top/bottom ke
paas quick entry-exit, bada move nahi pakadna.
"""

import numpy as np
import pandas as pd

try:
    from config.thresholds import SUBBRAIN_RANGE_SCALP
except ImportError:
    raise ImportError("Repo ROOT se chalao, 'subbrains/' ke andar se nahi.")


def evaluate(
    df: pd.DataFrame,
    current_regime: str,
    bid_ask_spread_normal: bool = True,
) -> dict:
    """
    Args:
        df: OHLCV DataFrame
        current_regime: regime/classifier.py se aaya string
        bid_ask_spread_normal: True agar liquidity stress nahi hai
                                (Section 17 ka Liquidity Stress check —
                                abhi broker se live spread data nahi hai,
                                isliye ye manual flag hai)

    Returns:
        dict (Section 4 format)
    """
    cfg = SUBBRAIN_RANGE_SCALP
    reasoning_tags = []
    conflicting_evidence = []

    regime_fit_map = {
        "STRONG_TREND": 5, "WEAK_TREND": 15, "RANGE": 100,
        "COMPRESSION": 10, "HIGH_VOL": 10, "LOW_VOL": 40, "NORMAL": 20,
    }
    regime_fit = regime_fit_map.get(current_regime, 20)

    if current_regime != "RANGE":
        return {
            "vote": "NO_TRADE", "confidence": 0,
            "reasoning_tags": [],
            "conflicting_evidence": [f"Regime '{current_regime}' hai, Range nahi — scalp setup invalid"],
            "regime_fit": regime_fit,
        }

    if not bid_ask_spread_normal:
        return {
            "vote": "NO_TRADE", "confidence": 0,
            "reasoning_tags": [],
            "conflicting_evidence": ["⚠️ Liquidity stress — bid-ask spread chauda hai, scalping risky"],
            "regime_fit": regime_fit,
        }

    if len(df) < cfg["MIN_RANGE_STABLE_MINUTES"]:
        return {
            "vote": "NO_TRADE", "confidence": 0,
            "reasoning_tags": [],
            "conflicting_evidence": [
                f"Range abhi {len(df)} candles se stable hai, kam se kam "
                f"{cfg['MIN_RANGE_STABLE_MINUTES']} chahiye"
            ],
            "regime_fit": regime_fit,
        }

    recent = df.tail(cfg["MIN_RANGE_STABLE_MINUTES"])
    range_high = recent["high"].max()
    range_low = recent["low"].min()
    range_width = range_high - range_low

    latest_close = df["close"].iloc[-1]
    distance_from_top_pct = ((range_high - latest_close) / range_high) * 100
    distance_from_bottom_pct = ((latest_close - range_low) / range_low) * 100

    scores = {}

    if distance_from_bottom_pct <= cfg["BOUNDARY_PROXIMITY_PCT"]:
        scores["boundary"] = 1.0  # bottom ke paas -> BUY (bounce expect)
        reasoning_tags.append(
            f"Price range ke bottom ke {distance_from_bottom_pct:.2f}% andar — quick bounce entry"
        )
    elif distance_from_top_pct <= cfg["BOUNDARY_PROXIMITY_PCT"]:
        scores["boundary"] = -1.0  # top ke paas -> SELL (rejection expect)
        reasoning_tags.append(
            f"Price range ke top ke {distance_from_top_pct:.2f}% andar — quick rejection entry"
        )
    else:
        scores["boundary"] = 0.0
        conflicting_evidence.append("Price range ke middle mein hai — boundary ke paas nahi, entry setup nahi")

    weighted_sum = scores["boundary"]
    confidence = round(abs(weighted_sum) * 70, 1)  # scalp trades mein max confidence conservative rakha

    if confidence < 40 or weighted_sum == 0:
        vote = "NO_TRADE"
    elif weighted_sum > 0:
        vote = "BUY"
    else:
        vote = "SELL"

    if vote != "NO_TRADE":
        target_pct = range_width / latest_close * cfg["TARGET_RANGE_WIDTH_PCT_MIN"] / 100
        reasoning_tags.append(
            f"Target: range width ka {cfg['TARGET_RANGE_WIDTH_PCT_MIN']}-"
            f"{cfg['TARGET_RANGE_WIDTH_PCT_MAX']}% | SL: {cfg['STOPLOSS_PCT_MIN']}-"
            f"{cfg['STOPLOSS_PCT_MAX']}% (bahut tight — theta decay ka khayal rakhna)"
        )

    return {
        "vote": vote, "confidence": confidence,
        "reasoning_tags": reasoning_tags,
        "conflicting_evidence": conflicting_evidence,
        "regime_fit": regime_fit,
    }


if __name__ == "__main__":
    np.random.seed(9)
    n = 60
    dates = pd.date_range("2025-01-01", periods=n, freq="D")

    base_price = 100
    close_prices = base_price + np.sin(np.linspace(0, 4 * np.pi, n)) * 1.5 + np.random.normal(0, 0.15, n)
    close_prices[-1] = base_price - 1.45  # range ke bottom ke paas end karna

    df_test = pd.DataFrame(index=dates)
    df_test["close"] = close_prices
    df_test["open"] = df_test["close"].shift(1).fillna(base_price)
    df_test["high"] = df_test[["open", "close"]].max(axis=1) + np.random.uniform(0.05, 0.15, n)
    df_test["low"] = df_test[["open", "close"]].min(axis=1) - np.random.uniform(0.05, 0.15, n)
    df_test["volume"] = np.random.randint(100000, 200000, n)

    print("=== Range-Scalp Sub-Brain Test (Synthetic Range Data) ===\n")
    result = evaluate(df_test, current_regime="RANGE", bid_ask_spread_normal=True)
    for key, value in result.items():
        print(f"{key}: {value}")

    print("\n✅ Test complete.")
    print("⚠️ Ye SYNTHETIC data hai, real market data se numbers alag honge.")
  
