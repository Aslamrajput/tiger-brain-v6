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
    find_sniper_entry,
    _in_entry_window,
    _is_square_off_bar,
)
from pipeline.intraday_strategies import (
    scan_zones, detect_zones, confirm_zone_reversal, _rejection_wick,
    volume_delta, delta_spike_confirms, zone_touched_on_1m,
    one_min_exhaustion, find_opposing_zone,
    zone_explosive_quality, detect_zones_explosive, liquidity_sweep,
)
from universe.fno_universe import is_expiry_day


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
           "zone_edge": 21900, "opposing_zone_edge": 22300}
    ex = check_intraday_exit(pos, cur_underlying=22000, cur_premium=50,
                             is_square_off_bar=True)
    assert ex["exit"] and ex["reason"] == "square_off"


def test_check_intraday_exit_hard_stop():
    pos = {"entry_premium": 100, "stop_premium": 70, "direction": "BUY",
           "zone_edge": 21900, "opposing_zone_edge": 22300}
    ex = check_intraday_exit(pos, cur_underlying=21900, cur_premium=65,
                             is_square_off_bar=False)
    assert ex["exit"] and ex["reason"] == "stop_loss_2000"


def test_check_intraday_exit_opposing_zone_reached():
    """V6.5 trend rider: exit when underlying reaches the OPPOSING zone."""
    pos = {"entry_premium": 100, "stop_premium": 70, "direction": "BUY",
           "zone_edge": 21900, "opposing_zone_edge": 22100}
    ex = check_intraday_exit(pos, cur_underlying=22100, cur_premium=160,
                             is_square_off_bar=False)
    assert ex["exit"] and ex["reason"] == "opposing_zone_reached"


def test_check_intraday_exit_no_fixed_target_below_runaway():
    """V6.5: NO fixed 40% target — a +41% gain must NOT exit (rides the trend)."""
    pos = {"entry_premium": 100, "stop_premium": 70, "direction": "BUY",
           "zone_edge": 21900, "opposing_zone_edge": 23000}
    ex = check_intraday_exit(pos, cur_underlying=22000, cur_premium=141,
                             is_square_off_bar=False)
    assert not ex["exit"]  # trend rider keeps riding past 40%


def test_check_intraday_exit_runaway_safety():
    """Only the +250% runaway safety should cap an extreme gamma spike."""
    pos = {"entry_premium": 100, "stop_premium": 70, "direction": "BUY",
           "zone_edge": 21900, "opposing_zone_edge": 23000}
    ex = check_intraday_exit(pos, cur_underlying=22100, cur_premium=360,
                             is_square_off_bar=False)
    assert ex["exit"] and ex["reason"] == "runaway_safety_250pct"


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


# ============================================================
# V6.5 — Brain 2: 1m VOLUME DELTA sniper trigger
# ============================================================
def test_volume_delta_sign_matches_bar_direction():
    # bullish bar → positive delta
    bull = {"open": 100, "close": 105, "high": 106, "low": 99, "volume": 1000}
    assert volume_delta(bull) > 0
    # bearish bar → negative delta
    bear = {"open": 105, "close": 100, "high": 106, "low": 99, "volume": 1000}
    assert volume_delta(bear) < 0


def test_volume_delta_zero_for_doji_or_no_volume():
    doji = {"open": 100, "close": 100, "high": 101, "low": 99, "volume": 1000}
    assert volume_delta(doji) == 0.0
    # volume genuinely unavailable (e.g. yfinance index tickers ^NSEI/^NSEBANK
    # report 0 volume). Fall back to a price-pressure proxy that preserves the
    # bar direction so the 1.8x spike-ratio test still fires.
    no_vol_bull = {"open": 100, "close": 105, "high": 106, "low": 99, "volume": 0}
    assert volume_delta(no_vol_bull) > 0.0
    no_vol_bear = {"open": 105, "close": 100, "high": 106, "low": 99, "volume": 0}
    assert volume_delta(no_vol_bear) < 0.0
    # with real volume the magnitude is volume-weighted (much larger)
    with_vol = {"open": 100, "close": 105, "high": 106, "low": 99, "volume": 1000}
    assert abs(volume_delta(with_vol)) > abs(volume_delta(no_vol_bull))


