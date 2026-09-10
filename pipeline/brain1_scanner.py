"""
Tiger Brain V6.1 — BRAIN 1: Market Scanner & Regime Detection
===============================================================
First brain. Decides whether a symbol has noteworthy momentum worth
attention — i.e. this is a NOISE FILTER.

Core rule (user requirement): Brain 1 must IGNORE minor, choppy candle
movements. A setup should only pass to Brain 2 when:

  1. There is high VOLUME VELOCITY (current volume well above average), OR
  2. There is significant RS DIVERGENCE (the symbol is moving differently
     from its benchmark/index).

This sits ON TOP of regime classification (regime/classifier.py) — it
detects regime, but also applies a momentum gate, so the bot catches
major market momentum, not minor retracements.

Wiring: brain1_scanner.scan() → if passed, smart_money_scanner (Brain 2)
"""

from __future__ import annotations

import logging

try:
    from config.thresholds import BRAIN1
    from regime.classifier import classify_regime
except ImportError:
    raise ImportError("Run from repo ROOT, not from inside 'pipeline/'.")

logger = logging.getLogger("tiger_brain.brain1_scanner")


def _atr(df, period: int = 14) -> float:
    """Average True Range (simple average of last `period` TRs)."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = (
        (high - low)
        .combine((high - prev_close).abs(), max)
        .combine((low - prev_close).abs(), max)
    )
    return float(tr.tail(period).mean())


def volume_velocity(df) -> dict:
    """
    Volume velocity = current bar volume vs rolling average volume.
    Returns dict with ratio and whether it clears the gate.
    """
    lookback = BRAIN1["VOLUME_LOOKBACK_BARS"]
    if len(df) < lookback + 1:
        return {"ratio": 0.0, "high_velocity": False, "avg_volume": None,
                "note": f"data has {len(df)} bars — need {lookback + 1}"}

    volumes = df["volume"]
    # Treat zero-volume bars (index spot data) as "data missing"; count only
    # non-zero bars in the average — per the repo's existing policy.
    nonzero = volumes.iloc[:-1][volumes.iloc[:-1] > 0]
    if nonzero.empty:
        return {"ratio": 0.0, "high_velocity": False, "avg_volume": None,
                "note": "all volumes are 0 (index spot) — cannot compute volume velocity"}

    avg_volume = float(nonzero.tail(lookback).mean())
    current = float(volumes.iloc[-1])
    if avg_volume <= 0:
        ratio = 0.0
    else:
        ratio = current / avg_volume

    return {
        "ratio": round(ratio, 2),
        "high_velocity": ratio >= BRAIN1["VOLUME_VELOCITY_MULTIPLIER"],
        "avg_volume": avg_volume,
        "note": None,
    }


def rs_divergence(df, benchmark_df) -> dict:
    """
    RS divergence = the symbol's N-bar return minus the benchmark's N-bar return.
    Positive = symbol outperforming; magnitude matters, sign indicates direction.

    Args:
        df: symbol OHLCV DataFrame
        benchmark_df: index/benchmark OHLCV DataFrame (same timeframe)
    """
    lookback = BRAIN1["RS_LOOKBACK_BARS"]
    if len(df) < lookback + 1 or len(benchmark_df) < lookback + 1:
        return {"divergence_pct": None, "significant": False,
                "note": f"need {lookback + 1} bars in both series"}

    symbol_ret = (float(df["close"].iloc[-1]) / float(df["close"].iloc[-lookback - 1]) - 1) * 100
    bench_ret = (
        float(benchmark_df["close"].iloc[-1]) / float(benchmark_df["close"].iloc[-lookback - 1]) - 1
    ) * 100
    divergence = symbol_ret - bench_ret

    return {
        "divergence_pct": round(divergence, 3),
        "symbol_return_pct": round(symbol_ret, 3),
        "benchmark_return_pct": round(bench_ret, 3),
        "significant": abs(divergence) >= BRAIN1["RS_DIVERGENCE_MIN_PCT"],
        "note": None,
    }


def is_choppy(df) -> dict:
    """
    Chop filter: if the combined range of the last N bars is below an
    ATR multiple, the market is dead — no momentum, skip.
    """
    lookback = BRAIN1["CHOP_LOOKBACK_BARS"]
    if len(df) < lookback + 15:
        return {"choppy": None, "note": "not enough data to compute ATR"}

    atr = _atr(df)
    if atr <= 0:
        return {"choppy": None, "note": "ATR is 0 — filter skipped"}

    recent = df.tail(lookback)
    combined_range = float(recent["high"].max() - recent["low"].min())
    threshold = atr * BRAIN1["CHOP_RANGE_ATR_MULTIPLIER"]

    return {
        "choppy": combined_range < threshold,
        "combined_range": round(combined_range, 2),
        "atr": round(atr, 2),
        "note": None,
    }


def body_to_range_ratio(df) -> float:
    """
    Current bar's body/range ratio — near 1.0 = strong directional
    candle, near 0 = pure doji/chop.
    """
    last = df.iloc[-1]
    body = abs(float(last["close"]) - float(last["open"]))
    rng = float(last["high"]) - float(last["low"])
    if rng <= 0:
        return 0.0
    return body / rng


def scan(df, benchmark_df=None, vix_series=None) -> dict:
    """
    Brain 1's main entry point. Lets a symbol pass through the momentum gate.

    Args:
        df: the symbol's OHLCV DataFrame
        benchmark_df: benchmark (index) OHLCV — for RS divergence
        vix_series: for the regime classifier (optional)

    Returns:
        dict:
            'passed_brain1': bool — whether to send to Brain 2
            'regime': regime classifier output (if computed)
            'momentum': volume-velocity details
            'rs_divergence': RS details (if benchmark provided)
            'chop': chop-filter details
            'body_to_range_ratio': float
            'brain1_notes': list — what was blocking
    """
    notes = []

    if len(df) < max(BRAIN1["VOLUME_LOOKBACK_BARS"], BRAIN1["RS_LOOKBACK_BARS"]) + 5:
        return {
            "passed_brain1": False, "regime": None, "momentum": None,
            "rs_divergence": None, "chop": None, "body_to_range_ratio": None,
            "brain1_notes": [f"insufficient data ({len(df)} bars) — Brain 1 cannot pass"],
        }

    # --- Regime detection (existing classifier reuse) ---
    try:
        regime_result = classify_regime(df, vix_series=vix_series)
    except ValueError as exc:
        regime_result = None
        notes.append(f"regime classify fail: {exc}")

    # --- Chop filter ---
    chop = is_choppy(df)
    if chop["choppy"] is True:
        notes.append(
            f"CHOP FILTER: last {BRAIN1['CHOP_LOOKBACK_BARS']} bars' range "
            f"({chop['combined_range']}) is only {BRAIN1['CHOP_RANGE_ATR_MULTIPLIER']}x "
            f"of ATR ({chop['atr']}) — dead market, skip"
        )

    # --- Candle quality: body-to-range ---
    btr = body_to_range_ratio(df)
    if btr < BRAIN1["MIN_BODY_TO_RANGE_RATIO"]:
        notes.append(
            f"CHOPPY CANDLE: body/range {btr:.2f} < {BRAIN1['MIN_BODY_TO_RANGE_RATIO']} "
            f"— wick-heavy indecision candle, this is noise"
        )

    # --- Momentum gate: volume velocity ---
    momentum = volume_velocity(df)
    if momentum["high_velocity"]:
        notes.append(
            f"VOLUME VELOCITY OK: {momentum['ratio']}x average volume"
        )
    elif momentum["note"]:
        notes.append(f"cannot compute volume velocity: {momentum['note']}")

    # --- Momentum gate: RS divergence ---
    rs = None
    if benchmark_df is not None:
        rs = rs_divergence(df, benchmark_df)
        if rs["significant"]:
            notes.append(
                f"RS DIVERGENCE OK: {rs['divergence_pct']:.2f}pp vs benchmark "
                f"(symbol {rs['symbol_return_pct']}% vs bench {rs['benchmark_return_pct']}%)"
            )
    else:
        notes.append("no benchmark data provided — RS divergence check skipped")

    # --- FINAL GATE ---
    # To pass Brain 1: (high volume velocity) OR (significant RS divergence)
    # — and the chop/last-candle quality must not reject.
    momentum_ok = momentum["high_velocity"] or (rs is not None and rs["significant"])
    not_choppy = chop["choppy"] is not True and btr >= BRAIN1["MIN_BODY_TO_RANGE_RATIO"]

    passed = momentum_ok and not_choppy

    if not momentum_ok:
        notes.append(
            "MOMENTUM GATE FAIL: neither volume velocity is high nor RS divergence "
            "significant — this is a minor retracement, not major momentum. Brain 2 not passed."
        )

    return {
        "passed_brain1": passed,
        "regime": regime_result,
        "momentum": momentum,
        "rs_divergence": rs,
        "chop": chop,
        "body_to_range_ratio": round(btr, 3),
        "brain1_notes": notes,
    }


# ============================================================
# QUICK MANUAL TEST — from repo ROOT: python3 -m pipeline.brain1_scanner
# ============================================================
if __name__ == "__main__":
    import numpy as np
    import pandas as pd

    np.random.seed(7)
    n = 80
    dates = pd.date_range("2025-01-01", periods=n, freq="D")

    def make_df(close, volume):
        d = pd.DataFrame(index=dates)
        d["close"] = close
        d["open"] = d["close"].shift(1).fillna(close[0])
        d["high"] = d[["open", "close"]].max(axis=1) + 0.3
        d["low"] = d[["open", "close"]].min(axis=1) - 0.3
        d["volume"] = volume
        return d

    # Case 1: strong momentum day (trend + volume spike)
    closes = 100 + np.linspace(0, 10, n) + np.random.normal(0, 0.2, n).cumsum() * 0.1
    vols = np.full(n, 200000)
    vols[-1] = 800000
    df1 = make_df(closes, vols)
    bench = make_df(100 + np.linspace(0, 2, n), np.full(n, 300000))

    print("=== Case 1: momentum day ===")
    r1 = scan(df1, benchmark_df=bench)
    print(f"passed_brain1: {r1['passed_brain1']}")
    for note in r1["brain1_notes"]:
        print(f"  - {note}")

    # Case 2: dead chop (flat closes, no volume spike)
    closes2 = 100 + np.random.normal(0, 0.1, n).cumsum() * 0.05
    vols2 = np.full(n, 200000)
    df2 = make_df(closes2, vols2)
    print("\n=== Case 2: chop ===")
    r2 = scan(df2, benchmark_df=bench)
    print(f"passed_brain1: {r2['passed_brain1']}")
    for note in r2["brain1_notes"]:
        print(f"  - {note}")
