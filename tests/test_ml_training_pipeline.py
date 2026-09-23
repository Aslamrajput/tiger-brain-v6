"""Tests for backtest/ml_training_pipeline.py — 3 advanced ML training modules.

Module 1: Volatility & Momentum Filters (ATR + IV Rank)
Module 2: Slippage & Liquidity Simulation (real-world costs)
Module 3: Walk-Forward Optimization (rolling window, anti-overfit)
"""
import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta


class TestVolatilityFilter:
    """Module 1 — Volatility & Momentum Filters."""

    def test_passes_when_atr_and_iv_in_range(self):
        from backtest.ml_training_pipeline import passes_volatility_filter
        passes, reason = passes_volatility_filter(atr_pct=0.5, iv_rank=50)
        assert passes is True
        assert "vol_ok" in reason

    def test_rejects_low_atr_sideways_market(self):
        from backtest.ml_training_pipeline import passes_volatility_filter
        passes, reason = passes_volatility_filter(atr_pct=0.1, iv_rank=50)
        assert passes is False
        assert "low_volatility" in reason

    def test_rejects_low_iv_rank_theta_risk(self):
        from backtest.ml_training_pipeline import passes_volatility_filter
        passes, reason = passes_volatility_filter(atr_pct=0.5, iv_rank=15)
        assert passes is False
        assert "low_iv_rank" in reason
        assert "theta" in reason

    def test_rejects_high_iv_rank_overpriced(self):
        from backtest.ml_training_pipeline import passes_volatility_filter
        passes, reason = passes_volatility_filter(atr_pct=0.5, iv_rank=98)
        assert passes is False
        assert "high_iv_rank" in reason
        assert "overpriced" in reason

    def test_filter_disabled_passes_all(self):
        from backtest.ml_training_pipeline import passes_volatility_filter
        cfg = {"ENABLED": False}
        passes, reason = passes_volatility_filter(0.01, 5, cfg)
        assert passes is True
        assert reason == "filter_disabled"

    def test_calculate_atr_pct_basic(self):
        from backtest.ml_training_pipeline import calculate_atr_pct
        df = pd.DataFrame({
            "high": [105, 110, 108, 115],
            "low": [95, 100, 98, 105],
            "close": [100, 105, 103, 110],
        })
        atr_pct = calculate_atr_pct(df, period=3)
        assert atr_pct > 0
        assert atr_pct < 100  # reasonable percentage

    def test_calculate_atr_pct_empty_df(self):
        from backtest.ml_training_pipeline import calculate_atr_pct
        assert calculate_atr_pct(pd.DataFrame(), period=14) == 0.0

    def test_calculate_iv_rank(self):
        from backtest.ml_training_pipeline import calculate_iv_rank
        history = [10, 15, 20, 25, 30, 35, 40]
        # Current IV = 30, min=10, max=40 → rank = (30-10)/(40-10)*100 = 66.67
        rank = calculate_iv_rank(30.0, history)
        assert rank == pytest.approx(66.67, abs=0.5)

    def test_calculate_iv_rank_no_history(self):
        from backtest.ml_training_pipeline import calculate_iv_rank
        assert calculate_iv_rank(30.0, []) == 50.0  # neutral

    def test_calculate_iv_rank_flat_history(self):
        from backtest.ml_training_pipeline import calculate_iv_rank
        # All same value → can't compute range → neutral
        assert calculate_iv_rank(20.0, [20, 20, 20]) == 50.0