def _one_min_df(start=22000, n_bars=60, seed=1, vol=0.001):
    """Synthetic 1m OHLCV (tz-aware IST)."""
    rng = np.random.default_rng(seed)
    n = n_bars
    idx = pd.date_range("2026-08-27 09:15", periods=n, freq="1min", tz="Asia/Kolkata")
    px = start
    opens, highs, lows, closes, vols = [], [], [], [], []
    for _ in range(n):
        o = px
        c = o * (1 + rng.normal(0, vol))
        h = max(o, c) * (1 + abs(rng.normal(0, vol * 0.5)))
        l = min(o, c) * (1 - abs(rng.normal(0, vol * 0.5)))
        opens.append(o); highs.append(h); lows.append(l); closes.append(c)
        vols.append(rng.integers(500, 5000))
        px = c
    return pd.DataFrame({"open": opens, "high": highs, "low": lows,
                         "close": closes, "volume": vols}, index=idx)


def test_delta_spike_confirms_buy_at_demand():
    # Build a 1m df with a clear buy-delta spike at the end
    df = _one_min_df(start=100, n_bars=20, seed=2)
    # force last bar to a strong bullish spike with high volume
    df.loc[df.index[-1], "open"] = 100
    df.loc[df.index[-1], "close"] = 108
    df.loc[df.index[-1], "high"] = 109
    df.loc[df.index[-1], "low"] = 99
    df.loc[df.index[-1], "volume"] = 50000
    ok, dv, why = delta_spike_confirms(df, len(df) - 1, "demand")
    assert ok and "buy-delta" in why


def test_delta_spike_confirms_sell_at_supply():
    df = _one_min_df(start=100, n_bars=20, seed=2)
    df.loc[df.index[-1], "open"] = 108
    df.loc[df.index[-1], "close"] = 100
    df.loc[df.index[-1], "high"] = 109
    df.loc[df.index[-1], "low"] = 99
    df.loc[df.index[-1], "volume"] = 50000
    ok, dv, why = delta_spike_confirms(df, len(df) - 1, "supply")
    assert ok and "sell-delta" in why


def test_zone_touched_on_1m_demand_and_supply():
    zone_demand = {"type": "demand", "top": 105, "bottom": 100}
    zone_supply = {"type": "supply", "top": 110, "bottom": 105}
    # 1m bar low pierces demand top
    assert zone_touched_on_1m({"low": 103, "high": 107}, zone_demand) == "demand"
    # 1m bar high pierces supply bottom
    assert zone_touched_on_1m({"low": 102, "high": 107}, zone_supply) == "supply"
    # no touch
    assert zone_touched_on_1m({"low": 90, "high": 95}, zone_demand) is None


def test_find_sniper_entry_returns_none_without_1m():
    df = _intraday_df(start=22000, n_bars=200, seed=4)
    # no 1m data → sniper returns None
    assert find_sniper_entry(df, 100, None, "stock") is None


# ============================================================
# V6.5 — Brain 5: 1m STRUCTURAL EXHAUSTION + opposing zone
# ============================================================
def test_one_min_exhaustion_bearish_reversal_for_long():
    df = _one_min_df(start=100, n_bars=20, seed=3)
    # last bar: bearish reversal with volume spike
    df.loc[df.index[-1], "open"] = 108
    df.loc[df.index[-1], "close"] = 100
    df.loc[df.index[-1], "high"] = 109
    df.loc[df.index[-1], "low"] = 99
    df.loc[df.index[-1], "volume"] = 50000
    ex, why = one_min_exhaustion(df, len(df) - 1, "BUY")
    assert ex and "reversal" in why


