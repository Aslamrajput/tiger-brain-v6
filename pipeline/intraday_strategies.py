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
# FVG (Fair Value Gap) — multi-timeframe institutional footprint
# ============================================================
def detect_fvg(df: pd.DataFrame, i: int, lookback: int = 40) -> list[dict]:
    """Detect Fair Value Gaps (3-bar imbalance) up to bar i.

    Bullish FVG: bar[i-2].high < bar[i].low  (gap up, unfilled)
    Bearish FVG: bar[i-2].low > bar[i].high  (gap down, unfilled)

    FVGs represent institutional order flow imbalances — price tends to
    revisit and fill these gaps. Unfilled FVGs near zones strengthen the
    zone's institutional significance.

    Returns list of FVGs: {type: 'bullish'|'bearish', top, bottom, bar, filled}
    """
    if i < 2:
        return []
    fvgs = []
    start = max(2, i - lookback + 1)
    for idx in range(start, i + 1):
        h1 = float(df.iloc[idx - 2]["high"])
        l1 = float(df.iloc[idx - 2]["low"])
        h3 = float(df.iloc[idx]["high"])
        l3 = float(df.iloc[idx]["low"])
        # Bullish FVG: gap between bar1.high and bar3.low
        if l3 > h1:
            gap_bottom = h1
            gap_top = l3
            # Check if filled by any subsequent bar
            filled = False
            if idx < i:
                for k in range(idx + 1, i + 1):
                    if float(df.iloc[k]["low"]) <= gap_bottom:
                        filled = True
                        break
            fvgs.append({
                "type": "bullish", "top": gap_top, "bottom": gap_bottom,
                "bar": idx, "filled": filled,
            })
        # Bearish FVG: gap between bar1.low and bar3.high
        elif h3 < l1:
            gap_top = l1
            gap_bottom = h3
            filled = False
            if idx < i:
                for k in range(idx + 1, i + 1):
                    if float(df.iloc[k]["high"]) >= gap_top:
                        filled = True
                        break
            fvgs.append({
                "type": "bearish", "top": gap_top, "bottom": gap_bottom,
                "bar": idx, "filled": filled,
            })
    return fvgs


