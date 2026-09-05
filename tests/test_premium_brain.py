"""Tests for Tiger Brain V18 Premium Discount Sniper (Brain 6)."""

from __future__ import annotations

import pytest

from backtest.tiger_premium_brain import (
    PremiumDiscountTracker,
    PremiumSnapshot,
    compute_premium_strike,
    check_premium_exit,
    IV_DEEP_DISCOUNT_MAX,
    IV_DISCOUNT_MAX,
    IV_FAIR_MAX,
    IV_EXPENSIVE_EXIT,
    STRIKE_OTM_THRESHOLD,
    STRIKE_ATM_THRESHOLD,
)


class TestPremiumDiscountTracker:
    def test_empty_tracker_defaults_to_middle_percentile(self):
        tracker = PremiumDiscountTracker()
        snap = tracker.evaluate("RELIANCE", current_iv=0.25)
        # No history → should return 50th percentile (middle)
        assert snap.iv_percentile == 50.0
        assert snap.iv_history_count == 0

    def test_deep_discount_when_iv_low_relative_to_history(self):
        tracker = PremiumDiscountTracker(lookback=100)
        # Populate history with high IVs (0.30-0.40)
        for iv in [0.35, 0.38, 0.32, 0.40, 0.36, 0.33, 0.39, 0.37, 0.34, 0.38]:
            tracker.update("NIFTY", iv)
        # Current IV is low (0.15) → should be deep discount
        snap = tracker.evaluate("NIFTY", current_iv=0.15)
        assert snap.iv_percentile < IV_DEEP_DISCOUNT_MAX
        assert snap.premium_status == "DEEP_DISCOUNT"
        assert snap.should_enter is True
        assert snap.discount_bonus == 10.0
        assert snap.recommended_strike == "OTM"

    def test_discount_when_iv_moderate(self):
        tracker = PremiumDiscountTracker(lookback=100)
        # History: mix of IVs, current sits in 30-50 percentile
        for iv in [0.15, 0.18, 0.20, 0.22, 0.25, 0.28, 0.30, 0.32, 0.35, 0.40]:
            tracker.update("BANKNIFTY", iv)
        # Current IV 0.22 → roughly 30th percentile
        snap = tracker.evaluate("BANKNIFTY", current_iv=0.22)
        assert snap.premium_status in ("DEEP_DISCOUNT", "DISCOUNT")
        assert snap.should_enter is True

    def test_expensive_blocks_entry(self):
        tracker = PremiumDiscountTracker(lookback=100)
        # History: low IVs (0.10-0.15)
        for iv in [0.10, 0.11, 0.12, 0.13, 0.14, 0.15, 0.10, 0.11, 0.12, 0.13]:
            tracker.update("CRUDEOIL", iv)
        # Current IV high (0.40) → expensive
        snap = tracker.evaluate("CRUDEOIL", current_iv=0.40)
        assert snap.iv_percentile > IV_FAIR_MAX
        assert snap.premium_status == "EXPENSIVE"
        assert snap.should_enter is False

    def test_fair_zone_requires_high_score(self):
        tracker = PremiumDiscountTracker(lookback=100)
        # History where current IV is ~55th percentile (fair zone)
        for iv in [0.10, 0.12, 0.14, 0.16, 0.18, 0.20, 0.22, 0.24, 0.26, 0.28,
                   0.30, 0.32, 0.34, 0.36, 0.38, 0.40, 0.42, 0.44, 0.46, 0.50]:
            tracker.update("GOLD", iv)
        # Current IV 0.30 → roughly 50th-55th percentile
        snap_low_score = tracker.evaluate("GOLD", current_iv=0.30, setup_score=80)
        assert snap_low_score.premium_status == "FAIR"
        assert snap_low_score.should_enter is False  # score 80 < 90

        snap_high_score = tracker.evaluate("GOLD", current_iv=0.30, setup_score=95)
        assert snap_high_score.should_enter is True  # score 95 >= 90

    def test_iv_expansion_exit_signal(self):
        tracker = PremiumDiscountTracker(lookback=100)
        # History of low IVs
        for iv in [0.10, 0.11, 0.12, 0.13, 0.14, 0.15, 0.10, 0.11, 0.12, 0.13]:
            tracker.update("SILVER", iv)
        # Current IV very high → should trigger exit
        snap = tracker.evaluate("SILVER", current_iv=0.45, is_holding=True)
        assert snap.should_exit is True
        assert snap.iv_percentile > IV_EXPENSIVE_EXIT

    def test_no_exit_when_not_holding(self):
        tracker = PremiumDiscountTracker(lookback=100)
        for iv in [0.10, 0.11, 0.12, 0.13, 0.14, 0.15, 0.10, 0.11, 0.12, 0.13]:
            tracker.update("SILVER", iv)
        # is_holding=False → should NOT signal exit even if expensive
        snap = tracker.evaluate("SILVER", current_iv=0.45, is_holding=False)
        assert snap.should_exit is False

    def test_history_rolls_over(self):
        tracker = PremiumDiscountTracker(lookback=5)
        for iv in [0.10, 0.12, 0.14, 0.16, 0.18, 0.20, 0.22]:
            tracker.update("TEST", iv)
        # Only last 5 readings kept: [0.14, 0.16, 0.18, 0.20, 0.22]
        snap = tracker.evaluate("TEST", current_iv=0.15)
        assert snap.iv_history_count == 5

    def test_insufficient_history_returns_50th_percentile(self):
        tracker = PremiumDiscountTracker(lookback=100)
        tracker.update("NEW", 0.20)
        tracker.update("NEW", 0.22)
        # Only 2 readings → not enough, default 50th
        snap = tracker.evaluate("NEW", current_iv=0.25)
        assert snap.iv_percentile == 50.0