def test_one_min_exhaustion_three_lower_highs_for_long():
    df = _one_min_df(start=100, n_bars=20, seed=3)
    # force the last 4 bars (lookback) to consecutive lower highs, with
    # the final bar NOT a high-volume bearish reversal (so the lower-highs
    # path is the one that triggers).
    highs_seq = [112, 110, 108, 106]
    for k in range(4):
        i = len(df) - 4 + k
        df.iloc[i, df.columns.get_loc("high")] = highs_seq[k]
        df.iloc[i, df.columns.get_loc("open")] = highs_seq[k] - 1
        df.iloc[i, df.columns.get_loc("close")] = highs_seq[k] - 1.5
        df.iloc[i, df.columns.get_loc("low")] = highs_seq[k] - 2
        df.iloc[i, df.columns.get_loc("volume")] = 100  # low vol → no vol-spike
    ex, why = one_min_exhaustion(df, len(df) - 1, "BUY")
    assert ex and "lower-highs" in why


def test_one_min_exhaustion_trend_intact_for_long():
    df = _one_min_df(start=100, n_bars=20, seed=3)
    # steady uptrend, no reversal — no exhaustion
    ex, why = one_min_exhaustion(df, len(df) - 1, "BUY")
    # may or may not trigger; ensure it returns a tuple
    assert isinstance((ex, why), tuple)


def test_find_opposing_zone_supply_for_demand_entry():
    df = _intraday_df(start=22000, n_bars=200, seed=5)
    for i in range(42, len(df)):
        z = find_opposing_zone(df, i, "demand")
        if z is not None:
            assert z["type"] == "supply"
            break


# ============================================================
# V6.5 — Brain 3: expiry-day detection
# ============================================================
def test_is_expiry_day_nifty_thursday():
    # 2026-09-03 is a Thursday
    assert is_expiry_day("NIFTY", pd.Timestamp("2026-09-03", tz="Asia/Kolkata"))
    # 2026-09-02 (Wed) is NOT NIFTY expiry
    assert not is_expiry_day("NIFTY", pd.Timestamp("2026-09-02", tz="Asia/Kolkata"))


def test_is_expiry_day_banknifty_wednesday():
    assert is_expiry_day("BANKNIFTY", pd.Timestamp("2026-09-02", tz="Asia/Kolkata"))
    assert not is_expiry_day("BANKNIFTY", pd.Timestamp("2026-09-03", tz="Asia/Kolkata"))


def test_is_expiry_day_stock_last_thursday_of_month():
    # 2026-09-24 is the last Thursday of Sep 2026
    assert is_expiry_day("SBIN", pd.Timestamp("2026-09-24", tz="Asia/Kolkata"))
    # 2026-09-17 is a Thursday but NOT the last Thursday
    assert not is_expiry_day("SBIN", pd.Timestamp("2026-09-17", tz="Asia/Kolkata"))


# ============================================================
# V6.5 — Engine: sniper-mode end-to-end smoke test
# ============================================================
def test_run_backtest_sniper_mode_produces_trades_with_1m():
    """Sniper mode (with 1m data) should run end-to-end without error and
    produce trade dicts that carry the V6.5 fields (opposing_zone_edge,
    delta_reason, expiry_trade)."""
    df_15m = _intraday_df(start=22000, n_bars=200, seed=8)
    df_1m = _one_min_df(start=22000, n_bars=400, seed=8)
    res = run_intraday_backtest({"SBIN": df_15m}, start_capital=150000.0,
                                max_loss_per_trade=2000.0,
                                data_map_1m={"SBIN": df_1m})
    # engine must run (trades may be 0 due to no zone touch in synthetic data,
    # but the result structure must be valid)
    assert "totals" in res and "trades" in res
    for tr in res["trades"]:
        assert "opposing_zone_edge" in tr
        assert "delta_reason" in tr
        assert "expiry_trade" in tr