class TestSlippageModel:
    """Module 2 — Slippage & Liquidity Simulation."""

    def test_slippage_reduces_profit(self):
        from backtest.ml_training_pipeline import apply_slippage_and_costs
        # Buy at ₹50, sell at ₹60, qty=100
        result = apply_slippage_and_costs(50, 60, 100, "BUY")
        assert result["gross_pnl"] == 1000  # (60-50)*100
        assert result["net_pnl"] < result["gross_pnl"]  # costs eat into profit
        assert result["total_costs"] > 0
        assert result["cost_pct"] > 0

    def test_slippage_adjusts_entry_and_exit(self):
        from backtest.ml_training_pipeline import apply_slippage_and_costs
        result = apply_slippage_and_costs(50, 60, 100, "BUY")
        # Entry should be higher (worse for buyer), exit lower (worse for seller)
        assert result["adjusted_entry"] > 50
        assert result["adjusted_exit"] < 60

    def test_costs_include_all_components(self):
        from backtest.ml_training_pipeline import apply_slippage_and_costs
        result = apply_slippage_and_costs(50, 60, 100, "BUY")
        breakdown = result.get("costs_breakdown", {})
        assert "slippage" in breakdown
        assert "exchange_fee" in breakdown
        assert "stt" in breakdown
        assert "brokerage" in breakdown
        assert "gst" in breakdown
        # Brokerage = ₹20 * 2 (entry + exit)
        assert breakdown["brokerage"] == 40.0

    def test_losing_trade_loses_more_after_costs(self):
        from backtest.ml_training_pipeline import apply_slippage_and_costs
        # Buy at ₹60, sell at ₹50 (loss)
        result = apply_slippage_and_costs(60, 50, 100, "BUY")
        assert result["gross_pnl"] == -1000
        assert result["net_pnl"] < result["gross_pnl"]  # even worse with costs

    def test_disabled_config_no_costs(self):
        from backtest.ml_training_pipeline import apply_slippage_and_costs
        cfg = {"ENABLED": False}
        result = apply_slippage_and_costs(50, 60, 100, "BUY", cfg)
        assert result["gross_pnl"] == result["net_pnl"]
        assert result["total_costs"] == 0.0

    def test_validate_model_after_costs_profitable(self):
        from backtest.ml_training_pipeline import validate_model_after_costs
        trades = [
            {"status": "CLOSED", "entry_price": 50, "exit_price": 60, "quantity": 100, "symbol": "A"},
            {"status": "CLOSED", "entry_price": 50, "exit_price": 55, "quantity": 100, "symbol": "B"},
            {"status": "CLOSED", "entry_price": 50, "exit_price": 48, "quantity": 100, "symbol": "C"},
        ]
        result = validate_model_after_costs(trades)
        assert result["n_trades"] == 3
        assert result["validated"] is True  # 2/3 wins, net PnL positive
        assert result["total_net_pnl"] > 0

    def test_validate_model_after_costs_unprofitable(self):
        from backtest.ml_training_pipeline import validate_model_after_costs
        trades = [
            {"status": "CLOSED", "entry_price": 50, "exit_price": 40, "quantity": 100, "symbol": "A"},
            {"status": "CLOSED", "entry_price": 50, "exit_price": 45, "quantity": 100, "symbol": "B"},
            {"status": "CLOSED", "entry_price": 50, "exit_price": 48, "quantity": 100, "symbol": "C"},
        ]
        result = validate_model_after_costs(trades)
        assert result["validated"] is False  # all losses

    def test_validate_skips_open_records(self):
        from backtest.ml_training_pipeline import validate_model_after_costs
        trades = [
            {"status": "OPEN", "entry_price": 50, "quantity": 100, "symbol": "A"},
            {"status": "CLOSED", "entry_price": 50, "exit_price": 60, "quantity": 100, "symbol": "B"},
        ]
        result = validate_model_after_costs(trades)
        assert result["n_trades"] == 1  # only CLOSED trade

    def test_validate_handles_exit_records_without_prices(self):
        """Exit records that link to entry records by symbol."""
        from backtest.ml_training_pipeline import validate_model_after_costs
        trades = [
            {"status": "OPEN", "entry_price": 50, "quantity": 100, "symbol": "A",
             "tradingsymbol": "A"},
            {"status": "CLOSED", "exit_price": 60, "pnl": 1000,
             "trade_cost": 5000, "symbol": "A", "tradingsymbol": "A"},
        ]
        result = validate_model_after_costs(trades)
        assert result["n_trades"] == 1
        assert result["total_gross_pnl"] > 0


