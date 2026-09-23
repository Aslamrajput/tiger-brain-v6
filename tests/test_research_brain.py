"""Tests for Tiger Research Brain — top gainers/losers + SMC trade plan."""
from __future__ import annotations

import pytest
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd
import numpy as np

from subbrains.research_brain import (
    MoverAnalysis,
    TradePlan,
    generate_trade_plan,
    scan_top_movers,
    analyze_movers_smc,
    save_plan,
    load_plan,
    run_research,
    get_today_watchlist,
    TOP_N_MOVERS,
    RESEARCH_CACHE_FILE,
)


class TestTradePlanModel:
    """TradePlan data model tests."""

    def test_trade_plan_defaults(self):
        plan = TradePlan(generated_at="2026-01-15 09:00:00", trade_date="2026-01-15")
        assert plan.top_gainers == []
        assert plan.top_losers == []
        assert plan.today_watchlist == []
        assert plan.market_bias == "NEUTRAL"

    def test_trade_plan_summary(self):
        plan = TradePlan(
            generated_at="2026-01-15 09:00:00",
            trade_date="2026-01-15",
            market_bias="BULLISH",
            top_gainers=[{
                "symbol": "RELIANCE", "daily_change_pct": 3.5,
                "bias": "BUY_CE", "research_score": 25.0,
            }],
            top_losers=[{
                "symbol": "TATASTEEL", "daily_change_pct": -2.1,
                "bias": "BUY_PE", "research_score": 18.0,
            }],
            today_watchlist=[{
                "symbol": "RELIANCE", "action": "BUY_CE",
                "zone_type": "demand", "zone_price": 2400,
            }],
        )
        summary = plan.summary()
        assert "RESEARCH BRAIN" in summary
        assert "BULLISH" in summary
        assert "RELIANCE" in summary
        assert "TATASTEEL" in summary


class TestScanTopMovers:
    """scan_top_movers — daily % change ranking."""

    def test_empty_symbols_returns_empty(self):
        gainers, losers = scan_top_movers({})
        assert gainers == []
        assert losers == []

    @patch("subbrains.research_brain.yf")
    def test_sorts_gainers_and_losers(self, mock_yf):
        """Gainers sorted descending, losers ascending."""
        # Mock yfinance download returning 2-day data for 3 stocks
        dates = pd.date_range("2026-01-14", periods=2)
        data = {}
        for sym, (p0, p1) in {
            "WINNER.NS": (100, 110),   # +10%
            "LOSER.NS": (100, 90),     # -10%
            "FLAT.NS": (100, 101),     # +1%
        }.items():
            data[sym] = pd.DataFrame({
                "Close": [p0, p1],
                "Volume": [10000000, 10000000],  # high volume
            }, index=dates)

        mock_df = pd.concat(data, axis=1, keys=data.keys())
        mock_yf.download.return_value = mock_df

        symbols = {"WINNER": "WINNER.NS", "LOSER": "LOSER.NS", "FLAT": "FLAT.NS"}
        gainers, losers = scan_top_movers(symbols, top_n=2)

        assert len(gainers) > 0
        assert gainers[0]["symbol"] == "WINNER"
        assert gainers[0]["daily_change_pct"] == pytest.approx(10.0, abs=0.1)
        assert losers[0]["symbol"] == "LOSER"
        assert losers[0]["daily_change_pct"] == pytest.approx(-10.0, abs=0.1)

    @patch("subbrains.research_brain.yf")
    def test_filters_low_volume(self, mock_yf):
        """Stocks below MIN_DAILY_VOLUME_CR are filtered out."""
        dates = pd.date_range("2026-01-14", periods=2)
        data = {}
        for sym, (p0, p1, vol) in {
            "LIQUID.NS": (100, 110, 50000000),   # ₹500cr volume
            "ILLIQUID.NS": (100, 110, 100),       # ₹0 volume
        }.items():
            data[sym] = pd.DataFrame({
                "Close": [p0, p1],
                "Volume": [vol, vol],
            }, index=dates)
        mock_df = pd.concat(data, axis=1, keys=data.keys())
        mock_yf.download.return_value = mock_df

        symbols = {"LIQUID": "LIQUID.NS", "ILLIQUID": "ILLIQUID.NS"}
        gainers, losers = scan_top_movers(symbols)
        syms = [g["symbol"] for g in gainers]
        assert "LIQUID" in syms
        assert "ILLIQUID" not in syms