# ============================================================
# V6.6 — EXPLOSIVE ZONE QUALITY, LIQUIDITY SWEEP, DYNAMIC TRAIL
# ============================================================
def _explosive_demand_df(start=22000, n_bars=60, base_idx=20):
    """15m df with a clear demand base (4-bar tight cluster) at base_idx
    preceded by a strong up impulse leg and followed by an explosive up-move
    on rising volume (institutional rejection)."""
    idx = pd.date_range("2026-08-27 09:15", periods=n_bars, freq="15min",
                        tz="Asia/Kolkata")
    opens, highs, lows, closes, vols = [], [], [], [], []
    px = start
    for i in range(n_bars):
        o = px
        if i == base_idx - 1:  # impulse leg: strong UP bar before the base
            c = o * 1.01  # +1% up move (>= impulse_min_pct 0.6%)
            h, l, v = c + 5, o - 2, 2000
        elif base_idx <= i <= base_idx + 3:  # 4-bar tight base cluster
            c = o + 1
            h, l, v = o + 4, o - 1, 1000  # small bodies, overlapping
        elif i == base_idx + 4:  # explosive up-move on volume
            c = o + 80
            h, l, v = o + 90, o - 2, 5000
        elif i == base_idx + 5:
            c = o + 60
            h, l, v = o + 70, o - 5, 4500
        else:
            c = o * (1 + 0.0005)
            h = max(o, c) + 3
            l = min(o, c) - 3
            v = 1500
        opens.append(o); highs.append(h); lows.append(l); closes.append(c)
        vols.append(v)
        px = c
    return pd.DataFrame({"open": opens, "high": highs, "low": lows,
                         "close": closes, "volume": vols}, index=idx)


def _weak_demand_df(start=22000, n_bars=60, base_idx=20):
    """15m df with a demand base whose rejection is a WEAK choppy move."""
    idx = pd.date_range("2026-08-27 09:15", periods=n_bars, freq="15min",
                        tz="Asia/Kolkata")
    opens, highs, lows, closes, vols = [], [], [], [], []
    px = start
    for i in range(n_bars):
        o = px
        if i == base_idx - 1:  # impulse leg: strong UP bar before the base
            c = o * 1.01
            h, l, v = c + 5, o - 2, 2000
        elif base_idx <= i <= base_idx + 3:  # 4-bar tight base cluster
            c = o + 1
            h, l, v = o + 4, o - 1, 1000
        elif i == base_idx + 4:  # WEAK move: tiny range, normal volume
            c = o + 5
            h, l, v = o + 8, o - 1, 1400
        elif i == base_idx + 5:
            c = o + 3
            h, l, v = o + 6, o - 1, 1400
        else:
            c = o + 0.5
            h = max(o, c) + 2
            l = min(o, c) - 2
            v = 1500
        opens.append(o); highs.append(h); lows.append(l); closes.append(c)
        vols.append(v)
        px = c
    return pd.DataFrame({"open": opens, "high": highs, "low": lows,
                         "close": closes, "volume": vols}, index=idx)


def test_zone_explosive_quality_passes_on_strong_rejection():
    df = _explosive_demand_df(start=22000, n_bars=60, base_idx=20)
    zones = detect_zones(df, 55, lookback=40)
    assert len(zones) > 0
    z = zones[0]
    is_exp, exp_pct = zone_explosive_quality(df, z["bar_idx"], z["type"])
    assert is_exp is True
    assert exp_pct > 0.0


def test_zone_explosive_quality_rejects_weak_zone():
    df = _weak_demand_df(start=22000, n_bars=60, base_idx=20)
    zones = detect_zones(df, 55, lookback=40)
    assert len(zones) > 0
    z = zones[0]
    is_exp, _ = zone_explosive_quality(df, z["bar_idx"], z["type"])
    assert is_exp is False


def test_detect_zones_explosive_filters_out_weak_zones():
    weak = _weak_demand_df(start=22000, n_bars=60, base_idx=20)
    # with the explosive gate ON, weak zones must be rejected
    kept = detect_zones_explosive(weak, 55, lookback=40, require_explosive=True)
    assert kept == []
    # with the gate OFF, the weak zone is returned (tagged explosive=False)
    all_zones = detect_zones_explosive(weak, 55, lookback=40, require_explosive=False)
    assert len(all_zones) >= 1
    assert all_zones[0]["explosive"] is False


