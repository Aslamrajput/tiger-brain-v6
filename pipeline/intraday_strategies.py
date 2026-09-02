"""
Tiger Brain V6.2 — 5 Parallel Intraday Strategies (Brain 2)
================================================================
Pure intraday options-buying ke liye 5 institutional strategies jo
PARALLEL chalti hain. Har strategy ek 15-min candle pe setup detect
kar sakti hai. Saare setups collect hote hain, score hote hain, aur
top-scoring setups daily 5-10 quota tak execute hote hain.

Strategies:
  a) SMC Order Blocks & Liquidity Sweeps (Stop-Hunt Capture)
  b) Institutional Opening Range Breakout (ORB)
  c) VWAP Deviation & Positive Volume Delta Alignment
  d) Multi-Timeframe EMA Trend Momentum Chaser
  e) Mathematical Relative Strength (RS) Divergence

⚠️ NO LOOKAHEAD — har strategy sirf `df.iloc[:i+1]` (abhi tak ke data)
use karti hai. Intraday VWAP day ki open se rolling ban-ta hai (reset
har trading day).

⚠️ DATA LIMITATION — real intraday OI/delta free mein nahi milta. Ye
strategies price+volume+VWAP+EMA pe based hain, OI velocity abhi
option-selector (Brain 3) ke synthetic chain mein aata hai.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd


# ============================================================
# Helpers — intraday indicators (no lookahead)
# ============================================================
def _intraday_session(df: pd.DataFrame, i: int) -> pd.DataFrame:
    """Return bars of the SAME trading day as bar i (no future bars)."""
    if df.index.tz is None:
        day = df.index[i].date()
        mask = df.index.normalize() == pd.Timestamp(day)
    else:
        day = df.index[i].normalize()
        mask = df.index.normalize() == day
    session = df[mask]
    # Only up to and including bar i
    return session[session.index <= df.index[i]]


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _vwap(session: pd.DataFrame) -> float:
    """VWAP of the session so far (typical price * volume / sum volume)."""
    typical = (session["high"] + session["low"] + session["close"]) / 3.0
    vol = session["volume"].replace(0, np.nan)
    pv = (typical * vol).sum()
    vv = vol.sum()
    if vv == 0 or math.isnan(vv):
        return float(session["close"].iloc[-1])
    return float(pv / vv)


def _atr(df: pd.DataFrame, window: int = 14) -> float:
    """Latest ATR (no lookahead)."""
    if len(df) < window + 1:
        return float(df["close"].diff().abs().mean() or 1.0)
    h, l, c = df["high"], df["low"], df["close"]
    prev_c = c.shift(1)
    tr = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    return float(tr.rolling(window).mean().iloc[-1])


# ============================================================
# Strategy base result
# ============================================================
def _setup(strategy: str, direction: str, score: float, entry: float,
           stop: float, note: str) -> dict:
    return {
        "strategy": strategy,
        "direction": direction,        # "BUY" (CE) or "SELL" (PE)
        "setup_score": round(score, 1),
        "entry_price": round(entry, 2),
        "stop_loss": round(stop, 2),
        "note": note,
        "setup_found": True,
    }


def _no_setup(strategy: str, reason: str) -> dict:
    return {"strategy": strategy, "setup_found": False, "reason": reason,
            "direction": None, "setup_score": 0.0,
            "entry_price": None, "stop_loss": None}


# ============================================================
# Strategy A — SMC Order Blocks & Liquidity Sweeps (Stop-Hunt)
# ============================================================
def smc_sweep(df: pd.DataFrame, i: int, lookback: int = 20) -> dict:
    """
    Liquidity sweep detection: last `lookback` bars ka low/high sweep
    hua, fir reversal candle close hua wapas range ke andar.
    Direction: sweep of lows + bullish reclaim => BUY (CE)
              sweep of highs + bearish reclaim => SELL (PE)
    """
    if i < lookback + 2:
        return _no_setup("SMC", "insufficient bars")
    window = df.iloc[i - lookback:i + 1]
    cur = df.iloc[i]
    prev = df.iloc[i - 1]

    # Liquidity pool = prior lookback low / high (excluding current bar)
    pool_low = float(window["low"].iloc[:-1].min())
    pool_high = float(window["high"].iloc[:-1].max())

    atr = _atr(df.iloc[:i + 1], 14)
    sweep_depth = max(atr * 0.15, 0.0)

    # Bullish sweep: prev wick pierced pool_low, current close reclaimed above
    swept_low = float(prev["low"]) < pool_low - sweep_depth
    reclaimed_up = float(cur["close"]) > pool_low
    bull_body = float(cur["close"]) > float(cur["open"])  # bullish candle

    # Bearish sweep: prev wick pierced pool_high, current close reclaimed below
    swept_high = float(prev["high"]) > pool_high + sweep_depth
    reclaimed_dn = float(cur["close"]) < pool_high
    bear_body = float(cur["close"]) < float(cur["open"])

    if swept_low and reclaimed_up and bull_body:
        entry = float(cur["close"])
        stop = min(pool_low, float(cur["low"])) - atr * 0.1
        score = 60 + min((float(prev["low"]) - pool_low) / max(atr, 1) * 8, 15)
        return _setup("SMC_Sweep", "BUY", score, entry, stop,
                      f"low sweep {pool_low:.2f} -> reclaim bullish")

    if swept_high and reclaimed_dn and bear_body:
        entry = float(cur["close"])
        stop = max(pool_high, float(cur["high"])) + atr * 0.1
        score = 60 + min((float(prev["high"]) - pool_high) / max(atr, 1) * 8, 15)
        return _setup("SMC_Sweep", "SELL", score, entry, stop,
                      f"high sweep {pool_high:.2f} -> reclaim bearish")

    return _no_setup("SMC", "no sweep+reclaim")


# ============================================================
# Strategy B — Institutional Opening Range Breakout (ORB)
# ============================================================
def opening_range_breakout(df: pd.DataFrame, i: int, orb_minutes: int = 30,
                           bar_minutes: int = 15) -> dict:
    """
    First `orb_minutes` (default 30 min = 2 bars of 15m) ka high/low
    ban-ta hai ORB range. Uske baad breakout + volume confirm => trade.
    Sirf morning window mein active (09:15-11:00).
    """
    if i < (orb_minutes // bar_minutes) + 1:
        return _no_setup("ORB", "insufficient ORB bars")
    session = _intraday_session(df, i)
    if len(session) < (orb_minutes // bar_minutes) + 1:
        return _no_setup("ORB", "ORB range not yet formed")

    orb_bars = orb_minutes // bar_minutes
    orb = session.iloc[:orb_bars]
    orb_high = float(orb["high"].max())
    orb_low = float(orb["low"].min())

    cur = df.iloc[i]
    cur_idx = session.index.get_loc(df.index[i])
    if cur_idx < orb_bars:
        return _no_setup("ORB", "still in ORB formation")

    # Current bar must be AFTER the ORB window (not the breakout bar itself
    # counted twice) — check it's a fresh breakout this bar
    prev_close = float(df.iloc[i - 1]["close"])
    cur_close = float(cur["close"])
    avg_vol = float(session["volume"].iloc[:cur_idx].mean() or 1)
    cur_vol = float(cur["volume"])

    # Bullish ORB breakout
    if (prev_close <= orb_high and cur_close > orb_high
            and cur_vol > avg_vol * 1.3):
        entry = cur_close
        stop = orb_low
        score = 62 + min((cur_vol / max(avg_vol, 1) - 1) * 10, 15)
        return _setup("ORB", "BUY", score, entry, stop,
                      f"ORB breakout above {orb_high:.2f} vol {cur_vol/max(avg_vol,1):.1f}x")

    # Bearish ORB breakout
    if (prev_close >= orb_low and cur_close < orb_low
            and cur_vol > avg_vol * 1.3):
        entry = cur_close
        stop = orb_high
        score = 62 + min((cur_vol / max(avg_vol, 1) - 1) * 10, 15)
        return _setup("ORB", "SELL", score, entry, stop,
                      f"ORB breakdown below {orb_low:.2f} vol {cur_vol/max(avg_vol,1):.1f}x")

    return _no_setup("ORB", "no ORB breakout")


# ============================================================
# Strategy C — VWAP Deviation & Positive Volume Delta
# ============================================================
def vwap_deviation(df: pd.DataFrame, i: int, dev_pct: float = 0.4) -> dict:
    """
    Price deviates above/below intraday VWAP by >= dev_pct, with positive
    volume delta (current vol > avg vol) confirming. Mean-reversion or
    momentum-continuation depending on direction.
    BUY: price > VWAP * (1 + dev) with strong volume (trend up, buy CE)
    SELL: price < VWAP * (1 - dev) with strong volume (trend down, buy PE)
    """
    session = _intraday_session(df, i)
    if len(session) < 5:
        return _no_setup("VWAP", "insufficient session bars")
    vwap = _vwap(session)
    cur = df.iloc[i]
    cur_close = float(cur["close"])
    avg_vol = float(session["volume"].iloc[:-1].mean() or 1)
    cur_vol = float(cur["volume"])

    if avg_vol <= 0:
        return _no_setup("VWAP", "no volume baseline")

    dev = (cur_close - vwap) / vwap * 100  # percent deviation
    vol_strong = cur_vol > avg_vol * 1.3
    bull = float(cur["close"]) > float(cur["open"])

    if dev >= dev_pct and vol_strong and bull:
        entry = cur_close
        atr = _atr(df.iloc[:i + 1], 14)
        stop = vwap - atr * 0.5
        score = 58 + min(abs(dev) * 4, 12)
        return _setup("VWAP_Dev", "BUY", score, entry, stop,
                      f"VWAP dev +{dev:.2f}% vol {cur_vol/avg_vol:.1f}x")

    if dev <= -dev_pct and vol_strong and not bull:
        entry = cur_close
        atr = _atr(df.iloc[:i + 1], 14)
        stop = vwap + atr * 0.5
        score = 58 + min(abs(dev) * 4, 12)
        return _setup("VWAP_Dev", "SELL", score, entry, stop,
                      f"VWAP dev {dev:.2f}% vol {cur_vol/avg_vol:.1f}x")

    return _no_setup("VWAP", f"dev {dev:.2f}% below threshold or weak vol")


# ============================================================
# Strategy D — Multi-Timeframe EMA Trend Momentum Chaser
# ============================================================
def ema_momentum(df: pd.DataFrame, i: int) -> dict:
    """
    Fast EMA (8) above Slow EMA (21) above Slower EMA (50) => uptrend.
    Entry on momentum bar (close > prev close, vol spike) in trend dir.
    Intraday so EMAs computed on 15m bars up to bar i (no lookahead).
    """
    if i < 50:
        return _no_setup("EMA_MTF", "insufficient bars for EMA50")
    window = df.iloc[:i + 1]
    c = window["close"]
    ema8 = _ema(c, 8).iloc[-1]
    ema21 = _ema(c, 21).iloc[-1]
    ema50 = _ema(c, 50).iloc[-1]
    cur = df.iloc[i]
    cur_close = float(cur["close"])
    prev_close = float(df.iloc[i - 1]["close"])
    avg_vol = float(window["volume"].iloc[-20:].mean() or 1)
    cur_vol = float(cur["volume"])

    # Bullish trend stack
    if ema8 > ema21 > ema50 and cur_close > prev_close and cur_vol > avg_vol * 1.2:
        atr = _atr(window, 14)
        stop = ema21 - atr * 0.8
        strength = (ema8 - ema50) / ema50 * 100
        score = 56 + min(strength * 3, 14)
        return _setup("EMA_MTF", "BUY", score, cur_close, stop,
                      f"EMA stack up 8>{ema21:.0f}>{ema50:.0f} str {strength:.2f}%")

    # Bearish trend stack
    if ema8 < ema21 < ema50 and cur_close < prev_close and cur_vol > avg_vol * 1.2:
        atr = _atr(window, 14)
        stop = ema21 + atr * 0.8
        strength = (ema50 - ema8) / ema50 * 100
        score = 56 + min(strength * 3, 14)
        return _setup("EMA_MTF", "SELL", score, cur_close, stop,
                      f"EMA stack down 8<{ema21:.0f}<{ema50:.0f} str {strength:.2f}%")

    return _no_setup("EMA_MTF", "no aligned EMA stack + momentum")


# ============================================================
# Strategy E — Mathematical Relative Strength (RS) Divergence
# ============================================================
def rs_divergence(df: pd.DataFrame, i: int, benchmark_df: Optional[pd.DataFrame] = None,
                  lookback: int = 10) -> dict:
    """
    Symbol ka return vs benchmark return over last `lookback` bars.
    Strong outperformance (RS >= 1.0pp) + symbol bullish => BUY (CE).
    Strong underperformance (RS <= -1.0pp) + symbol bearish => SELL (PE).
    """
    if benchmark_df is None or i < lookback + 1:
        return _no_setup("RS_Div", "no benchmark or insufficient bars")
    sym_window = df.iloc[i - lookback:i + 1]
    cur = df.iloc[i]
    cur_time = df.index[i]

    # Benchmark bars up to current time (no lookahead)
    bench_so_far = benchmark_df[benchmark_df.index <= cur_time]
    if len(bench_so_far) < lookback + 1:
        return _no_setup("RS_Div", "insufficient benchmark bars")
    bench_window = bench_so_far.iloc[-lookback - 1:]

    sym_ret = (float(sym_window["close"].iloc[-1]) / float(sym_window["close"].iloc[0]) - 1) * 100
    bench_ret = (float(bench_window["close"].iloc[-1]) / float(bench_window["close"].iloc[0]) - 1) * 100
    rs = sym_ret - bench_ret

    bull = float(cur["close"]) > float(cur["open"])
    bear = float(cur["close"]) < float(cur["open"])

    if rs >= 1.0 and bull:
        atr = _atr(df.iloc[:i + 1], 14)
        entry = float(cur["close"])
        stop = entry - atr * 1.0
        score = 57 + min(abs(rs) * 2, 13)
        return _setup("RS_Div", "BUY", score, entry, stop,
                      f"RS +{rs:.2f}pp (sym {sym_ret:.2f}% vs bench {bench_ret:.2f}%)")

    if rs <= -1.0 and bear:
        atr = _atr(df.iloc[:i + 1], 14)
        entry = float(cur["close"])
        stop = entry + atr * 1.0
        score = 57 + min(abs(rs) * 2, 13)
        return _setup("RS_Div", "SELL", score, entry, stop,
                      f"RS {rs:.2f}pp (sym {sym_ret:.2f}% vs bench {bench_ret:.2f}%)")

    return _no_setup("RS_Div", f"RS {rs:.2f}pp below threshold")


# ============================================================
# Orchestrator — run all 5 strategies in parallel on a bar
# ============================================================
STRATEGIES = {
    "SMC_Sweep": smc_sweep,
    "ORB": opening_range_breakout,
    "VWAP_Dev": vwap_deviation,
    "EMA_MTF": ema_momentum,
    "RS_Div": rs_divergence,
}


def scan_intraday(df: pd.DataFrame, i: int, benchmark_df: Optional[pd.DataFrame] = None) -> list[dict]:
    """
    Run all 5 strategies on bar i. Return list of found setups (empty if none).
    Each setup has strategy, direction, setup_score, entry_price, stop_loss.
    """
    setups = []
    for name, fn in STRATEGIES.items():
        if name == "RS_Div":
            res = fn(df, i, benchmark_df=benchmark_df)
        else:
            res = fn(df, i)
        if res.get("setup_found"):
            setups.append(res)
    return setups
