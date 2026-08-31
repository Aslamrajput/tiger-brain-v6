"""
Tiger Brain V6+V7 — Regime Classifier
=======================================
Ye module Blueprint ke Section 17 ke rules use karke batata hai ki abhi
market kaunse "regime" mein hai:
    STRONG_TREND | WEAK_TREND | RANGE | COMPRESSION | HIGH_VOL | LOW_VOL

Ye regime label hi Meta-Brain (meta_brain/weighting.py, jo aage banayenge)
ko batayega ki kaunse sub-brain ko kitna weight dena hai (Section 5, 24).

INPUT: Ek pandas DataFrame jisme columns hone chahiye:
    'open', 'high', 'low', 'close', 'volume'
    (index: datetime, daily ya intraday candles — dono chalega)

Optional: 'vix' column agar India VIX bhi saath mein diya jaye (High/Low
Vol regime detect karne ke liye zaroori hai — agar VIX nahi diya, sirf
ATR-based fallback use hoga).

⚠️ IMPORTANT: Ye numbers config/thresholds.py se aate hain, yahan
hardcoded NAHI hain. Kisi bhi threshold ko badalna ho, sirf config file
mein badlo.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

try:
    from config.thresholds import REGIME
except ImportError:
    raise ImportError(
        "config.thresholds import nahi ho paya. Is script ko repo ke ROOT "
        "folder se chalao (jahan config/ aur regime/ dono folders hain), "
        "'regime/' folder ke andar se seedha mat chalao."
    )


# ============================================================
# INDICATOR CALCULATIONS (manual implementation — koi extra
# heavy dependency nahi, sirf pandas/numpy)
# ============================================================

def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range — volatility ka basic measure."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()

    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = true_range.rolling(window=period).mean()
    return atr


def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Average Directional Index — trend strength ka measure (Section 17
    ka main indicator: ADX>25 = Strong Trend, ADX<20 = Range).
    """
    high, low, close = df["high"], df["low"], df["close"]

    plus_dm = high.diff()
    minus_dm = -low.diff()

    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm < 0] = 0
    # Jab dono positive hon, sirf bada wala rakhna (standard ADX rule)
    mask = plus_dm < minus_dm
    plus_dm[mask] = 0
    mask2 = minus_dm < plus_dm
    minus_dm[mask2] = 0

    atr = calculate_atr(df, period)

    plus_di = 100 * (plus_dm.rolling(window=period).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(window=period).mean() / atr)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx = dx.rolling(window=period).mean()

    return adx


def calculate_bollinger_band_width(
    df: pd.DataFrame, period: int = 20, std_dev: int = 2
) -> pd.Series:
    """
    Bollinger Band Width — Compression/Expansion detect karne ke liye
    (Section 17: BB Width 40%+ neeche 20-din avg se = Compression,
    30%+ upar ek candle mein = Expansion).
    """
    close = df["close"]
    sma = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()

    upper_band = sma + (std_dev * std)
    lower_band = sma - (std_dev * std)

    bb_width = (upper_band - lower_band) / sma
    return bb_width


# ============================================================
# REGIME CLASSIFICATION LOGIC (Section 17 rules)
# ============================================================

