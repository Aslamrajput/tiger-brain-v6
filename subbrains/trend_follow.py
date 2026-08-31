"""
Tiger Brain V6+V7 — Trend-Following Sub-Brain
================================================
Blueprint Section 3.1 aur 23 ka implementation. Ye sub-brain "Strong Trend"
regime mein active hota hai — Supertrend, VWAP alignment, volume aur OI
buildup dekh kar BUY/SELL/NO_TRADE vote deta hai, saath mein confidence
score (Section 4 ka Voting Table format).

⚠️ IMPORTANT — abhi OI data available nahi hai:
Blueprint mein OI buildup confirmation zaroori hai (25% weight confidence
mein), lekin humne abhi tak broker se live OI data connect nahi kiya hai
(wo Phase 2/3 mein aayega jab live options chain milegi). Isliye:
  - `oi_buildup_confirmed` ek parameter hai jo abhi CALLER ko manually
    pass karna hai (ya None, jisse ye factor skip ho jayega aur weight
    baaki factors mein proportionally redistribute ho jayega)
  - Jab tak real OI feed nahi lagta, is sub-brain ka confidence score
    THODA INCOMPLETE hai — Section 8 ka "koi factor skip nahi hona
    chahiye" rule abhi 100% follow nahi ho raha. Ye ek known gap hai,
    chhupaya nahi ja raha.
"""

import numpy as np
import pandas as pd

try:
    from config.thresholds import SUBBRAIN_TREND_FOLLOW, REGIME, VWAP
    from regime.classifier import calculate_atr
except ImportError:
    raise ImportError(
        "Imports fail hue. Is script ko repo ke ROOT folder se chalao, "
        "'subbrains/' folder ke andar se seedha mat chalao."
    )


# ============================================================
# SUPPORTING INDICATORS (jo regime/classifier.py mein nahi hain)
# ============================================================

def calculate_supertrend(
    df: pd.DataFrame, period: int = 10, multiplier: float = 3.0
) -> pd.DataFrame:
    """
    Supertrend indicator — trend direction batata hai (green=bullish,
    red=bearish). Section 3.1 ka main tool.

    Returns:
        DataFrame with columns: 'supertrend' (value), 'trend' (1=bullish,
        -1=bearish)
    """
    atr = calculate_atr(df, period)
    hl_avg = (df["high"] + df["low"]) / 2

    upper_band = hl_avg + (multiplier * atr)
    lower_band = hl_avg - (multiplier * atr)

    supertrend = pd.Series(index=df.index, dtype=float)
    trend = pd.Series(index=df.index, dtype=int)

    # Warmup period ke pehle NaN rahega (ATR calculate hone tak)
    first_valid = atr.first_valid_index()
    if first_valid is None:
        supertrend[:] = np.nan
        trend[:] = 0
        return pd.DataFrame({"supertrend": supertrend, "trend": trend})

    start_idx = df.index.get_loc(first_valid)
    trend.iloc[start_idx] = 1
    supertrend.iloc[start_idx] = lower_band.iloc[start_idx]

    for i in range(start_idx + 1, len(df)):
        close_prev = df["close"].iloc[i - 1]
        close_curr = df["close"].iloc[i]

        if trend.iloc[i - 1] == 1:
            curr_lower = max(lower_band.iloc[i], supertrend.iloc[i - 1])
            if close_curr < curr_lower:
                trend.iloc[i] = -1
                supertrend.iloc[i] = upper_band.iloc[i]
            else:
                trend.iloc[i] = 1
                supertrend.iloc[i] = curr_lower
        else:
            curr_upper = min(upper_band.iloc[i], supertrend.iloc[i - 1])
            if close_curr > curr_upper:
                trend.iloc[i] = 1
                supertrend.iloc[i] = lower_band.iloc[i]
            else:
                trend.iloc[i] = -1
                supertrend.iloc[i] = curr_upper

    return pd.DataFrame({"supertrend": supertrend, "trend": trend})


def has_volume_data(df: pd.DataFrame) -> bool:
    """
    Index spot candles (jaise Angel ka NIFTY token 99926000) volume = 0
    dete hain. Aise data pe volume-based factor ko "fail" maan lena galat
    hai — wo factor available hi nahi hai.
    """
    if "volume" not in df.columns:
        return False
    recent = df["volume"].tail(VWAP["ROLLING_CANDLES"])
    return bool(recent.notna().any() and (recent.fillna(0) > 0).any())