class TestWalkForward:
    """Module 3 — Walk-Forward Optimization."""

    def test_split_generates_windows(self):
        from backtest.ml_training_pipeline import split_walk_forward
        dates = pd.date_range("2026-01-01", periods=120, freq="D")
        df = pd.DataFrame({
            "entry_ts": dates,
            "zone_strength": np.random.rand(120),
            "label": np.random.randint(0, 2, 120),
        })
        windows = split_walk_forward(df, train_days=30, test_days=7, step_days=7)
        assert len(windows) > 0
        for w in windows:
            assert "train_df" in w
            assert "test_df" in w
            assert len(w["train_df"]) > 0
            assert len(w["test_df"]) > 0
            assert w["train_size"] == len(w["train_df"])
            assert w["test_size"] == len(w["test_df"])

    def test_split_empty_data(self):
        from backtest.ml_training_pipeline import split_walk_forward
        windows = split_walk_forward(pd.DataFrame(), train_days=60, test_days=15)
        assert windows == []

    def test_split_insufficient_data(self):
        """If data range < train_days + test_days, no windows."""
        from backtest.ml_training_pipeline import split_walk_forward
        dates = pd.date_range("2026-01-01", periods=5, freq="D")
        df = pd.DataFrame({"entry_ts": dates, "label": [0, 1, 0, 1, 0]})
        windows = split_walk_forward(df, train_days=60, test_days=15)
        assert windows == []

    def test_windows_are_chronological(self):
        """Each window's test period must be AFTER its train period."""
        from backtest.ml_training_pipeline import split_walk_forward
        dates = pd.date_range("2026-01-01", periods=90, freq="D")
        df = pd.DataFrame({"entry_ts": dates, "label": np.random.randint(0, 2, 90)})
        windows = split_walk_forward(df, train_days=30, test_days=7, step_days=7)
        for w in windows:
            train_end = pd.to_datetime(w["train_df"]["entry_ts"].iloc[-1])
            test_start = pd.to_datetime(w["test_df"]["entry_ts"].iloc[0])
            assert test_start > train_end

    def test_windows_rolling_forward(self):
        """Each successive window starts later than the previous."""
        from backtest.ml_training_pipeline import split_walk_forward
        dates = pd.date_range("2026-01-01", periods=120, freq="D")
        df = pd.DataFrame({"entry_ts": dates, "label": np.random.randint(0, 2, 120)})
        windows = split_walk_forward(df, train_days=30, test_days=7, step_days=7)
        for i in range(1, len(windows)):
            prev_start = windows[i - 1]["train_start"]
            curr_start = windows[i]["train_start"]
            assert curr_start > prev_start


class TestPipelineConfig:
    """Config tests for the 3 new modules."""

    def test_volatility_filter_config_exists(self):
        from config.thresholds import ML_ENGINE
        cfg = ML_ENGINE["VOLATILITY_FILTER"]
        assert cfg["ENABLED"] is True
        assert cfg["MIN_ATR_PCT"] == 0.3
        assert cfg["MIN_IV_RANK"] == 30
        assert cfg["MAX_IV_RANK"] == 95

    def test_slippage_model_config_exists(self):
        from config.thresholds import ML_ENGINE
        cfg = ML_ENGINE["SLIPPAGE_MODEL"]
        assert cfg["ENABLED"] is True
        assert cfg["SLIPPAGE_PCT"] == 0.75
        assert cfg["STT_PCT"] == 0.05
        assert cfg["GST_PCT"] == 18.0
        assert cfg["BROKERAGE_FLAT"] == 20.0

    def test_walk_forward_config_exists(self):
        from config.thresholds import ML_ENGINE
        cfg = ML_ENGINE["WALK_FORWARD"]
        assert cfg["TRAIN_WINDOW_DAYS"] == 60
        assert cfg["TEST_WINDOW_DAYS"] == 15
        assert cfg["STEP_DAYS"] == 15
        assert cfg["MIN_TRADES_PER_WINDOW"] == 5
