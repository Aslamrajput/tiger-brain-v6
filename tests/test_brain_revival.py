"""Tests for the 3 revived brains (3, 5, 6) wired into tiger_live.py.

Brain 3: Spread check advisory (bid-ask liquidity)
Brain 5: Gamma tracking advisory (near-expiry protection)
Brain 6: Premium discount advisor (IV percentile)
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from backtest.tiger_premium_brain import (
    PremiumDiscountTracker,
    IV_DEEP_DISCOUNT_MAX,
    IV_DISCOUNT_MAX,
    IV_FAIR_MAX,
)
from risk.exit_brain import check_gamma_risk
from config.thresholds import BRAIN5


class TestBrain6PremiumDiscount:
    """Brain 6 — IV percentile advisor now wired into live entry path."""

    def test_tracker_initialized_in_tiger_live(self):
        """PremiumDiscountTracker instance must be created in __init__."""
        from automation.tiger_live import TigerLiveRunner
        import inspect
        source = inspect.getsource(TigerLiveRunner.__init__)
        assert "PremiumDiscountTracker" in source, \
            "Brain 6 tracker not initialized in TigerLiveRunner.__init__"

    def test_deep_discount_adds_score_bonus(self):
        """IV at deep discount → +10 score bonus (advisory, not block)."""
        tracker = PremiumDiscountTracker()
        # Feed 100 low-IV readings so current low IV is deep discount
        for _ in range(50):
            tracker.update("TEST", 0.12)
        # Now current IV is still 0.12 → percentile should be low → DEEP_DISCOUNT
        snap = tracker.evaluate("TEST", 0.12, setup_score=75.0)
        assert snap.discount_bonus == 10.0
        assert snap.premium_status == "DEEP_DISCOUNT"
        assert snap.should_enter is True  # advisory: allow entry

    def test_expensive_iv_logs_warning_not_block(self):
        """IV expensive → should_enter=False, but advisory in live (Tiger decides)."""
        tracker = PremiumDiscountTracker()
        for _ in range(50):
            tracker.update("TEST", 0.10)
        # Current IV jumps to 0.50 → should be EXPENSIVE percentile
        snap = tracker.evaluate("TEST", 0.50, setup_score=50.0)
        assert snap.premium_status == "EXPENSIVE"
        assert snap.should_enter is False  # brain says no, but live = advisory

    def test_fair_iv_blocks_without_high_score(self):
        """Fair IV with low score → blocked by brain (advisory in live)."""
        tracker = PremiumDiscountTracker()
        # Feed mixed IV history: some high, some low
        for i in range(25):
            tracker.update("TEST", 0.10)
            tracker.update("TEST", 0.25)
        # Current IV at 0.18 → should be roughly middle percentile → FAIR
        snap = tracker.evaluate("TEST", 0.18, setup_score=75.0)
        assert snap.premium_status in ("FAIR", "DISCOUNT")
        if snap.premium_status == "FAIR":
            assert snap.should_enter is False

    def test_iv_check_code_in_place_live_orders(self):
        """Brain 6 IV check block must exist in _place_live_orders."""
        from automation.tiger_live import TigerLiveRunner
        import inspect
        source = inspect.getsource(TigerLiveRunner._place_live_orders)
        assert "BRAIN 6" in source
        assert "compute_iv" in source
        assert "premium_tracker" in source


class TestBrain5GammaTracking:
    """Brain 5 — gamma tracking now wired into monitor_open_positions."""

    def test_gamma_check_code_in_place(self):
        """Gamma advisory block must exist in monitor_open_positions."""
        from automation.tiger_live import TigerLiveRunner
        import inspect
        source = inspect.getsource(TigerLiveRunner.monitor_open_positions)
        assert "BRAIN 5" in source
        assert "check_gamma_risk" in source
        assert "gamma_warning" in source

    def test_gamma_exit_when_near_expiry_high_gamma(self):
        """Near expiry + high gamma → exit signal."""
        pos = {"days_to_expiry": 1, "gamma_pct": 3.0}
        result = check_gamma_risk(pos, gamma_pct=3.0)
        assert result["gamma_exit"] is True
        assert result["reason"] == "exit"

    def test_gamma_watch_when_near_expiry_low_gamma(self):
        """Near expiry + low gamma → watch mode (no exit)."""
        pos = {"days_to_expiry": 1, "gamma_pct": 0.5}
        result = check_gamma_risk(pos, gamma_pct=0.5)
        assert result["gamma_exit"] is False
        assert result["reason"] == "watch"

    def test_gamma_ok_when_far_from_expiry(self):
        """Far from expiry → gamma risk normal."""
        pos = {"days_to_expiry": 10, "gamma_pct": 3.0}
        result = check_gamma_risk(pos, gamma_pct=3.0)
        assert result["gamma_exit"] is False
        assert result["reason"] == "ok"

    def test_gamma_threshold_from_config(self):
        """GAMMA_RISK_DAYS_TO_EXPIRY should be 2 (config)."""
        assert BRAIN5["GAMMA_RISK_DAYS_TO_EXPIRY"] == 2
        assert BRAIN5["GAMMA_RISK_THRESHOLD_PCT"] == 2.0


class TestBrain3SpreadCheck:
    """Brain 3 — spread check advisory now wired into order placement."""

    def test_spread_check_code_in_place(self):
        """Spread advisory block must exist in _place_live_orders."""
        from automation.tiger_live import TigerLiveRunner
        import inspect
        source = inspect.getsource(TigerLiveRunner._place_live_orders)
        assert "BRAIN 3" in source
        assert "MAX_SPREAD_PCT_OF_PREMIUM" in source
        assert "spread" in source.lower()

    def test_spread_ok_function_exists(self):
        """option_selector._spread_ok must work correctly."""
        from broker.option_selector import _spread_ok
        # Tight spread → OK
        assert _spread_ok({"bid": 99, "ask": 101, "ltp": 100}) is True
        # Wide spread → NOT OK
        assert _spread_ok({"bid": 95, "ask": 110, "ltp": 100}) is False
        # Missing bid/ask → NOT OK (can't verify)
        assert _spread_ok({"bid": None, "ask": 101, "ltp": 100}) is False