def calculate_vwap(df: pd.DataFrame, window: int | None = None) -> pd.Series:
    """
    Rolling Volume Weighted Average Price (default window config se).
    Cumulative VWAP 2 saal ke daily data pe bekaar ho jaata hai — har din
    ka distance mahine purane average se naapa jaata hai.

    Volume data na ho (index spot candles) to typical price ka simple
    rolling average lautata hai — yani unweighted VWAP.
    """
    window = window or VWAP["ROLLING_CANDLES"]
    typical_price = (df["high"] + df["low"] + df["close"]) / 3

    if not has_volume_data(df):
        return typical_price.rolling(window, min_periods=1).mean()

    volume = df["volume"].fillna(0)
    rolling_tpv = (typical_price * volume).rolling(window, min_periods=1).sum()
    rolling_volume = volume.rolling(window, min_periods=1).sum()
    fallback = typical_price.rolling(window, min_periods=1).mean()
    return (rolling_tpv / rolling_volume).where(rolling_volume > 0, fallback)


def redistribute_weight(weights: dict, missing_key: str) -> None:
    """Jo factor available nahi hai uska weight baaki factors mein baant do."""
    if missing_key not in weights:
        return
    freed = weights.pop(missing_key)
    remaining_total = sum(weights.values())
    if remaining_total <= 0:
        return
    for key in weights:
        weights[key] += freed * (weights[key] / remaining_total)


# ============================================================
# TREND-FOLLOWING SUB-BRAIN — MAIN EVALUATION FUNCTION
# ============================================================

