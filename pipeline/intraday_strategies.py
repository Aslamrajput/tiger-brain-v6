"""
Tiger Brain V6.3 — PURE SUPPLY & DEMAND ZONE SCANNER (Brain 2)
================================================================
Koi VWAP nahi. Koi RS-Score nahi. Koi Black-Scholes nahi. Koi EMA
nahi. Sirf SUPPLY aur DEMAND ZONES.

Yahan major institutional zones detect hote hain:
  - DEMAND ZONE (Support): jahan institutions buy karte hain.
    Price yahan touch karega to rocket up.
  - SUPPLY ZONE (Resistance): jahan institutions sell karte hain.
    Price yahan touch karega to crash down.

Zone detection — PURE PRICE ACTION (no indicators):
  Ek zone = ek consolidation cluster jisme 3+ consecutive bars ka
  tight range (low body-to-range, overlapping highs/lows) ban-ta hai,
  uske pehle ek strong directional move aata hai (impulsive leg).
  Zone ke high/low = cluster ke extreme wicks.

Setup rule (dead simple):
  - Price touches DEMAND ZONE (low <= zone_high)  ➔ BUY ATM Call
  - Price touches SUPPLY ZONE (high >= zone_low)  ➔ BUY ATM Put

⚠️ NO LOOKAHEAD — zone detection sirf `df.iloc[:i+1]` use karta hai.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd


# ============================================================
# Zone detection — pure price action
# ============================================================
def _bar_body_ratio(c: pd.Series) -> float:
    """Body / range ratio of a candle — low = consolidation bar."""
    rng = (c["high"] - c["low"])
    body = abs(c["close"] - c["open"])
    if rng <= 0:
        return 0.0
    return body / rng


def detect_zones(df: pd.DataFrame, i: int, lookback: int = 40,
                 cluster_min: int = 3, impulse_min_pct: float = 0.4) -> list[dict]:
    """
    Detect major Supply & Demand zones up to bar i (no lookahead).

    Method (institutional S/D logic):
      1. Scan last `lookback` bars for a consolidation cluster:
         `cluster_min` consecutive bars with overlapping ranges and
         small bodies (body/range < 0.5) — this is the "base".
      2. Before the base, there must be a strong impulsive move
         (>= impulse_min_pct of price in one direction) — this is the
         institutional leg that created the zone.
      3. DEMAND zone = base BEFORE an up-move (institutions bought).
      4. SUPPLY zone = base BEFORE a down-move (institutions sold).
      Zone bounds = base's wick high & low.

    Returns list of zones: {type: 'demand'|'supply', top, bottom, score, bar}
    """
    if i < cluster_min + 2:
        return []
    window = df.iloc[max(0, i - lookback):i + 1]
    if len(window) < cluster_min + 1:
        return []

    zones = []
    closes = window["close"].astype(float).values
    opens = window["open"].astype(float).values
    highs = window["high"].astype(float).values
    lows = window["low"].astype(float).values

    # Walk the window looking for clusters
    j = 0
    n = len(window)
    while j < n - cluster_min:
        # Check if bars [j, j+cluster_min-1] form a tight base
        cluster = slice(j, j + cluster_min)
        bodies = [abs(closes[k] - opens[k]) for k in range(j, j + cluster_min)]
        ranges = [highs[k] - lows[k] for k in range(j, j + cluster_min)]
        # tight base: small bodies + overlapping ranges
        small_bodies = all(b / max(r, 1e-9) < 0.5 for b, r in zip(bodies, ranges))
        # overlapping: consecutive bars' ranges overlap
        overlapping = True
        for k in range(j, j + cluster_min - 1):
            if lows[k] > highs[k + 1] or highs[k] < lows[k + 1]:
                overlapping = False
                break
        if not (small_bodies and overlapping):
            j += 1
            continue

        base_high = max(highs[j:j + cluster_min])
        base_low = min(lows[j:j + cluster_min])
        base_mid = (base_high + base_low) / 2

        # Look at the bar BEFORE the base (impulsive leg)
        if j == 0:
            j += 1
            continue
        prev_close = closes[j - 1]
        prev_open = opens[j - 1]
        leg_move = (prev_close - prev_open) / max(prev_open, 1e-9)

        # Skip if base is too recent (we want mature zones, not current chop)
        bars_since_base = n - (j + cluster_min)

        # DEMAND zone: strong UP move before the base => institutions bought
        if leg_move >= impulse_min_pct / 100:
            # freshness: zone should not have been broken since
            broken = any(lows[k] < base_low for k in range(j + cluster_min, n))
            if not broken:
                strength = abs(leg_move)
                # prefer zones with more bars since (tested, mature)
                score = 55 + min(strength * 30, 25) + min(bars_since_base * 0.3, 15)
                zones.append({
                    "type": "demand", "top": base_high, "bottom": base_low,
                    "mid": base_mid, "score": round(score, 1),
                    "bars_since": bars_since_base,
                    "bar_idx": i - (n - (j + cluster_min)),
                })
        # SUPPLY zone: strong DOWN move before the base => institutions sold
        elif leg_move <= -impulse_min_pct / 100:
            broken = any(highs[k] > base_high for k in range(j + cluster_min, n))
            if not broken:
                strength = abs(leg_move)
                score = 55 + min(strength * 30, 25) + min(bars_since_base * 0.3, 15)
                zones.append({
                    "type": "supply", "top": base_high, "bottom": base_low,
                    "mid": base_mid, "score": round(score, 1),
                    "bars_since": bars_since_base,
                    "bar_idx": i - (n - (j + cluster_min)),
                })
        j += cluster_min  # skip past this cluster

    # Dedupe: keep strongest zone per type within 0.5% of price
    return zones


# ============================================================
# Setup detection — zone touch
# ============================================================
def scan_zones(df: pd.DataFrame, i: int, lookback: int = 40) -> list[dict]:
    """
    Scan for zone-touch setups at bar i (no lookahead).

    Returns list of setups:
      - Demand touch: price low <= zone top, close > zone bottom => BUY Call
      - Supply touch: price high >= zone bottom, close < zone top => BUY Put
    Each setup: {direction, setup_score, entry_price, stop_loss, zone}
    """
    if i < lookback:
        return []
    zones = detect_zones(df, i, lookback=lookback)
    if not zones:
        return []

    cur = df.iloc[i]
    cur_high = float(cur["high"])
    cur_low = float(cur["low"])
    cur_close = float(cur["close"])
    atr = _simple_range(df, i)

    setups = []
    for z in zones:
        # DEMAND touch: price came down into the demand zone
        if z["type"] == "demand" and cur_low <= z["top"] and cur_close >= z["bottom"]:
            # bullish if close back above zone bottom
            entry = cur_close
            stop = z["bottom"] - atr * 0.3
            setups.append({
                "strategy": "Demand_Zone",
                "direction": "BUY",
                "setup_score": z["score"],
                "entry_price": entry,
                "stop_loss": stop,
                "zone_type": "demand",
                "zone_top": z["top"],
                "zone_bottom": z["bottom"],
                "note": f"demand touch low={cur_low:.1f} zone[{z['bottom']:.1f}-{z['top']:.1f}]",
                "setup_found": True,
            })
        # SUPPLY touch: price came up into the supply zone
        elif z["type"] == "supply" and cur_high >= z["bottom"] and cur_close <= z["top"]:
            entry = cur_close
            stop = z["top"] + atr * 0.3
            setups.append({
                "strategy": "Supply_Zone",
                "direction": "SELL",
                "setup_score": z["score"],
                "entry_price": entry,
                "stop_loss": stop,
                "zone_type": "supply",
                "zone_top": z["top"],
                "zone_bottom": z["bottom"],
                "note": f"supply touch high={cur_high:.1f} zone[{z['bottom']:.1f}-{z['top']:.1f}]",
                "setup_found": True,
            })

    return setups


def _simple_range(df: pd.DataFrame, i: int, window: int = 20) -> float:
    """Average bar range over last `window` bars (no stddev, no ATR formula)."""
    if i < window:
        return float(df["high"].iloc[:i + 1].max() - df["low"].iloc[:i + 1].min() or 1.0)
    w = df.iloc[i - window + 1:i + 1]
    return float((w["high"] - w["low"]).mean() or 1.0)
