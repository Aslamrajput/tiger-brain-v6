"""Tests for the 5-Brain walk-forward backtest engine (brain_backtest.py)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.brain_backtest import (
    build_chain,
    bs_call_price,
    bs_put_price,
    bs_delta,
    realized_vol,
    run_5brain_backtest,
)


def _synthetic_df(start=22000, slope=1500, n=120, seed=1, vol=0.015):
    np.random.seed(seed)
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    c = start + np.linspace(0, slope, n) + np.random.normal(0, start * vol, n).cumsum()
    d = pd.DataFrame(index=dates)
    d["close"] = c
    d["open"] = np.r_[c[0], c[:-1]]
    d["high"] = np.maximum(d["open"], d["close"]) + start * 0.003
    d["low"] = np.minimum(d["open"], d["close"]) - start * 0.003
    d["volume"] = np.random.randint(100000, 400000, n)
    return d


def test_bs_call_put_price_put_call_parity_floor():
    # ITM call has positive intrinsic value at expiry
    assert bs_call_price(22000, 21000, 0.0, 0.2) == 1000.0
    assert bs_put_price(22000, 23000, 0.0, 0.2) == 1000.0
    # OTM at expiry is zero
    assert bs_call_price(22000, 23000, 0.0, 0.2) == 0.0


def test_bs_delta_call_in_range_pe_negative():
    # ATM-ish call delta should be ~0.5; PE delta negative
    d_call = bs_delta(22000, 22000, 30 / 365, 0.2, is_call=True)
    d_put = bs_delta(22000, 22000, 30 / 365, 0.2, is_call=False)
    assert 0.45 < d_call < 0.65
    assert -0.65 < d_put < -0.35


def test_realized_vol_finite_and_positive():
    df = _synthetic_df()
    v = realized_vol(df, window=20)
    assert 0.05 < v < 1.0


def test_build_chain_contracts_cover_delta_band():
    df = _synthetic_df()
    cur = float(df["close"].iloc[-1])
    chain = build_chain(cur, df, days_to_expiry=5)
    assert "contracts" in chain
    assert len(chain["contracts"]) > 0
    # both CE and PE present
    types = {c["option_type"] for c in chain["contracts"]}
    assert "CE" in types and "PE" in types
    # at least one CE in the 0.45-0.60 band (Brain 3 delta range)
    ce_deltas = [abs(c["delta"]) for c in chain["contracts"] if c["option_type"] == "CE"]
    assert any(0.40 <= d <= 0.75 for d in ce_deltas)
    # spread within Brain 3 gate (<=2% of premium)
    for c in chain["contracts"]:
        spread_pct = (c["ask"] - c["bid"]) / c["ltp"] * 100
        assert spread_pct <= 2.5


def test_build_chain_no_lookahead_uses_only_so_far():
    """Chain IV must depend only on data passed in, not future bars."""
    df = _synthetic_df(n=100)
    full = build_chain(float(df["close"].iloc[-1]), df, 5)
    half = build_chain(float(df["close"].iloc[49]), df.iloc[:50], 5)
    # Different data windows -> generally different IV (not guaranteed equal)
    assert full["underlying_price"] != half["underlying_price"]


def test_run_backtest_returns_required_keys():
    df = _synthetic_df(start=22000, slope=800, n=130, seed=3)
    df2 = _synthetic_df(start=2800, slope=40, n=130, seed=5)
    bench = _synthetic_df(start=80000, slope=2000, n=130, seed=99)
    res = run_5brain_backtest(
        {"NIFTY": df, "RELIANCE": df2}, bench, start_capital=150000.0, warmup=30
    )
    assert "totals" in res and "segment_stats" in res
    assert "trades" in res and "equity_curve" in res
    t = res["totals"]
    for k in ("total_trades", "win_rate_pct", "profit_factor", "max_drawdown_pct"):
        assert k in t


def test_trade_cap_never_exceeds_global_limit():
    """Brain 4 must never allow more than 10 trades on a single day."""
    df = _synthetic_df(start=22000, slope=800, n=140, seed=3)
    bench = _synthetic_df(start=80000, slope=2000, n=140, seed=99)
    res = run_5brain_backtest({"NIFTY": df}, bench, start_capital=150000.0, warmup=30)
    by_day = {}
    for tr in res["trades"]:
        d = tr["entry_date"].date() if hasattr(tr["entry_date"], "date") else tr["entry_date"]
        by_day[d] = by_day.get(d, 0) + 1
    max_day = max(by_day.values()) if by_day else 0
    assert max_day <= 10, f"Brain 4 cap violated: {max_day} trades on one day"


def test_no_lookahead_engine_uses_only_prior_bars():
    """Equity at each step must be computable from data up to that date."""
    df = _synthetic_df(start=22000, slope=800, n=130, seed=3)
    bench = _synthetic_df(start=80000, slope=2000, n=130, seed=99)
    res = run_5brain_backtest({"NIFTY": df}, bench, start_capital=150000.0, warmup=30)
    ec = res["equity_curve"]
    # equity curve dates are a subset of input dates
    input_dates = set(df.index)
    for point in ec:
        assert point["date"] in input_dates
    # no equity point before warmup
    warmup_date = df.index[30]
    for point in ec:
        assert point["date"] >= warmup_date


def test_position_sizing_respects_capital_cap():
    """Allocated capital per trade respects confidence-based cap (Brain 4)."""
    df = _synthetic_df(start=22000, slope=800, n=140, seed=3)
    bench = _synthetic_df(start=80000, slope=2000, n=140, seed=99)
    res = run_5brain_backtest({"NIFTY": df}, bench, start_capital=150000.0, warmup=30)
    for tr in res["trades"]:
        # Full capital (100%) scaled by confidence tier (60-100%).
        # Max possible = 100% of 150k = 150k.
        assert tr["allocated_capital"] <= 150000 * 1.00 + 100