# ============================================================
# Volume-based absorption pivots — genuine institutional footprint
# ============================================================
def detect_absorption_pivots(
    df: pd.DataFrame, i: int, lookback: int = 40,
    vol_multiplier: float = 1.5,
) -> list[dict]:
    """Detect high-volume absorption pivots up to bar i.

    An absorption pivot is a bar where:
      - Volume >= vol_multiplier × rolling average (institutional participation)
      - Price rejects: large wick relative to body (absorption = rejection)
      - Results in a pivot high (supply) or pivot low (demand)

    These mark genuine institutional footprints — not noise.

    Returns list of pivots: {type: 'demand'|'supply', price, bar, vol_ratio}
    """
    if i < 10:
        return []
    window = df.iloc[max(0, i - lookback):i + 1]
    if len(window) < 10:
        return []
    closes = window["close"].astype(float).values
    opens = window["open"].astype(float).values
    highs = window["high"].astype(float).values
    lows = window["low"].astype(float).values
    vols = window["volume"].astype(float).values

    avg_vol = np.mean(vols) if len(vols) > 0 else 1
    if avg_vol <= 0:
        avg_vol = 1

    pivots = []
    for k in range(2, len(window) - 1):
        vol_ratio = vols[k] / avg_vol
        if vol_ratio < vol_multiplier:
            continue
        body = abs(closes[k] - opens[k])
        rng = highs[k] - lows[k]
        if rng <= 0:
            continue
        upper_wick = highs[k] - max(closes[k], opens[k])
        lower_wick = min(closes[k], opens[k]) - lows[k]
        # Absorption: wick > 2× body (price rejected at level)
        # Pivot low (demand): large lower wick = buyers absorbed selling
        if lower_wick > 2 * body and lower_wick > upper_wick:
            # Check it's a local low (lower than neighbors)
            if lows[k] <= lows[k - 1] and lows[k] <= lows[k + 1]:
                pivots.append({
                    "type": "demand", "price": lows[k],
                    "bar": i - (len(window) - 1 - k), "vol_ratio": round(vol_ratio, 2),
                })
        # Pivot high (supply): large upper wick = sellers absorbed buying
        elif upper_wick > 2 * body and upper_wick > lower_wick:
            if highs[k] >= highs[k - 1] and highs[k] >= highs[k + 1]:
                pivots.append({
                    "type": "supply", "price": highs[k],
                    "bar": i - (len(window) - 1 - k), "vol_ratio": round(vol_ratio, 2),
                })
    return pivots


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
                 cluster_min: int = 3, impulse_min_pct: float = 0.3) -> list[dict]:
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

    REAL ZONE GATES (anti-fake):
      - cluster_min=3: 3+ bars consolidation
      - impulse_min_pct=0.3: institutional move (checked over 1-3 bars)
      - freshness: zone broken only on CLOSE violation (wicks = tests, OK)

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

        # Look at the bar(s) BEFORE the base (impulsive leg)
        # Real institutional impulse can be 1-3 bars. Check strongest.
        if j == 0:
            j += 1
            continue
        # Check last 1-3 bars before base for strongest impulse
        leg_move = 0.0
        for lookback_bars in range(1, min(4, j + 1)):
            prev_close = closes[j - lookback_bars]
            prev_open = opens[j - lookback_bars]
            move = (prev_close - prev_open) / max(prev_open, 1e-9)
            if abs(move) > abs(leg_move):
                leg_move = move

        # Skip if base is too recent (we want mature zones, not current chop)
        bars_since_base = n - (j + cluster_min)

        # DEMAND zone: strong UP move before the base => institutions bought
        if leg_move >= impulse_min_pct / 100:
            # freshness: zone broken only if CLOSE goes below (wick tests OK)
            broken = any(closes[k] < base_low for k in range(j + cluster_min, n))
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
            broken = any(closes[k] > base_high for k in range(j + cluster_min, n))
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

    # === DEDUP: merge overlapping zones, keep strongest per type ===
    # Real institutional zones don't overlap. If two zones of the same
    # type overlap (>50% area overlap), keep the one with higher score.
    if len(zones) <= 1:
        return _enrich_zones_with_smc(zones, df, i, lookback)

    deduped = []
    for z in sorted(zones, key=lambda x: x["score"], reverse=True):
        overlap_found = False
        for d in deduped:
            if d["type"] != z["type"]:
                continue
            # Check area overlap
            overlap_top = min(d["top"], z["top"])
            overlap_bot = max(d["bottom"], z["bottom"])
            if overlap_top > overlap_bot:
                z_area = z["top"] - z["bottom"]
                d_area = d["top"] - d["bottom"]
                overlap_area = overlap_top - overlap_bot
                if overlap_area / min(z_area, d_area) > 0.5:
                    overlap_found = True
                    break
        if not overlap_found:
            deduped.append(z)

    return _enrich_zones_with_smc(deduped, df, i, lookback)


def _enrich_zones_with_smc(
    zones: list[dict], df: pd.DataFrame, i: int, lookback: int
) -> list[dict]:
    """Enrich zones with FVG confluence + absorption pivot data.

    Zones that have nearby unfilled FVGs or absorption pivots get a
    score boost — this is genuine institutional footprint, not noise.
    """
    if not zones:
        return zones
    try:
        fvgs = detect_fvg(df, i, lookback)
        pivots = detect_absorption_pivots(df, i, lookback)
    except Exception:
        return zones

    for z in zones:
        z["fvg_confluence"] = 0
        z["absorption_confluence"] = 0
        # FVG confluence: unfilled FVG near zone (within 1% of price)
        for fvg in fvgs:
            if fvg.get("filled", True):
                continue
            zone_mid = (z["top"] + z["bottom"]) / 2
            fvg_mid = (fvg["top"] + fvg["bottom"]) / 2
            if zone_mid > 0:
                dist_pct = abs(fvg_mid - zone_mid) / zone_mid * 100
                if dist_pct < 1.0:
                    # Type alignment: bullish FVG + demand, bearish FVG + supply
                    aligned = (
                        (fvg["type"] == "bullish" and z["type"] == "demand")
                        or (fvg["type"] == "bearish" and z["type"] == "supply")
                    )
                    if aligned:
                        z["fvg_confluence"] += 1
                        z["score"] = round(z["score"] + 5, 1)
        # Absorption pivot confluence: pivot near zone
        for piv in pivots:
            if piv["type"] != z["type"]:
                continue
            zone_mid = (z["top"] + z["bottom"]) / 2
            if zone_mid > 0:
                dist_pct = abs(piv["price"] - zone_mid) / zone_mid * 100
                if dist_pct < 1.0:
                    z["absorption_confluence"] += 1
                    z["score"] = round(
                        z["score"] + min(piv["vol_ratio"] * 2, 10), 1)
    return zones