def evaluate(
    df: pd.DataFrame,
    current_regime: str,
    oi_buildup_confirmed: bool | None = None,
    is_index: bool = True,
) -> dict:
    """
    Trend-Following Sub-Brain ka vote deta hai — Section 4 ka Voting
    Table format follow karta hai.

    Args:
        df: OHLCV DataFrame (kam se kam 30+ rows, warmup ke liye)
        current_regime: regime/classifier.py se aaya regime string
                        (jaise 'STRONG_TREND')
        oi_buildup_confirmed: True/False agar OI data available hai,
                               None agar abhi nahi hai (factor skip hoga)
        is_index: True agar index option hai (VWAP distance threshold
                   index ka use hoga), False agar stock hai

    Returns:
        dict (Section 4 Voting Table format):
            'vote': 'BUY' | 'SELL' | 'NO_TRADE'
            'confidence': 0-100
            'reasoning_tags': list of str
            'conflicting_evidence': list of str
            'regime_fit': 0-100 (ye regime is sub-brain ke liye kitna
                          suitable hai, Meta-Brain ke liye extra context)
    """
    cfg = SUBBRAIN_TREND_FOLLOW
    reasoning_tags = []
    conflicting_evidence = []

    # --- Regime Fit Check (Section 4) ---
    # Ye sub-brain sirf Strong/Weak Trend regime mein hi fit hai
    regime_fit_map = {
        "STRONG_TREND": 100,
        "WEAK_TREND": 60,
        "RANGE": 15,
        "COMPRESSION": 20,
        "HIGH_VOL": 30,
        "LOW_VOL": 25,
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
    supertrend_df = calculate_supertrend(df)
    vwap_series = calculate_vwap(df)

    latest_close = df["close"].iloc[-1]
    latest_trend = supertrend_df["trend"].iloc[-1]
    latest_vwap = vwap_series.iloc[-1]

    vwap_distance_pct = ((latest_close - latest_vwap) / latest_vwap) * 100
    min_vwap_dist = (
        cfg["MIN_VWAP_DISTANCE_PCT"] if is_index else cfg["MIN_VWAP_DISTANCE_PCT"] * 1.67
    )  # stock threshold Section 21 ke hisaab se index se zyada hota hai

    # Volume check (index spot pe volume 0 hota hai — tab factor skip)
    volume_available = has_volume_data(df)
    volume_avg_20 = df["volume"].tail(20).mean() if volume_available else 0.0
    latest_volume = df["volume"].iloc[-1] if volume_available else 0.0
    volume_multiplier = latest_volume / volume_avg_20 if volume_avg_20 > 0 else 0

    # --- Scoring har factor ka (0.0 to 1.0 scale, phir weight se multiply) ---
    scores = {}

    # 1. Supertrend direction
    if latest_trend == 1:
        scores["supertrend"] = 1.0
        reasoning_tags.append("Supertrend bullish (green)")
    elif latest_trend == -1:
        scores["supertrend"] = -1.0
        reasoning_tags.append("Supertrend bearish (red)")
    else:
        scores["supertrend"] = 0.0
        conflicting_evidence.append("Supertrend direction unclear")

    # 2. VWAP alignment
    if abs(vwap_distance_pct) >= min_vwap_dist:
        vwap_direction = 1.0 if vwap_distance_pct > 0 else -1.0
        scores["vwap"] = vwap_direction
        reasoning_tags.append(
            f"Price VWAP se {vwap_distance_pct:.2f}% door ({'upar' if vwap_direction > 0 else 'neeche'})"
        )
    else:
        scores["vwap"] = 0.0
        conflicting_evidence.append(
            f"VWAP distance sirf {vwap_distance_pct:.2f}% — meaningful nahi (noise ho sakta hai)"
        )

    # 3. ADX (regime se hi pata chal jaata hai ki strong trend hai ya nahi)
    if current_regime == "STRONG_TREND":
        # Supertrend/VWAP jis disha mein hain, ADX unko confirm karta hai
        scores["adx"] = scores["supertrend"] if scores["supertrend"] != 0 else 0.0
    else:
        scores["adx"] = 0.0
        conflicting_evidence.append(f"Regime '{current_regime}' Strong Trend nahi hai")

    # 4. Volume confirmation
    weights = dict(cfg["CONFIDENCE_WEIGHTS"])  # copy taaki original na badle
    if not volume_available:
        redistribute_weight(weights, "volume")
        conflicting_evidence.append(
            "⚠️ Volume data available nahi tha (index spot candles mein "
            "volume 0 aata hai) — factor skip, weight redistribute."
        )
    elif volume_multiplier >= cfg["MIN_VOLUME_MULTIPLIER"]:
        volume_direction = scores["supertrend"] if scores["supertrend"] != 0 else 1.0
        scores["volume"] = volume_direction
        reasoning_tags.append(f"Volume {volume_multiplier:.1f}x average se")
    else:
        scores["volume"] = 0.0
        conflicting_evidence.append(
            f"Volume sirf {volume_multiplier:.1f}x avg — confirmation weak"
        )

    # 5. OI buildup (agar data available hai)
    if oi_buildup_confirmed is None:
        # OI factor skip — uska weight baaki factors mein proportionally redistribute
        redistribute_weight(weights, "oi")
        conflicting_evidence.append(
            "⚠️ OI data available nahi tha — is factor ko skip karke baaki "
            "weights proportionally badhaye gaye hain. Confidence score "
            "isliye poora reliable nahi (Section 8 rule: 'koi factor skip "
            "nahi hona chahiye' abhi 100% follow nahi ho raha)."
        )
    else:
        direction = scores["supertrend"] if scores["supertrend"] != 0 else 1.0
        scores["oi"] = direction if oi_buildup_confirmed else 0.0
        if oi_buildup_confirmed:
            reasoning_tags.append("OI buildup confirm ho raha hai trend ki disha mein")
        else:
            conflicting_evidence.append("OI buildup confirm nahi hua")

    # --- Weighted Confidence Score Calculate Karna ---
    weighted_sum = sum(scores.get(k, 0.0) * weights.get(k, 0.0) for k in weights)
    # weighted_sum range: -1.0 (full bearish) to +1.0 (full bullish)
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
# Chalane ka tarika: repo ROOT se → python3 -m subbrains.trend_follow
# ============================================================
if __name__ == "__main__":
    np.random.seed(7)
    n = 60
    dates = pd.date_range("2025-01-01", periods=n, freq="D")

    base_price = 100
    trend = np.linspace(0, 20, n)  # clear upward trend
    noise = np.random.normal(0, 0.5, n)
    close_prices = base_price + trend + noise.cumsum() * 0.2

    df_test = pd.DataFrame(index=dates)
    df_test["close"] = close_prices
    df_test["open"] = df_test["close"].shift(1).fillna(base_price)
    df_test["high"] = df_test[["open", "close"]].max(axis=1) + np.random.uniform(0.2, 0.8, n)
    df_test["low"] = df_test[["open", "close"]].min(axis=1) - np.random.uniform(0.2, 0.8, n)
    df_test["volume"] = np.random.randint(200000, 400000, n)
    df_test.loc[df_test.index[-1], "volume"] = 700000  # aaj volume spike simulate

    print("=== Trend-Following Sub-Brain Test (Synthetic Uptrend Data) ===\n")

    print("--- Without OI data ---")
    result = evaluate(df_test, current_regime="STRONG_TREND", oi_buildup_confirmed=None)
    for key, value in result.items():
        print(f"{key}: {value}")

    print("\n--- With OI confirmed ---")
    result2 = evaluate(df_test, current_regime="STRONG_TREND", oi_buildup_confirmed=True)
    for key, value in result2.items():
        print(f"{key}: {value}")

    print("\n✅ Test complete — koi crash nahi hua.")
    print("⚠️ Ye SYNTHETIC data hai, real market data se numbers alag honge.")
    
