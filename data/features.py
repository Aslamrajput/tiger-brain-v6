"""Live Feature Store — extracts ML features from Angel WS buffer + data maps.

Provides a real-time feature vector for the ML inference gate. All features
are derived from live market data (WS 1m candles, 15m zones, option chain)
with zero look-ahead. The feature vector is consumed by pipeline.ml_engine
to compute win_probability before order placement.

Feature list (must match ML_ENGINE["FEATURE_COLUMNS"] in thresholds.py):
  zone_strength      — best zone score (0-100) from detect_zones
  volume_velocity    — latest 1m volume / rolling avg (surge factor)
  option_chain_pcr   — put-call OI ratio from fetch_pcr
  live_iv_skew       — |ATM IV - nearest OTM IV| / ATM IV (skew signal)
  setup_score        — 7-brain / scalper alignment score
  body_pct           — latest 1m body as % of range
  vol_surge_ratio    — 1m volume surge vs 10-bar average
  rsi                — 14-period RSI on 15m closes
  brain_alignment    — count of aligned 7-brains (0-7)
  is_scalper         — 1.0 if scalper signal, else 0.0
  is_momentum_hunter — 1.0 if momentum-hunter signal, else 0.0
  sensex_trend       — broad-index 30m trend: +1 bullish / -1 bearish / 0 neutral
  vix_level          — India VIX close (0.0 if unavailable)
"""
from __future__ import annotations

import logging
import math
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Broad-index proxy symbols used to derive sensex_trend. Sensex (BSE) is not
# in the NSE F&O data set, so we use the most liquid NSE index as proxy.
_INDEX_PROXIES = ["^NSEI", "NIFTY", "^NSEBANK", "BANKNIFTY"]

# Feature column order — MUST match config/thresholds.py ML_ENGINE
# 13 base features + 3 sniper features (Sep 2026, TIGER SNIPER ADVANCED V2):
#   sniper_zone_strength   — raw 0-100 SMC confluence score from mcx_scanner
#   fvg_size               — Fair Value Gap width as fraction of ATR
#   commodity_volatility   — ATR(14) as % of price (MCX volatility regime)
FEATURE_COLUMNS = [
    "zone_strength", "volume_velocity", "option_chain_pcr", "live_iv_skew",
    "setup_score", "body_pct", "vol_surge_ratio", "rsi",
    "brain_alignment", "is_scalper", "is_momentum_hunter",
    "sensex_trend", "vix_level",
    # --- sniper features (14-16) ---
    "sniper_zone_strength", "fvg_size", "commodity_volatility",
]


def _safe_div(a: float, b: float, default: float = 0.0) -> float:
    if b == 0 or not math.isfinite(b):
        return default
    return a / b


def _compute_sensex_trend(data_map_15m: dict) -> float:
    """Broad-index 30m trend from the index 15m candles.

    sensex_30m_change = close.pct_change(6) * 100  (6×15m = 90 min window;
    the closest liquid proxy). sensex_trend is bucketed:
      +1  if change > +0.3%  (bullish broad market)
      -1  if change < -0.3%  (bearish broad market)
       0  otherwise (neutral / chop)

    Returns 0.0 when no index data is available (safe default = neutral).
    """
    for proxy in _INDEX_PROXIES:
        df = data_map_15m.get(proxy)
        if df is None or df.empty or "close" not in df.columns:
            continue
        closes = df["close"].astype(float)
        if len(closes) < 7:
            return 0.0
        change = float(closes.pct_change(6).iloc[-1] * 100.0)
        if math.isnan(change):
            return 0.0
        if change > 0.3:
            return 1.0
        if change < -0.3:
            return -1.0
        return 0.0
    return 0.0


def _compute_vix_level(broker) -> float:
    """India VIX close, if available via broker. Falls back to 0.0.

    VIX is fetched opportunistically; absence does not break the pipeline.
    """
    if broker is None:
        return 0.0
    try:
        fn = getattr(broker, "get_vix", None)
        if callable(fn):
            v = float(fn())
            if math.isfinite(v) and v > 0:
                return v
    except Exception:
        pass
    return 0.0