# ============================================================
# V6.7 — INSTITUTIONAL VOLUME PROFILE (VAH / VAL / POC)
# ============================================================
def compute_volume_profile(
    df: pd.DataFrame,
    i: int,
    lookback: int = 50,
    n_bins: int = 20,
    value_area_pct: float = 70.0,
) -> dict:
    """Compute institutional Volume Profile — POC, VAH, VAL.

    Builds a volume histogram over `lookback` bars by binning price into
    `n_bins` horizontal slices. The bin with the highest volume is the
    Point of Control (POC). The Value Area (VA) is the price range
    containing `value_area_pct` of total volume, centered on POC.

    This is the institutional footprint — where smart money accumulated
    or distributed. Zones coinciding with POC/VA edges are the strongest.

    Args:
        df: OHLCV dataframe.
        i: current bar index (no lookahead — uses df.iloc[:i+1]).
        lookback: number of bars to include in the profile.
        n_bins: histogram resolution (more bins = more precise POC).
        value_area_pct: % of volume defining the Value Area (standard 70%).

    Returns:
        {poc, vah, val, total_volume, va_volume, va_pct, bins}
        or empty dict if insufficient data.
    """
    if i < 5:
        return {}
    window = df.iloc[max(0, i - lookback):i + 1]
    if len(window) < 5:
        return {}

    highs = window["high"].astype(float).values
    lows = window["low"].astype(float).values
    vols = window["volume"].astype(float).values

    price_min = float(np.min(lows))
    price_max = float(np.max(highs))
    if price_max <= price_min:
        return {}

    bin_width = (price_max - price_min) / n_bins
    if bin_width <= 0:
        return {}

    # Distribute each bar's volume across the price bins it spans
    bin_volumes = np.zeros(n_bins)
    for k in range(len(window)):
        bar_low = max(lows[k], price_min)
        bar_high = min(highs[k], price_max)
        if bar_high <= bar_low:
            bar_high = bar_low + bin_width * 0.01
        lo_bin = int((bar_low - price_min) / bin_width)
        hi_bin = int((bar_high - price_min) / bin_width)
        lo_bin = max(0, min(n_bins - 1, lo_bin))
        hi_bin = max(0, min(n_bins - 1, hi_bin))
        span = max(hi_bin - lo_bin, 1)
        vol_per_bin = vols[k] / span
        for b in range(lo_bin, hi_bin + 1):
            bin_volumes[b] += vol_per_bin

    total_volume = float(bin_volumes.sum())
    if total_volume <= 0:
        return {}

    # POC = bin with highest volume
    poc_bin = int(np.argmax(bin_volumes))
    poc = price_min + (poc_bin + 0.5) * bin_width

    # Value Area: expand outward from POC until value_area_pct captured
    target_va = total_volume * (value_area_pct / 100.0)
    va_volume = bin_volumes[poc_bin]
    lo, hi = poc_bin, poc_bin
    while va_volume < target_va and (lo > 0 or hi < n_bins - 1):
        # Expand to whichever side has more volume (standard VA method)
        below = bin_volumes[lo - 1] if lo > 0 else -1
        above = bin_volumes[hi + 1] if hi < n_bins - 1 else -1
        if above >= below and hi < n_bins - 1:
            hi += 1
            va_volume += bin_volumes[hi]
        elif lo > 0:
            lo -= 1
            va_volume += bin_volumes[lo]
        else:
            break

    vah = price_min + (hi + 1) * bin_width
    val = price_min + lo * bin_width

    return {
        "poc": round(poc, 2),
        "vah": round(vah, 2),
        "val": round(val, 2),
        "total_volume": total_volume,
        "va_volume": round(va_volume, 2),
        "va_pct": round(va_volume / total_volume * 100, 1),
        "price_min": round(price_min, 2),
        "price_max": round(price_max, 2),
    }