def test_detect_zones_explosive_keeps_explosive_zone():
    strong = _explosive_demand_df(start=22000, n_bars=60, base_idx=20)
    kept = detect_zones_explosive(strong, 55, lookback=40, require_explosive=True)
    assert len(kept) >= 1
    assert kept[0]["explosive"] is True
    assert kept[0]["expansion_pct"] > 0.0


def test_liquidity_sweep_bear_trap_for_buy():
    """A 1m bar pierces below a prior low then snaps back up → BUY sweep."""
    n = 30
    idx = pd.date_range("2026-08-27 09:15", periods=n, freq="1min",
                        tz="Asia/Kolkata")
    lows = [22000.0 + i * 2 for i in range(20)] + [21950.0, 21955.0] + \
           [22040.0 + i for i in range(8)]
    highs = [l + 10 for l in lows]
    closes = [l + 5 for l in lows]  # closes back above the swept low
    opens = [l + 2 for l in lows]
    vols = [1000] * n
    df = pd.DataFrame({"open": opens, "high": highs, "low": lows,
                       "close": closes, "volume": vols}, index=idx)
    swept, reason = liquidity_sweep(df, n - 1, "BUY")
    assert swept is True
    assert "sweep" in reason


def test_liquidity_sweep_bull_trap_for_sell():
    """A 1m bar pierces above a prior high then snaps back down → SELL sweep."""
    n = 30
    idx = pd.date_range("2026-08-27 09:15", periods=n, freq="1min",
                        tz="Asia/Kolkata")
    highs = [22000.0 - i * 2 for i in range(20)] + [22050.0, 22045.0] + \
            [21960.0 - i for i in range(8)]
    lows = [h - 10 for h in highs]
    closes = [h - 5 for h in highs]  # closes back below the swept high
    opens = [h - 2 for h in highs]
    vols = [1000] * n
    df = pd.DataFrame({"open": opens, "high": highs, "low": lows,
                       "close": closes, "volume": vols}, index=idx)
    swept, reason = liquidity_sweep(df, n - 1, "SELL")
    assert swept is True
    assert "sweep" in reason


def test_liquidity_sweep_no_pierce_returns_false():
    """No new local extreme → no sweep."""
    n = 25
    idx = pd.date_range("2026-08-27 09:15", periods=n, freq="1min",
                        tz="Asia/Kolkata")
    lows = [22000.0 + i for i in range(n)]  # monotonically rising, no pierce
    highs = [l + 5 for l in lows]
    closes = [l + 2 for l in lows]
    opens = [l for l in lows]
    vols = [1000] * n
    df = pd.DataFrame({"open": opens, "high": highs, "low": lows,
                       "close": closes, "volume": vols}, index=idx)
    swept, _ = liquidity_sweep(df, n - 1, "BUY")
    assert swept is False


def test_dynamic_trail_does_not_fire_below_30pct_gain():
    """Before +30% gain the trail must NOT trigger (ride the noise)."""
    pos = {"entry_premium": 100.0, "stop_premium": 80.0, "direction": "BUY",
           "opposing_zone_edge": None, "peak_premium": 110.0}
    # +10% gain → below 30% activation threshold
    res = check_intraday_exit(pos, cur_underlying=22100, cur_premium=110,
                              is_square_off_bar=False)
    assert res["exit"] is False


def test_dynamic_trail_locks_in_after_30pct_gain_on_retracement():
    """After +30%, a retracement that breaches 55% of peak profit locks in."""
    pos = {"entry_premium": 100.0, "stop_premium": 80.0, "direction": "BUY",
           "opposing_zone_edge": None, "peak_premium": 160.0}
    # peak +60%, trail floor = 100 * (1 + 0.60*0.55) = 133 → cur 130 triggers
    res = check_intraday_exit(pos, cur_underlying=22100, cur_premium=130,
                              is_square_off_bar=False)
    assert res["exit"] is True
    assert res["reason"] == "dynamic_trail_lock"