class TestGenerateTradePlan:
    """Trade plan generation from mover analysis."""

    def test_bullish_bias_when_strong_gainers(self):
        """More strong gainers than losers → BULLISH."""
        gainers = [
            MoverAnalysis("A", "A", 5.0, "GAINER", 100, 100,
                          research_score=20, bias="BUY_CE"),
            MoverAnalysis("B", "B", 4.0, "GAINER", 100, 100,
                          research_score=18, bias="BUY_CE"),
            MoverAnalysis("C", "C", 3.0, "GAINER", 100, 100,
                          research_score=16, bias="BUY_CE"),
        ]
        losers = [
            MoverAnalysis("D", "D", -2.0, "LOSER", 100, 100,
                          research_score=10, bias="BUY_PE"),
        ]
        plan = generate_trade_plan(gainers, losers)
        assert plan.market_bias == "BULLISH"

    def test_bearish_bias_when_strong_losers(self):
        """More strong losers → BEARISH."""
        gainers = [
            MoverAnalysis("A", "A", 1.0, "GAINER", 100, 100,
                          research_score=10, bias="BUY_CE"),
        ]
        losers = [
            MoverAnalysis("B", "B", -5.0, "LOSER", 100, 100,
                          research_score=20, bias="BUY_PE"),
            MoverAnalysis("C", "C", -4.0, "LOSER", 100, 100,
                          research_score=18, bias="BUY_PE"),
        ]
        plan = generate_trade_plan(gainers, losers)
        assert plan.market_bias == "BEARISH"

    def test_neutral_bias_when_balanced(self):
        """Equal strong gainers + losers → NEUTRAL."""
        gainers = [
            MoverAnalysis("A", "A", 3.0, "GAINER", 100, 100,
                          research_score=20, bias="BUY_CE"),
        ]
        losers = [
            MoverAnalysis("B", "B", -3.0, "LOSER", 100, 100,
                          research_score=20, bias="BUY_PE"),
        ]
        plan = generate_trade_plan(gainers, losers)
        assert plan.market_bias == "NEUTRAL"

    def test_watchlist_excludes_neutral_bias(self):
        """NEUTRAL bias stocks don't make the watchlist."""
        gainers = [
            MoverAnalysis("A", "A", 1.0, "GAINER", 100, 100,
                          research_score=5, bias="NEUTRAL"),
            MoverAnalysis("B", "B", 5.0, "GAINER", 100, 100,
                          research_score=25, bias="BUY_CE"),
        ]
        losers = []
        plan = generate_trade_plan(gainers, losers)
        watch_symbols = [w["symbol"] for w in plan.today_watchlist]
        assert "B" in watch_symbols
        assert "A" not in watch_symbols

    def test_watchlist_sorted_by_research_score(self):
        """Higher score = higher priority in watchlist."""
        gainers = [
            MoverAnalysis("LOW", "L", 2.0, "GAINER", 100, 100,
                          research_score=10, bias="BUY_CE"),
            MoverAnalysis("HIGH", "H", 5.0, "GAINER", 100, 100,
                          research_score=30, bias="BUY_CE"),
        ]
        losers = []
        plan = generate_trade_plan(gainers, losers)
        assert plan.today_watchlist[0]["symbol"] == "HIGH"


class TestPlanCache:
    """Save/load research plan cache."""

    def test_save_and_load_plan(self, tmp_path):
        """Plan saved to JSON can be loaded back."""
        plan = TradePlan(
            generated_at=datetime.now().isoformat(sep=" "),
            trade_date="2026-01-15",
            market_bias="BULLISH",
            today_watchlist=[{"symbol": "RELIANCE", "action": "BUY_CE"}],
        )
        cache = tmp_path / "research_plan.json"
        save_plan(plan, cache)
        loaded = load_plan(cache)
        assert loaded is not None
        assert loaded.market_bias == "BULLISH"
        assert loaded.today_watchlist[0]["symbol"] == "RELIANCE"

    def test_stale_plan_returns_none(self, tmp_path):
        """Plan older than RESEARCH_CACHE_HOURS returns None."""
        from subbrains.research_brain import RESEARCH_CACHE_HOURS
        old_time = datetime(2020, 1, 1).isoformat(sep=" ")
        cache = tmp_path / "research_plan.json"
        cache.write_text(json.dumps({
            "generated_at": old_time,
            "trade_date": "2020-01-01",
            "top_gainers": [], "top_losers": [],
            "today_watchlist": [], "tomorrow_candidates": [],
            "market_bias": "NEUTRAL", "notes": "",
        }))
        loaded = load_plan(cache)
        assert loaded is None  # stale

    def test_missing_file_returns_none(self, tmp_path):
        """Non-existent cache file returns None."""
        loaded = load_plan(tmp_path / "nonexistent.json")
        assert loaded is None


class TestGetTodayWatchlist:
    """Watchlist retrieval for intraday prioritization."""

    def test_empty_when_no_cache(self):
        """Returns empty list when no plan cached."""
        with patch("subbrains.research_brain.load_plan", return_value=None):
            assert get_today_watchlist() == []

    def test_returns_symbols_from_plan(self):
        """Returns symbol list from cached plan."""
        plan = TradePlan(
            generated_at=datetime.now().isoformat(sep=" "),
            trade_date="2026-01-15",
            today_watchlist=[
                {"symbol": "RELIANCE", "action": "BUY_CE"},
                {"symbol": "TCS", "action": "BUY_PE"},
            ],
        )
        with patch("subbrains.research_brain.load_plan", return_value=plan):
            wl = get_today_watchlist()
            assert "RELIANCE" in wl
            assert "TCS" in wl


class TestResearchBrainWiring:
    """Verify research brain is wired into Tiger's pre-market."""

    def test_research_brain_called_in_pre_market(self):
        """pre_market_wake must call run_research."""
        from automation.tiger_live import TigerLiveRunner
        import inspect
        source = inspect.getsource(TigerLiveRunner.pre_market_wake)
        assert "run_research" in source
        assert "research_brain" in source
        assert "_research_plan" in source

    def test_research_brain_module_imports(self):
        """Module must import without errors."""
        from subbrains.research_brain import run_research, get_today_watchlist
        assert callable(run_research)
        assert callable(get_today_watchlist)

    def test_buying_only_bias(self):
        """Research brain only produces BUY_CE or BUY_PE — never SELL."""
        gainers = [MoverAnalysis("A", "A", 5, "GAINER", 100, 100,
                                 bias="BUY_CE", research_score=20)]
        losers = [MoverAnalysis("B", "B", -5, "LOSER", 100, 100,
                                bias="BUY_PE", research_score=20)]
        plan = generate_trade_plan(gainers, losers)
        for w in plan.today_watchlist:
            assert w["action"] in ("BUY_CE", "BUY_PE")
            assert "SELL" not in w["action"]