def zone_at_vp_edge(zone: dict, vp: dict, tolerance_pct: float = 0.5) -> bool:
    """Check if a demand/supply zone coincides with a Volume Profile edge.

    Zones at VAH/VAL/POC are institutional order blocks — the strongest
    reversals happen there. This is the "ALERT readiness" trigger.

    Args:
        zone: {type, top, bottom, ...}
        vp: {poc, vah, val, ...} from compute_volume_profile
        tolerance_pct: how close (in % of price) zone must be to VP edge

    Returns:
        True if zone edge is within tolerance of POC, VAH, or VAL.
    """
    if not vp or not zone:
        return False
    ref = vp.get("poc", 0) or vp.get("vah", 0) or vp.get("val", 0)
    if ref <= 0:
        return False
    tol = ref * tolerance_pct / 100
    zone_edge = zone.get("bottom", 0) if zone.get("type") == "demand" else zone.get("top", 0)
    edges = [vp.get("poc", 0), vp.get("vah", 0), vp.get("val", 0)]
    return any(abs(zone_edge - e) <= tol for e in edges if e > 0)


# ============================================================
# V6.6 — INSTITUTIONAL ZONE QUALITY (explosive rejection filter)
# ============================================================
def zone_explosive_quality(df: pd.DataFrame, base_bar_idx: int,
                           zone_type: str, expansion_lookback: int = 5,
                           min_expansion_atr: float = 1.0) -> tuple[bool, float]:
    """
    Only validate a zone if the HISTORICAL REJECTION from that level caused an
    immediate, high-volume EXPLOSIVE expansion move. Weak/choppy/retail
    consolidation zones are REJECTED.

    Checks the `expansion_lookback` bars immediately AFTER the zone base:
      - DEMAND zone: price must EXPAND UP (a close rises >= min_expansion_atr * ATR
        above the base high within the window) on volume >= the prior average.
      - SUPPLY zone: price must EXPAND DOWN (a close falls >= ... ATR below
        the base low) on volume >= avg.

    The expansion may develop over a few bars (not only the immediate next bar),
    which matches how real institutional rejections unfold.

    Returns (is_explosive, expansion_strength_pct).
    """
    n = len(df)
    start = base_bar_idx + 1
    end = min(n, start + expansion_lookback)
    if end <= start or start < 1:
        return (False, 0.0)
    after = df.iloc[start:end]
    if len(after) < 2:
        return (False, 0.0)
    base_high = float(df.iloc[base_bar_idx]["high"])
    base_low = float(df.iloc[base_bar_idx]["low"])
    # ATR proxy: average bar range over the 20 bars before the base
    atr_win = df.iloc[max(0, base_bar_idx - 20):base_bar_idx]
    atr = float((atr_win["high"] - atr_win["low"]).mean() or 1.0)
    # volume baseline: avg volume over 20 bars before the base
    vol_base = float(atr_win["volume"].mean() or 0.0)
    after_vol = float(after["volume"].mean() or 0.0)
    # volume surge: expansion must carry volume. Some sources (e.g. yfinance
    # for ^NSEI/^NSEBANK index tickers) report ZERO volume — in that case
    # skip the volume gate and rely on the expansion-ATR confirmation alone.
    vol_surge = True if vol_base <= 0 else after_vol >= vol_base * 1.0

    closes = after["close"].astype(float).values
    if zone_type == "demand":
        # explosive up-rejection: furthest close above base high
        max_close = float(closes.max())
        expansion = max_close - base_high
    else:  # supply
        min_close = float(closes.min())
        expansion = base_low - min_close
    expansion_atr = expansion / max(atr, 1e-9)
    is_explosive = expansion_atr >= min_expansion_atr and vol_surge
    expansion_pct = round(expansion / base_high * 100.0, 2) if base_high else 0.0
    return (is_explosive, expansion_pct)


def detect_zones_explosive(df: pd.DataFrame, i: int, lookback: int = 40,
                           cluster_min: int = 4, impulse_min_pct: float = 0.6,
                           require_explosive: bool = True,
                           expansion_lookback: int = 3,
                           min_expansion_atr: float = 1.5) -> list[dict]:
    """
    V6.6 zone detection: detect_zones + the explosive-rejection quality gate.
    Each returned zone carries `explosive` (bool) and `expansion_pct`.
    If require_explosive, only explosive-quality zones are returned.
    """
    zones = detect_zones(df, i, lookback, cluster_min, impulse_min_pct)
    kept = []
    for z in zones:
        is_exp, exp_pct = zone_explosive_quality(
            df, z["bar_idx"], z["type"], expansion_lookback, min_expansion_atr
        )
        z["explosive"] = is_exp
        z["expansion_pct"] = exp_pct
        if require_explosive and not is_exp:
            continue
        # boost score for stronger explosive rejections
        if is_exp:
            z["score"] = round(z["score"] + min(exp_pct * 1.5, 20), 1)
        kept.append(z)
    return kept


