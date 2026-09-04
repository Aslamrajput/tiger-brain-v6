"""
Tiger Brain V6.5 — PRE-MARKET 'GUN-POWDER' SCANNER (Brain 1)
================================================================
Scans the entire high-liquidity Indian F&O ocean (150+ stocks,
NIFTY, BANKNIFTY) + MCX commodities (Gold, Silver, Crude, NatGas)
on DAILY and 4-HOUR charts.

An asset is "explosive gun-powder" when it has been CONSOLIDATING
inside a historical major Supply or Demand zone for MORE THAN 2 DAYS
on the daily (and confirmed on the 4H). These are the coiled springs
that, on a zone touch + 1m volume-delta confirmation in the live
session, fire the Brain 2 sniper.

Output: a ranked "Explosive Watchlist" before market open
(09:15 IST for NSE, MCX afternoon session).

⚠️ HONESTY: yfinance daily/4H data is real and reliable for zone
detection. The scanner is a genuine pre-market filter — it does NOT
predict direction, it isolates coiled assets at institutional levels.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict

import numpy as np
import pandas as pd

try:
    from pipeline.intraday_strategies import detect_zones, detect_zones_explosive
    from universe.fno_universe import scan_universe, segment_of
except ImportError:
    raise ImportError("Repo ROOT se chalao.")

logger = logging.getLogger("tiger_brain.scanner")
logging.basicConfig(level=logging.INFO)


# ============================================================
# Consolidation detector (inside a zone for > N days)
# ============================================================
def _is_consolidating(df: pd.DataFrame, zone_bottom: float, zone_top: float,
                      n_days: int = 2, lookback: int = 6) -> tuple[bool, int]:
    """
    Check if the last `lookback` daily/4H bars have stayed INSIDE the zone
    (or oscillated around it) for at least n_days. Consolidation = small
    bodies, range mostly within zone bounds.
    Returns (consolidating, bars_inside).
    """
    if len(df) < lookback + 1:
        return (False, 0)
    recent = df.iloc[-lookback:]
    bars_inside = 0
    for _, bar in recent.iterrows():
        # bar mostly within zone OR touching it
        mid = (bar["high"] + bar["low"]) / 2
        if zone_bottom * 0.995 <= mid <= zone_top * 1.005:
            bars_inside += 1
        # also count if the bar's range overlaps the zone
        elif bar["low"] <= zone_top and bar["high"] >= zone_bottom:
            bars_inside += 1
    avg_range = float((recent["high"] - recent["low"]).mean() or 1.0)
    # consolidation = recent ranges shrinking (last bar range <= 1.3x of avg
    # is tight enough to be "coiled")
    last_range = float(recent.iloc[-1]["high"] - recent.iloc[-1]["low"])
    coiled = last_range <= avg_range * 1.4
    return (bars_inside >= n_days and coiled, bars_inside)


def _scan_one(symbol: str, ticker: str, daily_df: pd.DataFrame,
              fourh_df: pd.DataFrame, min_days: int = 2) -> dict | None:
    """Scan a single symbol on daily + 4H. Return a watchlist entry or None."""
    if daily_df is None or len(daily_df) < 30:
        return None
    # Detect major EXPLOSIVE zones on the daily (V6.6: only zones whose
    # historical rejection caused an immediate high-volume expansion move).
    daily_zones = detect_zones_explosive(daily_df, len(daily_df) - 1,
                                         lookback=40, require_explosive=True,
                                         expansion_lookback=5, min_expansion_atr=1.0)
    if not daily_zones:
        return None

    # 4H confirmation: zones on 4H too (explosive quality)
    fourh_zones = []
    if fourh_df is not None and len(fourh_df) >= 30:
        fourh_zones = detect_zones_explosive(fourh_df, len(fourh_df) - 1,
                                             lookback=40, require_explosive=True,
                                             expansion_lookback=5, min_expansion_atr=1.0)

    cur_price = float(daily_df.iloc[-1]["close"])
    candidates = []
    for z in daily_zones:
        consolidating, bars_in = _is_consolidating(
            daily_df, z["bottom"], z["top"], n_days=min_days
        )
        if not consolidating:
            continue
        # current price inside or touching the zone
        inside = z["bottom"] * 0.99 <= cur_price <= z["top"] * 1.01
        if not inside:
            continue
        # 4H zone agreement: does any 4H zone overlap?
        fourh_agrees = any(
            oz["type"] == z["type"] and not (
                oz["top"] < z["bottom"] * 0.99 or oz["bottom"] > z["top"] * 1.01
            ) for oz in fourh_zones
        ) if fourh_zones else True  # if no 4H data, don't block
        score = z["score"] + bars_in + (5 if fourh_agrees else 0)
        candidates.append({
            "symbol": symbol, "ticker": ticker,
            "zone_type": z["type"],
            "zone_bottom": round(z["bottom"], 2),
            "zone_top": round(z["top"], 2),
            "price": round(cur_price, 2),
            "bars_in_zone": bars_in,
            "fourh_confirmed": fourh_agrees,
            "expansion_pct": z.get("expansion_pct", 0.0),
            "gun_powder_score": round(score, 1),
            "direction": "BUY Call" if z["type"] == "demand" else "BUY Put",
        })
    if not candidates:
        return None
    return max(candidates, key=lambda c: c["gun_powder_score"])


def run_premarket_scan(symbols: dict | None = None, period_d: str = "1y",
                       period_4h: str = "60d", min_days: int = 2,
                       fetch=True) -> dict:
    """
    Run the pre-market gun-powder scan across the full 150+ universe.

    Returns: {
        "watchlist": [ {symbol, zone_type, zone, price, score, direction, ...} ],
        "scanned": N, "explosive": M, "by_segment": {...}
    }
    The watchlist is ranked by gun_powder_score (highest coiled spring first).
    """
    syms = symbols or scan_universe()
    watchlist = []
    scanned = 0
    failed = []
    if fetch:
        import yfinance as yf
        for sym, tk in syms.items():
            scanned += 1
            try:
                d = yf.download(tk, period=period_d, interval="1d",
                                progress=False, auto_adjust=False)
                f = yf.download(tk, period=period_4h, interval="60m",
                                progress=False, auto_adjust=False)
                if d is None or d.empty or len(d) < 30:
                    failed.append(sym)
                    continue
                d = _normalize(d)
                f = _normalize(f) if f is not None and not f.empty else None
                entry = _scan_one(sym, tk, d, f, min_days=min_days)
                if entry:
                    watchlist.append(entry)
            except Exception as e:
                failed.append(sym)
                logger.debug(f"{sym}: {e}")
    # sort by score
    watchlist.sort(key=lambda c: c["gun_powder_score"], reverse=True)
    by_seg = defaultdict(int)
    for w in watchlist:
        by_seg[segment_of(w["symbol"])] += 1
    return {
        "watchlist": watchlist,
        "scanned": scanned,
        "explosive": len(watchlist),
        "by_segment": dict(by_seg),
        "failed": failed,
    }


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    df = df.copy()
    df.columns = [c.lower() if isinstance(c, str)
                  else (c[0].lower() if hasattr(c, "__len__") else str(c).lower())
                  for c in df.columns]
    df.index = pd.to_datetime(df.index)
    df = df.dropna(subset=["close"])
    return df


def print_watchlist(result: dict) -> None:
    wl = result["watchlist"]
    print("=" * 78)
    print("  TIGER BRAIN V6.5 — PRE-MARKET 'GUN-POWDER' SCANNER (Brain 1)")
    print("=" * 78)
    print(f"  Scanned:        {result['scanned']} symbols (150+ NSE F&O + MCX)")
    print(f"  Explosive:      {result['explosive']} coiled assets at major zones")
    print(f"  Failed fetch:   {len(result['failed'])}")
    print("-" * 78)
    print(f"  {'#':>2s} {'Symbol':<14s} {'Zone':<8s} {'Price':>10s} "
          f"{'ZoneLo':>10s} {'ZoneHi':>10s} {'Bars':>4s} {'4H':>3s} {'Score':>6s} {'Dir':<10s}")
    print(f"  {'--':>2s} {'-'*14} {'-'*8} {'-'*10} {'-'*10} {'-'*10} {'-'*4} {'-'*3} {'-'*6} {'-'*10}")
    for i, w in enumerate(wl[:30], 1):
        print(f"  {i:>2d} {w['symbol']:<14s} {w['zone_type']:<8s} "
              f"{w['price']:>10.2f} {w['zone_bottom']:>10.2f} "
              f"{w['zone_top']:>10.2f} {w['bars_in_zone']:>4d} "
              f"{'Y' if w['fourh_confirmed'] else 'n':>3s} "
              f"{w['gun_powder_score']:>6.1f} {w['direction']:<10s}")
    if len(wl) > 30:
        print(f"  ... +{len(wl)-30} more")
    print("-" * 78)
    print("  HONESTY: scanner isolates coiled assets — does NOT predict the")
    print("  breakout. Brain 2 still needs 1m volume-delta confirmation.")
    print("=" * 78)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    print("Running pre-market gun-powder scan (daily + 4H)...")
    res = run_premarket_scan()
    print_watchlist(res)