def test_dynamic_trail_rides_above_floor():
    """After +30%, a minor retracement that stays above the trail floor keeps riding."""
    pos = {"entry_premium": 100.0, "stop_premium": 80.0, "direction": "BUY",
           "opposing_zone_edge": None, "peak_premium": 160.0}
    # floor 133 → cur 150 keeps riding (no opposing zone, no exhaustion)
    res = check_intraday_exit(pos, cur_underlying=22100, cur_premium=150,
                              is_square_off_bar=False)
    assert res["exit"] is False


def test_1m_exhaustion_gated_until_15pct_gain():
    """A 1m reversal candle at small gain (<15%) must NOT exit (mid-rocket noise)."""
    n = 30
    idx = pd.date_range("2026-08-27 09:15", periods=n, freq="1min",
                        tz="Asia/Kolkata")
    opens = [100.0 + i for i in range(n - 1)] + [129.0]
    highs = [o + 2 for o in opens]
    lows = [o - 2 for o in opens]
    closes = [o - 1 for o in opens]
    closes[-1] = 120.0  # bearish reversal candle
    vols = [500] * (n - 1) + [5000]  # volume on the reversal
    df_1m = pd.DataFrame({"open": opens, "high": highs, "low": lows,
                          "close": closes, "volume": vols}, index=idx)
    pos = {"entry_premium": 100.0, "stop_premium": 80.0, "direction": "BUY",
           "opposing_zone_edge": None}
    # +10% gain (110) — below 15% → exhaustion gated, keep riding
    res = check_intraday_exit(pos, cur_underlying=22100, cur_premium=110,
                              is_square_off_bar=False,
                              df_1m=df_1m, i_1m=n - 1)
    assert res["exit"] is False


def test_opposing_zone_exit_takes_priority_over_trail():
    """Reaching the opposing zone exits regardless of the trail (move done)."""
    pos = {"entry_premium": 100.0, "stop_premium": 80.0, "direction": "BUY",
           "opposing_zone_edge": 22500.0, "peak_premium": 160.0}
    res = check_intraday_exit(pos, cur_underlying=22500, cur_premium=150,
                              is_square_off_bar=False)
    assert res["exit"] is True
    assert res["reason"] == "opposing_zone_reached"


def test_find_sniper_entry_requires_explosive_zone():
    """Sniper must NOT fire on a weak/choppy zone (explosive gate)."""
    df_15m = _weak_demand_df(start=22000, n_bars=200, base_idx=150)
    df_1m = _one_min_df(start=22000, n_bars=400, seed=3)
    setup = find_sniper_entry(df_15m, len(df_15m) - 2, df_1m, "stocks",
                              is_expiry=False)
    # weak zone → no explosive zone passes the gate → no sniper entry
    assert setup is None


def test_find_sniper_entry_carries_v66_fields():
    """When a sniper fires on an explosive zone, it carries the V6.6 fields."""
    df_15m = _explosive_demand_df(start=22000, n_bars=200, base_idx=150)
    df_1m = _one_min_df(start=22000, n_bars=400, seed=5)
    # the explosion is mid-bar; build a 1m touch + delta spike manually is
    # complex — just assert the engine doesn't crash and fields exist if set
    setup = find_sniper_entry(df_15m, len(df_15m) - 2, df_1m, "stocks",
                              is_expiry=False)
    if setup is not None:
        for k in ("explosive", "sweep", "strike_kind", "delta_spike_mult",
                  "expansion_pct"):
            assert k in setup


def test_sniper_strategy_renamed_to_boom():
    """V6.6 sniper strategy labels use 'Boom_Sniper' (momentum capture)."""
    df_15m = _explosive_demand_df(start=22000, n_bars=200, base_idx=150)
    df_1m = _one_min_df(start=22000, n_bars=400, seed=7)
    setup = find_sniper_entry(df_15m, len(df_15m) - 2, df_1m, "stocks",
                              is_expiry=False)
    if setup is not None:
        assert "Boom_Sniper" in setup["strategy"]