def sensex_blocks_option(sensex_trend: float, option_type: str) -> bool:
    """Directional filter: block options that fight the broad trend.

      bullish market (sensex_trend = +1) + PE  → BLOCK (don't buy puts in rally)
      bearish market (sensex_trend = -1) + CE → BLOCK (don't buy calls in selloff)

    Returns True when the option should be BLOCKED.
    """
    opt = (option_type or "").upper().strip()
    if sensex_trend > 0 and opt == "PE":
        return True
    if sensex_trend < 0 and opt == "CE":
        return True
    return False


def _compute_rsi(closes: pd.Series, period: int = 14) -> float:
    """Wilder RSI on the last `period+1` closes. Returns 50.0 on failure."""
    if closes is None or len(closes) < period + 1:
        return 50.0
    series = closes.astype(float).iloc[-(period + 1):]
    deltas = series.diff().dropna()
    if deltas.empty:
        return 50.0
    gains = deltas.clip(lower=0).rolling(window=period, min_periods=period).mean()
    losses = (-deltas.clip(upper=0)).rolling(window=period, min_periods=period).mean()
    last_gain = gains.iloc[-1]
    last_loss = losses.iloc[-1]
    if math.isnan(last_gain) or math.isnan(last_loss):
        return 50.0
    if last_loss == 0:
        return 100.0
    rs = last_gain / last_loss
    return float(100.0 - (100.0 / (1.0 + rs)))


def _compute_iv_skew(df_15m: Optional[pd.DataFrame], broker, symbol: str) -> float:
    """Skew = |ATM IV - OTM IV| / ATM IV from optionGreek chain.

    Falls back to 0.0 if chain unavailable. Uses a single optionGreek
    call (throttled by fetch_live_greeks upstream).
    """
    if broker is None or broker.smart_api is None or df_15m is None or df_15m.empty:
        return 0.0
    try:
        from data.loader import fetch_option_chain_oi
        chain = fetch_option_chain_oi(broker, underlying=symbol, strikes_around_atm=5)
        if chain is None or chain.empty:
            return 0.0
        if "iv" not in chain.columns or "delta" not in chain.columns:
            return 0.0
        ivs = chain["iv"].astype(float)
        deltas = chain["delta"].abs()
        if ivs.empty or deltas.empty:
            return 0.0
        # ATM = strike with delta closest to 0.50
        atm_idx = (deltas - 0.50).abs().idxmin()
        atm_iv = float(ivs.loc[atm_idx])
        # OTM = delta 0.30-0.40
        otm_mask = (deltas >= 0.30) & (deltas <= 0.40)
        otm_ivs = ivs[otm_mask]
        if otm_ivs.empty or atm_iv <= 0:
            return 0.0
        otm_iv = float(otm_ivs.mean())
        return float(abs(atm_iv - otm_iv) / atm_iv)
    except Exception:
        return 0.0