def classify_regime(df: pd.DataFrame, vix_series: pd.Series | None = None) -> dict:
    """
    Latest candle ke liye regime classify karta hai.

    Args:
        df: OHLCV DataFrame (kam se kam 30-40 rows honi chahiye taaki
            ADX/ATR/BB properly calculate ho sakein — warmup period lagta hai)
        vix_series: Optional India VIX series (same index alignment ke saath)

    Returns:
        dict with:
            'regime': str — 'STRONG_TREND', 'WEAK_TREND', 'RANGE',
                      'COMPRESSION', 'HIGH_VOL', 'LOW_VOL', ya 'NORMAL'
                      agar koi extreme condition nahi mili
            'adx': latest ADX value
            'atr': latest ATR value
            'bb_width': latest Bollinger Band Width
            'bb_width_avg_20d': 20-din ka average BB width (comparison ke liye)
            'vix': latest VIX value (agar diya gaya ho)
            'notes': list of extra observations (jaise "Vol Shock detected")
    """
    if len(df) < 30:
        raise ValueError(
            f"Kam se kam 30 rows chahiye regime classify karne ke liye, "
            f"sirf {len(df)} mile. Indicators ko warmup period chahiye hota hai."
        )

    adx_series = calculate_adx(df)
    atr_series = calculate_atr(df)
    bb_width_series = calculate_bollinger_band_width(df)

    latest_adx = float(adx_series.iloc[-1])
    latest_atr = float(atr_series.iloc[-1])
    latest_bb_width = float(bb_width_series.iloc[-1])

    atr_avg_20d = float(atr_series.tail(20).mean())
    bb_width_avg_20d = float(bb_width_series.tail(20).mean())

    notes = []
    regime = "NORMAL"  # default agar koi extreme condition match na ho

    # --- Trend Regimes (ADX-based, Section 17) ---
    if latest_adx > REGIME["STRONG_TREND_ADX_MIN"]:
        regime = "STRONG_TREND"
    elif REGIME["WEAK_TREND_ADX_MIN"] <= latest_adx <= REGIME["WEAK_TREND_ADX_MAX"]:
        regime = "WEAK_TREND"
    elif latest_adx < REGIME["RANGE_ADX_MAX"]:
        # Range confirm karne ke liye price band bhi check karna hai
        recent_high = df["high"].tail(REGIME["RANGE_LOOKBACK_CANDLES"]).max()
        recent_low = df["low"].tail(REGIME["RANGE_LOOKBACK_CANDLES"]).min()
        band_width = recent_high - recent_low
        max_allowed_band = latest_atr * REGIME["RANGE_ATR_BAND_MULTIPLIER"]

        if band_width <= max_allowed_band:
            regime = "RANGE"
        else:
            notes.append(
                "ADX range jaisa hai par price band ATR-multiplier se zyada "
                "chauda hai — 'RANGE' confirm nahi hua, NORMAL treat kar rahe hain."
            )

    # --- Compression / Expansion Check (Bollinger Band Width) ---
    # Ye trend-regime ke saath bhi ho sakta hai isliye alag se check
    if bb_width_avg_20d > 0:
        bb_drop_pct = ((bb_width_avg_20d - latest_bb_width) / bb_width_avg_20d) * 100
        if bb_drop_pct >= REGIME["COMPRESSION_BB_WIDTH_DROP_PCT"]:
            regime = "COMPRESSION"
            notes.append(
                f"Bollinger Band Width {bb_drop_pct:.1f}% neeche 20-din avg se — "
                "Compression detected."
            )

        prev_bb_width = float(bb_width_series.iloc[-2]) if len(bb_width_series) > 1 else latest_bb_width
        if prev_bb_width > 0:
            bb_rise_pct = ((latest_bb_width - prev_bb_width) / prev_bb_width) * 100
            if bb_rise_pct >= REGIME["EXPANSION_BB_WIDTH_RISE_PCT"]:
                notes.append(
                    f"Bollinger Band Width {bb_rise_pct:.1f}% badha ek candle mein "
                    "— Expansion/Breakout ho sakta hai."
                )

    # --- Volatility Regimes (VIX-based agar available, warna ATR fallback) ---
    latest_vix = None
    if vix_series is not None and len(vix_series) > 0:
        latest_vix = float(vix_series.iloc[-1])

        if latest_vix > REGIME["HIGH_VOL_VIX_MIN"]:
            if regime in ("NORMAL", "RANGE"):
                regime = "HIGH_VOL"
            else:
                notes.append(f"VIX {latest_vix:.1f} High Vol zone mein hai (saath mein {regime} bhi active).")

        elif latest_vix < REGIME["LOW_VOL_VIX_MAX"]:
            if regime in ("NORMAL", "RANGE"):
                regime = "LOW_VOL"
            else:
                notes.append(f"VIX {latest_vix:.1f} Low Vol zone mein hai (saath mein {regime} bhi active).")

        # Volatility Shock check (VIX ka daily jump)
        if len(vix_series) > 1:
            prev_vix = float(vix_series.iloc[-2])
            if prev_vix > 0:
                vix_jump_pct = ((latest_vix - prev_vix) / prev_vix) * 100
                if vix_jump_pct >= REGIME["VOL_SHOCK_VIX_DAILY_JUMP_PCT"]:
                    notes.append(
                        f"⚠️ VIX {vix_jump_pct:.1f}% jump ek din mein — "
                        "Volatility Shock detected. Extra caution rakhna."
                    )
    else:
        # VIX nahi mila, ATR-based fallback
        if atr_avg_20d > 0:
            atr_rise_pct = ((latest_atr - atr_avg_20d) / atr_avg_20d) * 100
            if atr_rise_pct >= REGIME["HIGH_VOL_ATR_RISE_PCT"]:
                notes.append(
                    f"VIX data nahi tha — ATR fallback se High Vol jaisa lag raha hai "
                    f"({atr_rise_pct:.1f}% upar avg se)."
                )
            elif atr_rise_pct <= -REGIME["LOW_VOL_ATR_DROP_PCT"]:
                notes.append(
                    f"VIX data nahi tha — ATR fallback se Low Vol jaisa lag raha hai "
                    f"({abs(atr_rise_pct):.1f}% neeche avg se)."
                )

    return {
        "regime": regime,
        "adx": round(latest_adx, 2) if not np.isnan(latest_adx) else None,
        "atr": round(latest_atr, 4) if not np.isnan(latest_atr) else None,
        "bb_width": round(latest_bb_width, 4) if not np.isnan(latest_bb_width) else None,
        "bb_width_avg_20d": round(bb_width_avg_20d, 4) if not np.isnan(bb_width_avg_20d) else None,
        "vix": round(latest_vix, 2) if latest_vix is not None else None,
        "notes": notes,
    }


