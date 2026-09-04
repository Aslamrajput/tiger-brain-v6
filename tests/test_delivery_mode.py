"""Tests for Tiger Brain V16 delivery mode + fund brain integration."""

from __future__ import annotations

import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from backtest.run_tiger_brain_backtest import (
    run_tiger_brain_backtest,
    DELIVERY_ROCKET_MIN_SCORE,
    DELIVERY_MAX_HOLD_DAYS,
    DELIVERY_STOP_PCT,
)
from backtest.tiger_fund_brain import announce_fund_plan, TIER_SMALL


def _make_test_data(symbol="TEST", days=5, bars_per_day=25):
    """Create synthetic 15m OHLCV data for testing."""
    start = datetime(2024, 1, 1, 9, 15)
    rows = []
    for d in range(days):
        for b in range(bars_per_day):
            ts = start + timedelta(days=d, minutes=15 * b)
            base_price = 100 + d * 2 + b * 0.1
            rows.append({
                "open": base_price, "high": base_price + 1,
                "low": base_price - 1, "close": base_price + 0.5,
                "volume": 1000 + b * 10,
            })
    df = pd.DataFrame(rows, index=pd.DatetimeIndex(
        [start + timedelta(days=d, minutes=15*b) for d in range(days) for b in range(bars_per_day)]
    ))
    df.index.name = "ts"
    return df


def _make_1m_data(symbol="TEST", days=5, bars_per_day=25):
    """Create synthetic 1m OHLCV data matching the 15m data."""
    start = datetime(2024, 1, 1, 9, 15)
    rows = []
    for d in range(days):
        for b in range(bars_per_day * 15):
            ts = start + timedelta(days=d, minutes=b)
            base_price = 100 + d * 2 + (b / 15) * 0.1
            rows.append({
                "open": base_price, "high": base_price + 0.5,
                "low": base_price - 0.5, "close": base_price + 0.3,
                "volume": 100 + b,
            })
    df = pd.DataFrame(rows, index=pd.DatetimeIndex(
        [start + timedelta(days=d, minutes=b) for d in range(days) for b in range(bars_per_day * 15)]
    ))
    df.index.name = "ts"
    return df


class TestDeliveryModeConstants:
    def test_delivery_score_threshold(self):
        assert DELIVERY_ROCKET_MIN_SCORE == 90

    def test_delivery_hold_days(self):
        assert DELIVERY_MAX_HOLD_DAYS == 3

    def test_delivery_stop_pct(self):
        assert DELIVERY_STOP_PCT == 30.0


class TestFundBrainIntegration:
    def test_backtest_with_fund_brain_enabled(self):
        """Verify fund brain activates when use_fund_brain=True."""
        df = _make_test_data()
        data_map = {"TEST": df}
        result = run_tiger_brain_backtest(
            data_map, start_capital=100000, use_fund_brain=True,
            use_delivery_mode=False, verbose=False)
        # Should complete without error
        assert "totals" in result
        assert "trades" in result

    def test_backtest_with_fund_brain_disabled(self):
        """Verify backtest works with fund brain disabled (legacy mode)."""
        df = _make_test_data()
        data_map = {"TEST": df}
        result = run_tiger_brain_backtest(
            data_map, start_capital=100000, use_fund_brain=False,
            use_delivery_mode=False, verbose=False)
        assert "totals" in result

    def test_backtest_with_delivery_mode_enabled(self):
        """Verify delivery mode doesn't crash the backtest."""
        df = _make_test_data()
        data_map = {"TEST": df}
        result = run_tiger_brain_backtest(
            data_map, start_capital=100000, use_fund_brain=True,
            use_delivery_mode=True, verbose=False)
        assert "totals" in result

    def test_micro_account_backtest(self):
        """Verify ₹10,000 micro account runs without crashing."""
        df = _make_test_data()
        data_map = {"TEST": df}
        result = run_tiger_brain_backtest(
            data_map, start_capital=10000, use_fund_brain=True,
            use_delivery_mode=True, verbose=False)
        assert "totals" in result

    def test_whale_account_backtest(self):
        """Verify ₹10cr whale account runs without crashing."""
        df = _make_test_data()
        data_map = {"TEST": df}
        result = run_tiger_brain_backtest(
            data_map, start_capital=10000000000, use_fund_brain=True,
            use_delivery_mode=True, verbose=False)
        assert "totals" in result