def extract_live_features(
    symbol: str,
    signal: dict,
    data_map_15m: dict,
    data_map_1m: dict,
    broker,
    pcr_value: float = 1.0,
) -> dict:
    """Build the live feature vector for ML inference.

    Args:
        symbol: underlying symbol (e.g. "NIFTY", "HDFCBANK")
        signal: the scanner signal dict (has setup_score, zone_type, etc.)
        data_map_15m: {symbol: DataFrame} of 15m candles
        data_map_1m: {symbol: DataFrame} of 1m candles
        broker: AngelBroker instance (for IV skew fetch)
        pcr_value: latest PCR value (pre-fetched by scanner)

    Returns:
        dict of feature_name → float value (all FEATURE_COLUMNS present)
    """
    feats: dict[str, float] = {col: 0.0 for col in FEATURE_COLUMNS}

    # --- zone_strength ---
    zone_score = 0.0
    if "zone_score" in signal:
        zone_score = float(signal["zone_score"])
    elif "score_details" in signal and isinstance(signal["score_details"], dict):
        zone_score = float(signal["score_details"].get("zone_score", 0.0))
    # If zone score not in signal, derive from best zone in 15m data
    if zone_score == 0.0 and symbol in data_map_15m:
        try:
            from pipeline.intraday_strategies import detect_zones
            df_15m = data_map_15m[symbol]
            if df_15m is not None and not df_15m.empty:
                zones = detect_zones(df_15m, len(df_15m) - 2, lookback=len(df_15m) - 2)
                if zones:
                    zone_score = max(z.get("score", 0.0) for z in zones)
        except Exception:
            pass
    feats["zone_strength"] = min(zone_score / 100.0, 1.0)

    # --- volume_velocity + vol_surge_ratio ---
    vol_surge = 1.0
    vol_velocity = 0.0
    if symbol in data_map_1m:
        df_1m = data_map_1m[symbol]
        if df_1m is not None and not df_1m.empty and "volume" in df_1m.columns:
            vols = df_1m["volume"].astype(float)
            if len(vols) >= 11:
                latest_vol = float(vols.iloc[-1])
                avg_vol = float(vols.iloc[-11:-1].mean())
                vol_surge = _safe_div(latest_vol, avg_vol, 1.0)
                # velocity = rate of change of volume (last 3 bars)
                recent = vols.iloc[-3:]
                if len(recent) >= 2 and recent.iloc[0] > 0:
                    vol_velocity = float((recent.iloc[-1] - recent.iloc[0]) / recent.iloc[0])
    feats["vol_surge_ratio"] = vol_surge
    feats["volume_velocity"] = vol_velocity

    # --- option_chain_pcr ---
    feats["option_chain_pcr"] = max(float(pcr_value), 0.0)

    # --- live_iv_skew ---
    df_15m = data_map_15m.get(symbol)
    feats["live_iv_skew"] = _compute_iv_skew(df_15m, broker, symbol)

    # --- setup_score (normalized 0-1) ---
    setup_score = float(signal.get("setup_score", 0.0))
    feats["setup_score"] = min(setup_score / 100.0, 1.0)

    # --- body_pct ---
    body_pct = 0.0
    if symbol in data_map_1m:
        df_1m = data_map_1m[symbol]
        if df_1m is not None and not df_1m.empty:
            row = df_1m.iloc[-1]
            o = float(row.get("open", 0) or 0)
            c = float(row.get("close", 0) or 0)
            h = float(row.get("high", 0) or 0)
            l = float(row.get("low", 0) or 0)
            rng = h - l
            body = abs(c - o)
            body_pct = _safe_div(body, rng, 0.0) * 100.0
    feats["body_pct"] = body_pct

    # --- rsi ---
    rsi = 50.0
    if symbol in data_map_15m:
        df_15m_sym = data_map_15m[symbol]
        if df_15m_sym is not None and not df_15m_sym.empty and "close" in df_15m_sym.columns:
            rsi = _compute_rsi(df_15m_sym["close"], period=14)
    feats["rsi"] = rsi

    # --- brain_alignment ---
    feats["brain_alignment"] = float(signal.get("brain_alignment", 0.0)) / 7.0

    # --- is_scalper / is_momentum_hunter ---
    feats["is_scalper"] = 1.0 if signal.get("is_scalper", False) else 0.0
    feats["is_momentum_hunter"] = 1.0 if signal.get("is_momentum_hunter", False) else 0.0

    # --- sensex_trend: broad-index 30m directional bias ---
    feats["sensex_trend"] = _compute_sensex_trend(data_map_15m)

    # --- vix_level: India VIX close (opportunistic, 0.0 if unavailable) ---
    feats["vix_level"] = _compute_vix_level(broker)

    # --- SNIPER FEATURES (14-16) — sourced from mcx_scanner signal ---
    # sniper_zone_strength: raw 0-100 SMC confluence score (0 if no scanner signal)
    feats["sniper_zone_strength"] = float(signal.get("sniper_zone_strength",
                                       signal.get("zone_strength", 0.0)))
    # fvg_size: Fair Value Gap width as fraction of ATR
    feats["fvg_size"] = float(signal.get("fvg_size", 0.0))
    # commodity_volatility: ATR(14) as % of price (MCX volatility regime)
    feats["commodity_volatility"] = float(signal.get("commodity_volatility", 0.0))

    # Sanitize: replace NaN/Inf with 0
    for k, v in feats.items():
        if not isinstance(v, (int, float)) or not math.isfinite(v):
            feats[k] = 0.0
    return feats


def feature_vector(feats: dict) -> np.ndarray:
    """Convert feature dict to ordered numpy array for model.predict_proba."""
    return np.array([[feats.get(col, 0.0) for col in FEATURE_COLUMNS]], dtype=np.float64)
