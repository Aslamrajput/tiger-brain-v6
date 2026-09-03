"""Tests for the pure intraday 5-Brain backtest engine (intraday_backtest.py)."""

from __future__ import annotations

import math
from collections import Counter

import numpy as np
import pandas as pd
import pytest

from backtest.intraday_backtest import (
    brain1_intraday_pass,
    atm_premium,
    check_intraday_exit,
    realized_vol_simple,
    run_intraday_backtest,
    size_with_hard_stop,
    _in_entry_window,
    _is_square_off_bar,
)


def _intraday_df(start=22000, n_bars=150, seed=1, vol=0.004, tz="Asia/Kolkata"):
    """Synthetic 15-min OHLCV spanning several trading days (25 bars/day)."""
    np.random.seed(seed)
    dates = pd.date_range("2026-06-01 09:15", periods=n_bars * 3, freq="15min", tz=tz)
    # Keep only IST session bars (09:15-15:30)
    dates = pd.DatetimeIndex([d for d in dates if 9 <= d.hour <= 15][:n_bars])
    c = start * (1 + np.linspace(0, 0.02, len(dates)) + np.random.normal(0, vol, len(dates)).cumsum())
    d = pd.DataFrame(index=dates)
    d["close"] = c
    d["open"] = np.r_[c[0], c[:-1]]
    d["high"] = np.maximum(d["open"], d["close"]) + start * 0.001
    d["low"] = np.minimum(d["open"], d["close"]) - start * 0.001
    d["volume"] = np.random.randint(50000, 200000, len(dates))
    return d


def test_atm_premium_positive_for_otm_and_itm():
    # ATM call premium positive (intrinsic ~0, time value positive)
    p = atm_premium(22000, 22000, 1.0, is_call=True, iv_pct=0.20)
    assert p > 0
    # ITM call has intrinsic value
    p_itm = atm_premium(22100, 22000, 1.0, is_call=True, iv_pct=0.20)
    assert p_itm > p  # ITM > ATM
    # OTM call has less premium than ATM
    p_otm = atm_premium(21900, 22000, 1.0, is_call=True, iv_pct=0.20)
    assert p_otm < p


def test_in_entry_window_morning_and_afternoon():
    assert _in_entry_window(pd.Timestamp("2026-08-27 09:45", tz="Asia/Kolkata"))
    assert _in_entry_window(pd.Timestamp("2026-08-27 14:00", tz="Asia/Kolkata"))
    assert not _in_entry_window(pd.Timestamp("2026-08-27 12:00", tz="Asia/Kolkata"))


def test_is_square_off_bar():
    assert _is_square_off_bar(pd.Timestamp("2026-08-27 15:15", tz="Asia/Kolkata"))
    assert not _is_square_off_bar(pd.Timestamp("2026-08-27 14:00", tz="Asia/Kolkata"))


def test_size_with_hard_stop_respects_2000_cap():
    s = size_with_hard_stop(entry_premium=100, stop_premium=80, lot_sz=75, max_loss=2000)
    assert s["max_loss"] <= 2000
    assert s["quantity"] == 75
    s2 = size_with_hard_stop(entry_premium=100, stop_premium=95, lot_sz=75, max_loss=2000)
    assert s2["max_loss"] <= 2000
    assert s2["lots"] == 5


def test_check_intraday_exit_square_off_priority():
    pos = {"entry_premium": 100, "stop_premium": 70, "direction": "BUY",
           "zone_edge": 21900}
    ex = check_intraday_exit(pos, cur_underlying=22000, cur_premium=50,
                             is_square_off_bar=True)
    assert ex["exit"] and ex["reason"] == "square_off_1515"


def test_check_intraday_exit_hard_stop():
    pos = {"entry_premium": 100, "stop_premium": 70, "direction": "BUY",
           "zone_edge": 21900}
    ex = check_intraday_exit(pos, cur_underlying=21900, cur_premium=65,
                             is_square_off_bar=False)
    assert ex["exit"] and ex["reason"] == "stop_loss_2000"


def test_check_intraday_exit_target():
    pos = {"entry_premium": 100, "stop_premium": 70, "direction": "BUY",
           "zone_edge": 21900}
    ex = check_intraday_exit(pos, cur_underlying=22100, cur_premium=141,
                             is_square_off_bar=False)
    assert ex["exit"] and ex["reason"] == "target_40pct"


def test_check_intraday_exit_zone_break():
    pos = {"entry_premium": 100, "stop_premium": 50, "direction": "BUY",
           "zone_edge": 21900}
    # underlying breaches the zone edge but premium not yet at hard stop
    ex = check_intraday_exit(pos, cur_underlying=21899, cur_premium=80,
                             is_square_off_bar=False)
    assert ex["exit"] and ex["reason"] == "zone_break"


def test_brain1_intraday_rejects_outside_window():
    df = _intraday_df()
    noon_idx = [i for i, ts in enumerate(df.index) if ts.hour == 12][:1]
    if noon_idx:
        r = brain1_intraday_pass(df, noon_idx[0])
        assert not r["passed_brain1"]


def test_run_intraday_backtest_returns_required_keys():
    df = _intraday_df(start=22000, n_bars=150, seed=3)
    res = run_intraday_backtest({"SBIN": df}, benchmark_df=None,
                                start_capital=150000.0, max_loss_per_trade=2000.0)
    assert "totals" in res and "segment_stats" in res
    assert "trades" in res and "equity_curve" in res
    assert "strategy_stats" in res and "daily_seg_counts" in res


def test_hard_stop_never_exceeds_2000_plus_costs():
    df = _intraday_df(start=22000, n_bars=150, seed=3)
    res = run_intraday_backtest({"SBIN": df}, benchmark_df=None,
                                start_capital=150000.0, max_loss_per_trade=2000.0)
    for tr in res["trades"]:
        assert tr["pnl"] >= -2200, f"Hard stop violated: trade lost ₹{tr['pnl']}"


def test_no_overnight_carry():
    df = _intraday_df(start=22000, n_bars=150, seed=3)
    res = run_intraday_backtest({"SBIN": df}, benchmark_df=None,
                                start_capital=150000.0, max_loss_per_trade=2000.0)
    for tr in res["trades"]:
        assert "exit_ts" in tr and tr["exit_reason"] != "open"


def test_daily_quota_never_exceeds_cap():
    df = _intraday_df(start=22000, n_bars=150, seed=3)
    res = run_intraday_backtest({"SBIN": df}, benchmark_df=None,
                                start_capital=150000.0, max_loss_per_trade=2000.0)
    by_day = Counter()
    for tr in res["trades"]:
        d = tr["entry_ts"].date() if hasattr(tr["entry_ts"], "date") else tr["entry_ts"]
        by_day[d] += 1
    for day, cnt in by_day.items():
        assert cnt <= 10, f"Daily cap violated: {cnt} trades on {day}"