class TestComputePremiumStrike:
    def test_otm_strike_for_low_iv_percentile(self):
        strike, kind = compute_premium_strike(
            underlying=100.0, iv_percentile=15.0,
            direction="BUY", is_call=True)
        assert kind == "OTM"
        assert strike > 100  # OTM call = above underlying

    def test_otm_put_strike_for_low_iv_percentile(self):
        strike, kind = compute_premium_strike(
            underlying=100.0, iv_percentile=20.0,
            direction="SELL", is_call=False)
        assert kind == "OTM"
        assert strike < 100  # OTM put = below underlying

    def test_atm_strike_for_moderate_iv_percentile(self):
        strike, kind = compute_premium_strike(
            underlying=100.0, iv_percentile=35.0,
            direction="BUY", is_call=True)
        assert kind == "ATM"
        assert strike == 100

    def test_itm_strike_for_high_iv_percentile(self):
        strike, kind = compute_premium_strike(
            underlying=100.0, iv_percentile=60.0,
            direction="BUY", is_call=True)
        assert kind == "ITM"
        assert strike < 100  # ITM call = below underlying

    def test_itm_put_for_high_iv_percentile(self):
        strike, kind = compute_premium_strike(
            underlying=100.0, iv_percentile=55.0,
            direction="SELL", is_call=False)
        assert kind == "ITM"
        assert strike > 100  # ITM put = above underlying


class TestCheckPremiumExit:
    def test_exit_signal_when_iv_expanded(self):
        tracker = PremiumDiscountTracker(lookback=100)
        for iv in [0.10, 0.11, 0.12, 0.13, 0.14, 0.15, 0.10, 0.11, 0.12, 0.13]:
            tracker.update("NIFTY", iv)
        pos = {"symbol": "NIFTY", "iv": 0.12, "entry_iv": 0.12}
        result = check_premium_exit(pos, current_iv=0.45, tracker=tracker)
        assert result["exit"] is True
        assert "iv_expansion" in result["reason"]

    def test_no_exit_when_iv_normal(self):
        tracker = PremiumDiscountTracker(lookback=100)
        for iv in [0.10, 0.11, 0.12, 0.13, 0.14, 0.15, 0.10, 0.11, 0.12, 0.13]:
            tracker.update("NIFTY", iv)
        pos = {"symbol": "NIFTY", "iv": 0.12, "entry_iv": 0.12}
        result = check_premium_exit(pos, current_iv=0.13, tracker=tracker)
        assert result["exit"] is False

    def test_no_exit_with_insufficient_history(self):
        tracker = PremiumDiscountTracker(lookback=100)
        tracker.update("NEW", 0.20)
        pos = {"symbol": "NEW", "iv": 0.20}
        result = check_premium_exit(pos, current_iv=0.50, tracker=tracker)
        # Not enough history → percentile 50 → no exit
        assert result["exit"] is False


class TestPremiumSnapshotSummary:
    def test_summary_contains_key_info(self):
        snap = PremiumSnapshot(
            symbol="NIFTY", current_iv=0.25, iv_percentile=20.0,
            premium_status="DEEP_DISCOUNT", recommended_strike="OTM",
            should_enter=True, should_exit=False, iv_history_count=50,
            discount_bonus=10.0, notes=["test note"])
        summary = snap.summary()
        assert "NIFTY" in summary
        assert "DEEP_DISCOUNT" in summary
        assert "OTM" in summary
        assert "YES" in summary