# ============================================================
# V6.6 — 1m LIQUIDITY SWEEP (smart-money stop-hunt confirmation)
# ============================================================
def liquidity_sweep(df_1m, i: int, direction: str, lookback: int = 20) -> tuple[bool, str]:
    """
    Detect a recent 1-minute liquidity sweep (institutional stop-hunt) that
    confirms the boom entry.

    A liquidity sweep = price briefly PIERCES a recent local extreme then
    snaps back — institutions grab stops then reverse.
      - For DEMAND/BUY: a 1m low within the last `lookback` bars made a NEW
        local low (below the prior lookback-low) but a later bar closed back
        ABOVE that level → bear-trap sweep.
      - For SUPPLY/SELL: a 1m high made a NEW local high then closed back
        BELOW → bull-trap sweep.

    Returns (swept, reason). Looks back up to `lookback` 1m bars.
    """
    if i < lookback + 2:
        return (False, "insufficient 1m bars")
    window = df_1m.iloc[i - lookback:i + 1]
    lows = window["low"].astype(float).values
    highs = window["high"].astype(float).values
    closes = window["close"].astype(float).values
    n = len(window)
    # reference range = the FIRST half of the window (the established range
    # before the sweep). Using the full window would include the sweep itself.
    ref_end = max(n // 2, 2)
    if direction == "BUY":  # demand → bear-trap sweep (sweep lows then reverse up)
        prior_low = float(lows[:ref_end].min())
        pierced = [k for k in range(ref_end, n) if lows[k] < prior_low]
        if not pierced:
            return (False, "no low sweep")
        last_pierce = pierced[-1]
        snap = any(closes[k] > prior_low for k in range(last_pierce + 1, n))
        if snap:
            return (True, "bear-trap-sweep")
        return (False, "no snap-back")
    else:  # SELL → bull-trap sweep (sweep highs then reverse down)
        prior_high = float(highs[:ref_end].max())
        pierced = [k for k in range(ref_end, n) if highs[k] > prior_high]
        if not pierced:
            return (False, "no high sweep")
        last_pierce = pierced[-1]
        snap = any(closes[k] < prior_high for k in range(last_pierce + 1, n))
        if snap:
            return (True, "bull-trap-sweep")
        return (False, "no snap-back")


# ============================================================
def _rejection_wick(bar) -> tuple[float, str]:
    """Return (wick_ratio, side) — institutional rejection wick size + side."""
    o, c, h, l = (float(bar["open"]), float(bar["close"]),
                  float(bar["high"]), float(bar["low"]))
    rng = h - l
    if rng <= 0:
        return (0.0, "none")
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    if lower_wick > upper_wick and lower_wick > body * 0.8:
        return (lower_wick / rng, "lower")
    if upper_wick > lower_wick and upper_wick > body * 0.8:
        return (upper_wick / rng, "upper")
    return (max(upper_wick, lower_wick) / rng, "none")


def confirm_zone_reversal(df, i, zone_type) -> tuple[bool, str]:
    """
    5-min equivalent reversal confirmation on the 15m bar AFTER a zone touch.

    A zone touch at bar i-1 must be CONFIRMED by bar i before entry:
      - DEMAND (buy call): bar i closes bullish (close > open) AND shows a
        lower rejection wick (institutions bought the dip), OR closes above
        the touch bar's high (lower-timeframe structural break up).
      - SUPPLY (buy put): bar i closes bearish (close < open) AND shows an
        upper rejection wick (institutions sold the rip), OR closes below
        the touch bar's low (structural break down).

    Returns (confirmed, reason). Uses only bars i-1 and i (no lookahead).
    """
    if i < 2:
        return (False, "insufficient bars")
    cur = df.iloc[i]
    prev = df.iloc[i - 1]
    o, c, h, l = (float(cur["open"]), float(cur["close"]),
                  float(cur["high"]), float(cur["low"]))
    prev_h, prev_l = float(prev["high"]), float(prev["low"])
    wick_ratio, wick_side = _rejection_wick(cur)

    if zone_type == "demand":
        bullish_close = c > o
        rejection = wick_side == "lower" and wick_ratio >= 0.35
        struct_break = c > prev_h  # broke above the touch bar's high
        if bullish_close and (rejection or struct_break):
            why = "lower-rejection" if rejection else "struct-break-up"
            return (True, why)
        return (False, f"no bullish confirm (close {c:.0f} vs open {o:.0f})")
    # supply
    bearish_close = c < o
    rejection = wick_side == "upper" and wick_ratio >= 0.35
    struct_break = c < prev_l  # broke below the touch bar's low
    if bearish_close and (rejection or struct_break):
        why = "upper-rejection" if rejection else "struct-break-down"
        return (True, why)
    return (False, f"no bearish confirm (close {c:.0f} vs open {o:.0f})")


def scan_zones(df: pd.DataFrame, i: int, lookback: int = 40, require_confirm: bool = True) -> list[dict]:
    """
    Scan for CONFIRMED zone-touch setups at bar i (no lookahead).

    Flow:
      1. Detect zones using data up to bar i.
      2. Check if the PREVIOUS bar (i-1) touched a zone.
      3. If require_confirm: bar i must confirm the reversal (institutional
         rejection wick or structural break) before a setup fires.
      4. Entry triggers on bar i close only when confirmed.

    This prevents operator fakeouts — no blind raw zone touches.

    Returns list of confirmed setups with direction/score/entry/stop/zone.
    """
    if i < lookback:
        return []
    zones = detect_zones(df, i, lookback=lookback)
    if not zones:
        return []

    # The touch happens on the PREVIOUS bar (i-1); confirmation on bar i.
    touch_bar = df.iloc[i - 1]
    touch_high = float(touch_bar["high"])
    touch_low = float(touch_bar["low"])
    touch_close = float(touch_bar["close"])
    cur = df.iloc[i]
    cur_close = float(cur["close"])
    atr = _simple_range(df, i)

    setups = []
    for z in zones:
        # Did the previous bar touch this zone?
        demand_touched = (z["type"] == "demand" and touch_low <= z["top"]
                          and touch_close >= z["bottom"])
        supply_touched = (z["type"] == "supply" and touch_high >= z["bottom"]
                          and touch_close <= z["top"])
        if not (demand_touched or supply_touched):
            continue

        zone_type = z["type"]
        if require_confirm:
            confirmed, why = confirm_zone_reversal(df, i, zone_type)
            if not confirmed:
                continue
        else:
            why = "no-confirm-mode"

        if zone_type == "demand":
            entry = cur_close
            stop = z["bottom"] - atr * 0.3
            score = z["score"] + (5 if why.startswith(("lower", "upper")) else 3)
            setups.append({
                "strategy": "Demand_Zone",
                "direction": "BUY",
                "setup_score": round(score, 1),
                "entry_price": entry,
                "stop_loss": stop,
                "zone_type": "demand",
                "zone_top": z["top"],
                "zone_bottom": z["bottom"],
                "confirmation": why,
                "note": f"demand touch@{i-1} + {why} zone[{z['bottom']:.1f}-{z['top']:.1f}]",
                "setup_found": True,
            })
        else:
            entry = cur_close
            stop = z["top"] + atr * 0.3
            score = z["score"] + (5 if why.startswith(("lower", "upper")) else 3)
            setups.append({
                "strategy": "Supply_Zone",
                "direction": "SELL",
                "setup_score": round(score, 1),
                "entry_price": entry,
                "stop_loss": stop,
                "zone_type": "supply",
                "zone_top": z["top"],
                "zone_bottom": z["bottom"],
                "confirmation": why,
                "note": f"supply touch@{i-1} + {why} zone[{z['bottom']:.1f}-{z['top']:.1f}]",
                "setup_found": True,
            })

    return setups


def _simple_range(df: pd.DataFrame, i: int, window: int = 20) -> float:
    """Average bar range over last `window` bars (no stddev, no ATR formula)."""
    if i < window:
        return float(df["high"].iloc[:i + 1].max() - df["low"].iloc[:i + 1].min() or 1.0)
    w = df.iloc[i - window + 1:i + 1]
    return float((w["high"] - w["low"]).mean() or 1.0)


# ============================================================
# V6.5 — 1-minute VOLUME DELTA sniper (Brain 2 sharp entry)
# ============================================================
def volume_delta(bar) -> float:
    """
    Approximate 1-minute volume delta (buy-minus-sell pressure).

    True volume delta needs tick-level bid/ask trade classification (tick
    rule). yfinance 1m OHLCV gives only candle OHLC + volume, so we use the
    standard proxy: split the bar's volume by the fraction of the range
    above/below the open.
      - close > open (bullish)  => positive delta (buy pressure)
      - close < open (bearish)  => negative delta (sell pressure)
      magnitude = volume * |close-open| / range
    This is a well-known proxy used when tick data is unavailable.

    Volume-unavailable fallback: some data sources report ZERO volume for
    instruments that are not directly tradeable (e.g. yfinance returns 0
    volume for ^NSEI/^NSEBANK index tickers — an index, not a listed
    contract). When volume is genuinely absent (v<=0), we fall back to a
    pure price-pressure delta = direction * (|close-open|/range), a 0..1
    body-fraction measure. This preserves the 1.8x spike-ratio test in
    delta_spike_confirms exactly (the ratio is scale-invariant) without
    fabricating volume or weakening the confirmation for assets that DO
    report volume.
    """
    o, c, h, l, v = (float(bar["open"]), float(bar["close"]),
                     float(bar["high"]), float(bar["low"]),
                     float(bar.get("volume", 0) or 0))
    rng = h - l
    if rng <= 0:
        return 0.0
    direction = 1.0 if c >= o else -1.0
    strength = abs(c - o) / rng  # 0..1 body fraction
    if v > 0:
        return direction * v * strength
    # volume genuinely unavailable — price-pressure proxy (scale-invariant)
    return direction * strength


def delta_spike_confirms(df_1m, i, zone_type, lookback: int = 5) -> tuple[bool, float, str]:
    """
    Check if the 1m bar at index i shows a sharp volume-delta spike that
    CONFIRMS institutional buying (demand) or selling (supply).

    A "spike" = current |delta| >= 1.8x the average |delta| of the last
    `lookback` 1m bars AND in the correct direction.

    Returns (confirmed, delta_value, reason).
    """
    if i < lookback + 1:
        return (False, 0.0, "insufficient 1m bars")
    cur_delta = volume_delta(df_1m.iloc[i])
    recent_deltas = [abs(volume_delta(df_1m.iloc[j]))
                     for j in range(i - lookback, i)]
    avg_abs = sum(recent_deltas) / len(recent_deltas) if recent_deltas else 0.0
    if avg_abs <= 0:
        return (False, cur_delta, "no prior volume")
    spike = abs(cur_delta) >= avg_abs * 1.8
    if zone_type == "demand":
        if cur_delta > 0 and spike:
            return (True, cur_delta, f"buy-delta-spike {abs(cur_delta)/avg_abs:.1f}x")
        return (False, cur_delta, "no buy-delta spike")
    # supply
    if cur_delta < 0 and spike:
        return (True, cur_delta, f"sell-delta-spike {abs(cur_delta)/avg_abs:.1f}x")
    return (False, cur_delta, "no sell-delta spike")


def zone_touched_on_1m(bar, zone) -> str | None:
    """
    Did a 1m bar TOUCH a 15m zone? Returns 'demand' / 'supply' / None.

    STRICT 0.3% ENTRY GATE TOLERANCE:
      demand touch: 1m low <= zone top  AND 1m low >= zone bottom * 0.997
      supply touch: 1m high >= zone bottom AND 1m high <= zone top * 1.003

    Tolerance is exactly 0.3% — a wider penetration is a ZONE BREAK,
    not a touch. If price breaks right through the zone edge, REJECT
    the signal instantly (institution has abandoned that level).
    """
    low = float(bar["low"])
    high = float(bar["high"])
    if zone["type"] == "demand":
        # Price must touch zone (low <= zone top) but NOT break through
        # Break-through: low < zone bottom * 0.997 (0.3% below zone)
        if low <= zone["top"] and low >= zone["bottom"] * 0.997:
            return "demand"
        # Break-through rejection — price went through the zone
        return None
    if zone["type"] == "supply":
        # Price must touch zone (high >= zone bottom) but NOT break through
        # Break-through: high > zone top * 1.003 (0.3% above zone)
        if high >= zone["bottom"] and high <= zone["top"] * 1.003:
            return "supply"
        return None
    return None


# ============================================================
# V6.5 — 1m STRUCTURAL EXHAUSTION (Brain 5 trailing exit)
# ============================================================
def one_min_exhaustion(df_1m, i, direction: str, lookback: int = 4) -> tuple[bool, str]:
    """
    Detect structural exhaustion on the 1m chart to exit a trend rider.

    For a BUY (long call) position:
      - exhaustion = a bearish 1m reversal candle with above-average volume
        (institutional distribution) OR 3 consecutive lower highs.
    For a SELL (long put) position:
      - exhaustion = a bullish 1m reversal candle with above-average volume
        OR 3 consecutive higher lows.

    Returns (exhausted, reason).
    """
    if i < lookback + 1:
        return (False, "insufficient 1m bars")
    bars = df_1m.iloc[i - lookback + 1:i + 1]
    avg_vol = float(bars["volume"].mean() or 1)
    cur = df_1m.iloc[i]
    cur_vol = float(cur.get("volume", 0) or 0)
    cur_close, cur_open = float(cur["close"]), float(cur["open"])
    cur_high, cur_low = float(cur["high"]), float(cur["low"])
    cur_rng = max(cur_high - cur_low, 1e-9)
    body_frac = abs(cur_close - cur_open) / cur_rng
    # vol spike: real volume when available; price-pressure proxy (strong body)
    # when the data source reports zero volume (index tickers).
    vol_spike = (cur_vol > avg_vol * 1.3) if avg_vol > 0 else (body_frac >= 0.5)

    if direction == "BUY":
        # bearish reversal candle w/ volume = exhaustion
        bearish = cur_close < cur_open
        # Keep the proxy when volume=0 — don't overwrite with a hard check
        # that always fails for indices (Bug fix: was vol_spike = cur_vol > avg_vol * 1.3)
        if avg_vol > 0:
            vol_spike = cur_vol > avg_vol * 1.3
        # 3 consecutive lower highs
        highs = [float(bars.iloc[j]["high"]) for j in range(len(bars))]
        lower_highs = len(highs) >= 3 and all(highs[k] < highs[k - 1] for k in range(1, len(highs)))
        if bearish and vol_spike:
            return (True, "bearish-reversal-vol")
        if lower_highs:
            return (True, "3-lower-highs")
    else:  # SELL (long put)
        bullish = cur_close > cur_open
        # Keep the proxy when volume=0 — don't overwrite with a hard check
        # that always fails for indices (Bug fix: was vol_spike = cur_vol > avg_vol * 1.3)
        if avg_vol > 0:
            vol_spike = cur_vol > avg_vol * 1.3
        lows = [float(bars.iloc[j]["low"]) for j in range(len(bars))]
        higher_lows = len(lows) >= 3 and all(lows[k] > lows[k - 1] for k in range(1, len(lows)))
        if bullish and vol_spike:
            return (True, "bullish-reversal-vol")
        if higher_lows:
            return (True, "3-higher-lows")
    return (False, "trend-intact")


def find_opposing_zone(df_15m, i_15m, entry_zone_type: str,
                       lookback: int = 40) -> dict | None:
    """
    Find the nearest OPPOSING 15m institutional zone for the trend-rider exit.
      Bought at DEMAND → exit at the nearest SUPPLY zone.
      Bought at SUPPLY → exit at the nearest DEMAND zone.
    Returns the zone dict (with top/bottom) or None.
    """
    zones = detect_zones(df_15m, i_15m, lookback=lookback)
    target_type = "supply" if entry_zone_type == "demand" else "demand"
    opp = [z for z in zones if z["type"] == target_type]
    if not opp:
        return None
    cur_price = float(df_15m.iloc[i_15m]["close"])
    # nearest zone by distance to current price (in the trade direction)
    if entry_zone_type == "demand":
        # going up, pick nearest supply ABOVE current price
        above = [z for z in opp if z["bottom"] > cur_price]
        if above:
            return min(above, key=lambda z: z["bottom"])
        return max(opp, key=lambda z: z["top"])  # fallback closest
    else:
        below = [z for z in opp if z["top"] < cur_price]
        if below:
            return max(below, key=lambda z: z["top"])
        return min(opp, key=lambda z: z["bottom"])