# ============================================================
# QUICK MANUAL TEST — synthetic data se (koi internet nahi chahiye)
# Isko chalane ke liye: repo ke ROOT folder se ye command chalao:
#   python3 -m regime.classifier
# ============================================================
if __name__ == "__main__":
    # Synthetic OHLCV data banate hain testing ke liye (60 din, thoda
    # trend + noise) — sirf ye verify karne ke liye ki code crash na ho
    # aur numbers reasonable range mein aayein.
    np.random.seed(42)
    n = 60
    dates = pd.date_range("2025-01-01", periods=n, freq="D")

    base_price = 100
    trend = np.linspace(0, 15, n)  # thoda upward trend
    noise = np.random.normal(0, 1, n)
    close_prices = base_price + trend + noise.cumsum() * 0.3

    df_test = pd.DataFrame(index=dates)
    df_test["close"] = close_prices
    df_test["open"] = df_test["close"].shift(1).fillna(base_price)
    df_test["high"] = df_test[["open", "close"]].max(axis=1) + np.random.uniform(0.2, 1.0, n)
    df_test["low"] = df_test[["open", "close"]].min(axis=1) - np.random.uniform(0.2, 1.0, n)
    df_test["volume"] = np.random.randint(100000, 500000, n)

    # Synthetic VIX bhi test karte hain
    vix_test = pd.Series(
        np.random.uniform(13, 16, n) + np.random.normal(0, 0.5, n),
        index=dates,
    )

    print("=== Regime Classifier Test (Synthetic Data) ===\n")
    result = classify_regime(df_test, vix_series=vix_test)

    for key, value in result.items():
        print(f"{key}: {value}")

    print("\n✅ Test complete — koi crash nahi hua, code chal raha hai.")
    print("⚠️ Ye SYNTHETIC data hai, real market data se numbers alag honge.")
  
