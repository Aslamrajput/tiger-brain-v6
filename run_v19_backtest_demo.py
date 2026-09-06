"""V19 backtest on synthetic trending data — generates results for GitHub.

Builds multi-symbol synthetic 15m + 1m OHLCV data with strong trends and
zone touches so the Tiger Brain entry engine produces trades, then runs the
full run_tiger_brain_backtest and prints the report.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from backtest.run_tiger_brain_backtest import run_tiger_brain_backtest
from backtest.intraday_backtest import print_report


def _build_15m(start, n_days, seed, drift_pct, vol_pct, tz="Asia/Kolkata"):
    """Build 15m OHLCV spanning n_days of trading sessions (25 bars/day).

    Injects alternating trend legs so supply/demand zones form and get touched.
    """
    rng = np.random.default_rng(seed)
    bars_per_day = 25
    total_bars = n_days * bars_per_day
    base_dates = pd.date_range("2026-06-01 09:15", periods=total_bars * 3,
                               freq="15min", tz=tz)
    dates = pd.DatetimeIndex([d for d in base_dates if 9 <= d.hour <= 15][:total_bars])
    total_bars = len(dates)  # actual bars available

    # alternating drift legs: up-down-up so zones get retested
    leg = total_bars // 4
    drifts = []
    for i in range(4):
        sign = 1 if i % 2 == 0 else -1
        drifts.extend([sign * start * drift_pct / max(leg, 1)] * leg)
    drifts = np.array(drifts[:total_bars])
    if len(drifts) < total_bars:
        drifts = np.r_[drifts, np.zeros(total_bars - len(drifts))]

    noise = rng.normal(0, start * vol_pct, total_bars).cumsum()
    c = start + drifts.cumsum() + noise
    c = np.maximum(c, start * 0.5)

    d = pd.DataFrame(index=dates)
    d["close"] = c
    d["open"] = np.r_[c[0], c[:-1]]
    d["high"] = np.maximum(d["open"], d["close"]) + start * 0.002
    d["low"] = np.minimum(d["open"], d["close"]) - start * 0.002
    d["volume"] = rng.integers(100000, 500000, total_bars)
    return d


def _build_1m_from_15m(df_15m, seed, vol_pct=0.0008):
    """Build 1m OHLCV aligned to 15m bars (15 1m bars per 15m bar)."""
    rng = np.random.default_rng(seed)
    rows = []
    for ts_15, bar in df_15m.iterrows():
        for j in range(15):
            ts_1m = ts_15 + pd.Timedelta(minutes=j)
            o = float(bar["open"]) if j == 0 else rows[-1]["close"]
            c = o * (1 + rng.normal(0, vol_pct))
            h = max(o, c) * (1 + abs(rng.normal(0, vol_pct * 0.4)))
            lo = min(o, c) * (1 - abs(rng.normal(0, vol_pct * 0.4)))
            rows.append({"open": o, "high": h, "low": lo, "close": c,
                         "volume": int(rng.integers(2000, 40000))})
    idx = [ts_15 + pd.Timedelta(minutes=j)
           for ts_15 in df_15m.index for j in range(15)]
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


def main():
    np.random.seed(42)
    # Multi-symbol universe: index + stocks + commodity
    specs = {
        "NIFTY": dict(start=22000, n_days=20, seed=11, drift_pct=0.06, vol_pct=0.003),
        "BANKNIFTY": dict(start=48000, n_days=20, seed=12, drift_pct=0.05, vol_pct=0.003),
        "RELIANCE": dict(start=2900, n_days=20, seed=13, drift_pct=0.07, vol_pct=0.004),
        "SBIN": dict(start=820, n_days=20, seed=14, drift_pct=0.08, vol_pct=0.004),
        "HDFCBANK": dict(start=1650, n_days=20, seed=15, drift_pct=0.06, vol_pct=0.004),
        "CRUDEOIL": dict(start=6500, n_days=20, seed=16, drift_pct=0.09, vol_pct=0.005),
        "GOLD": dict(start=72000, n_days=20, seed=17, drift_pct=0.05, vol_pct=0.004),
    }

    data_map = {}
    data_map_1m = {}
    for sym, s in specs.items():
        df15 = _build_15m(s["start"], s["n_days"], s["seed"],
                          s["drift_pct"], s["vol_pct"])
        df1m = _build_1m_from_15m(df15, s["seed"] + 100)
        data_map[sym] = df15
        data_map_1m[sym] = df1m

    print("\n" + "#" * 72)
    print("#  TIGER BRAIN V19 — BACKTEST (SYNTHETIC TRENDING DATA)")
    print(f"#  Symbols: {list(data_map.keys())}")
    print("#  This demonstrates V19 exit engine + smart square-off behavior.")
    print("#" * 72)

    combined = run_tiger_brain_backtest(
        data_map, start_capital=150000.0,
        data_map_1m=data_map_1m, broker=None)

    print("\n\n")
    print("#" * 72)
    print("#  TIGER BRAIN V19 — COMBINED PORTFOLIO RESULT")
    print("#" * 72)
    print_report(combined)

    # Exit reason breakdown (shows smart square-off in action)
    from collections import Counter
    reasons = Counter(t.get("exit_reason", "?") for t in combined["trades"])
    print("\n" + "-" * 72)
    print("  EXIT REASON BREAKDOWN (V19 smart square-off)")
    print("-" * 72)
    for r, cnt in reasons.most_common():
        print(f"  {r:<35s} {cnt:>5d} trades")
    print("-" * 72)

    return combined


if __name__ == "__main__":
    main()
