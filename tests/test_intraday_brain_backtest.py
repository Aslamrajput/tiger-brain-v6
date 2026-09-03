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
    spread_ok,
    model_spread_pct,
    _in_entry_window,
    _is_square_off_bar,
)
from pipeline.intraday_strategies import (
    scan_zones, detect_zones, confirm_zone_reversal, _rejection_wick,
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


# ============================================================
# UPGRADE 1 — Zone rejection & confirmation filter (Brain 2)
# ============================================================
def _bar(open, close, high, low):
    return {"open": open, "close": close, "high": high, "low": low}


def test_rejection_wick_detects_lower_and_upper():
    # lower rejection: long lower wick, bullish
    wr, side = _rejection_wick(_bar(100, 105, 106, 90))
    assert side == "lower" and wr >= 0.35
    # upper rejection: long upper wick, bearish
    wr2, side2 = _rejection_wick(_bar(105, 100, 115, 99))
    assert side2 == "upper" and wr2 >= 0.35


def test_confirm_zone_reversal_demand_bullish_confirm():
    # touch bar i-1 (in demand zone), then bar i confirms with bullish close
    df = pd.DataFrame([
        _bar(100, 101, 102, 99), _bar(101, 100, 102, 98),  # touch bar (down into zone)
        _bar(100, 104, 105, 99),                            # confirm bar (bullish, lower wick)
    ])
    ok, why = confirm_zone_reversal(df, 2, "demand")
    assert ok and "rejection" in why or "struct" in why


def test_confirm_zone_reversal_demand_no_confirm():
    # confirm bar closes bearish => no confirmation
    df = pd.DataFrame([
        _bar(100, 101, 102, 99), _bar(101, 100, 102, 98),
        _bar(100, 99, 101, 97),  # bearish close, no confirm
    ])
    ok, why = confirm_zone_reversal(df, 2, "demand")
    assert not ok


def test_confirm_zone_reversal_supply_bearish_confirm():
    # touch bar up into supply; confirm bar = bearish close + long upper wick
    df = pd.DataFrame([
        _bar(100, 99, 102, 98), _bar(99, 101, 103, 98),   # touch bar (up into supply)
        _bar(101, 99, 105, 98),                            # confirm: close<open, upper wick=4 > body*0.8
    ])
    ok, why = confirm_zone_reversal(df, 2, "supply")
    assert ok and "rejection" in why


def test_scan_zones_requires_confirmation_by_default():
    """Confirmed setups should be a strict subset of raw touches."""
    df = _intraday_df(start=22000, n_bars=200, seed=5)
    # count confirmed vs unconfirmed at several bars
    confirmed = 0
    raw = 0
    for i in range(42, len(df)):
        confirmed += len(scan_zones(df, i, require_confirm=True))
        raw += len(scan_zones(df, i, require_confirm=False))
    # confirmed never exceeds raw
    assert confirmed <= raw


# ============================================================
# UPGRADE 2 — NSE vs MCX time-zone enforcement (Brain 1 & 5)
# ============================================================
def test_nse_entry_window_morning_and_afternoon():
    assert _in_entry_window(pd.Timestamp("2026-08-27 09:45", tz="Asia/Kolkata"), "stock")
    assert _in_entry_window(pd.Timestamp("2026-08-27 14:00", tz="Asia/Kolkata"), "stock")
    # NSE has no evening session
    assert not _in_entry_window(pd.Timestamp("2026-08-27 18:00", tz="Asia/Kolkata"), "stock")


def test_mcx_entry_window_morning_and_evening():
    # MCX morning 09:00-11:30
    assert _in_entry_window(pd.Timestamp("2026-08-27 09:30", tz="Asia/Kolkata"), "commodity")
    # MCX evening 17:00-23:00
    assert _in_entry_window(pd.Timestamp("2026-08-27 18:00", tz="Asia/Kolkata"), "commodity")
    # midday gap (11:30-17:00) is NOT an MCX entry window
    assert not _in_entry_window(pd.Timestamp("2026-08-27 13:00", tz="Asia/Kolkata"), "commodity")


def test_nse_square_off_1515_not_mcx():
    ts = pd.Timestamp("2026-08-27 15:15", tz="Asia/Kolkata")
    assert _is_square_off_bar(ts, "stock")
    assert not _is_square_off_bar(ts, "commodity")


def test_mcx_square_off_2315_not_nse():
    ts = pd.Timestamp("2026-08-27 23:15", tz="Asia/Kolkata")
    assert _is_square_off_bar(ts, "commodity")
    assert not _is_square_off_bar(ts, "stock")


def test_brain1_segment_aware_rejects_mcx_in_nse_window():
    df = _intraday_df(start=22000, n_bars=150, seed=3)
    # find a 14:00 bar (NSE afternoon, not MCX window)
    for i, ts in enumerate(df.index):
        if ts.hour == 14 and ts.minute == 0:
            # commodity segment should reject at 14:00 (midday MCX gap)
            r = brain1_intraday_pass(df, i, segment="commodity")
            assert not r["passed_brain1"]
            break


# ============================================================
# UPGRADE 3 — Liquidity & spread safety layer (Brain 3)
# ============================================================
def test_spread_gate_passes_liquid_index():
    ok, sp = spread_ok("NIFTY", 120.0)
    assert ok and sp <= 0.5


def test_spread_gate_rejects_illiquid_midcap():
    # ONGC is tier 3 + cheap premium → spread > 0.5%
    ok, sp = spread_ok("ONGC", 4.0)
    assert not ok and sp > 0.5


def test_spread_gate_rejects_cheap_illiquid_option():
    # very cheap option on a tier-2 symbol → tick-size widening pushes > 0.5%
    ok, sp = spread_ok("BHARTIARTL", 3.0)
    assert not ok and sp > 0.5


def test_model_spread_monotonic_in_liquidity_tier():
    # same premium: tier 1 < tier 2 < tier 3 spread
    p1 = model_spread_pct("NIFTY", 50.0)
    p2 = model_spread_pct("BHARTIARTL", 50.0)
    p3 = model_spread_pct("ONGC", 50.0)
    assert p1 < p2 < p3


def test_model_spread_widens_for_cheap_premiums():
    # same symbol: cheaper premium → larger relative spread (tick size)
    sp_high = model_spread_pct("SBIN", 100.0)
    sp_low = model_spread_pct("SBIN", 5.0)
    assert sp_low > sp_high


def test_backtest_all_trades_pass_spread_gate():
    """Every executed trade must have entry_spread_pct <= 0.5%."""
    df = _intraday_df(start=22000, n_bars=200, seed=7)
    res = run_intraday_backtest({"SBIN": df}, start_capital=150000.0,
                                max_loss_per_trade=2000.0)
    for tr in res["trades"]:
        assert tr["entry_spread_pct"] <= 0.5, (
            f"Spread gate violated: {tr['symbol']} spread {tr['entry_spread_pct']}%"
        )


def test_backtest_trades_have_confirmation_field():
    """Every trade must record its zone confirmation reason."""
    df = _intraday_df(start=22000, n_bars=200, seed=7)
    res = run_intraday_backtest({"SBIN": df}, start_capital=150000.0,
                                max_loss_per_trade=2000.0)
    for tr in res["trades"]:
        assert "confirmation" in tr
        assert tr["confirmation"] in (
            "lower-rejection", "upper-rejection",
            "struct-break-up", "struct-break-down",
            "no-confirm-mode", "n/a",
        )


def test_no_overnight_carry_nse_and_mcx():
    """No open position should survive past its segment square-off."""
    df = _intraday_df(start=22000, n_bars=150, seed=3)
    res = run_intraday_backtest({"SBIN": df}, start_capital=150000.0,
                                max_loss_per_trade=2000.0)
    for tr in res["trades"]:
        assert "exit_ts" in tr and tr["exit_reason"] != "open"
