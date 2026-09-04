"""Tests for Tiger Brain V16 Fund Announcement Brain."""

from __future__ import annotations

import pytest
from datetime import date

from backtest.tiger_fund_brain import (
    announce_fund_plan,
    classify_capital,
    size_trade_with_fund_brain,
    project_growth,
    TIER_MICRO, TIER_SMALL, TIER_MID, TIER_LARGE, TIER_WHALE,
    FundPlan,
)


class TestCapitalClassification:
    def test_micro_tier(self):
        assert classify_capital(10000) == TIER_MICRO
        assert classify_capital(25000) == TIER_MICRO

    def test_small_tier(self):
        assert classify_capital(25001) == TIER_SMALL
        assert classify_capital(100000) == TIER_SMALL

    def test_mid_tier(self):
        assert classify_capital(100001) == TIER_MID
        assert classify_capital(500000) == TIER_MID

    def test_large_tier(self):
        assert classify_capital(500001) == TIER_LARGE
        assert classify_capital(5000000) == TIER_LARGE

    def test_whale_tier(self):
        assert classify_capital(5000001) == TIER_WHALE
        assert classify_capital(10000000000) == TIER_WHALE


class TestFundAnnouncement:
    def test_micro_account_plan(self):
        plan = announce_fund_plan(10000)
        assert plan.tier == TIER_MICRO
        assert plan.max_trades_today == 2
        assert plan.risk_per_trade_pct == 3.0
        assert plan.risk_per_trade_rupees == 300.0  # 3% of 10000
        assert plan.growth_target_monthly_pct == 30.0
        assert len(plan.notes) > 0

    def test_small_account_plan(self):
        plan = announce_fund_plan(100000)
        assert plan.tier == TIER_SMALL
        assert plan.risk_per_trade_pct == 2.0
        assert plan.risk_per_trade_rupees == 2000.0

    def test_whale_account_plan(self):
        plan = announce_fund_plan(10000000000)
        assert plan.tier == TIER_WHALE
        assert plan.risk_per_trade_pct == 0.5

    def test_plan_summary_string(self):
        plan = announce_fund_plan(50000)
        summary = plan.summary()
        assert "₹50,000" in summary
        assert "SMALL" in summary

    def test_invalid_capital_raises(self):
        with pytest.raises(ValueError):
            announce_fund_plan(-1000)
        with pytest.raises(ValueError):
            announce_fund_plan(0)

    def test_available_capital_default(self):
        plan = announce_fund_plan(100000)
        assert plan.available_capital == 100000

    def test_available_capital_custom(self):
        plan = announce_fund_plan(100000, available_capital=80000)
        assert plan.available_capital == 80000


class TestTradeSizing:
    def test_basic_sizing(self):
        plan = announce_fund_plan(100000)
        result = size_trade_with_fund_brain(
            plan, entry_premium=50, stop_premium=30, lot_sz=100)
        assert result["quantity"] > 0
        assert result["lots"] > 0
        assert result["max_loss"] > 0
        assert result["reason"] == "sized_by_fund_brain"

    def test_micro_account_forces_one_lot(self):
        plan = announce_fund_plan(10000)
        result = size_trade_with_fund_brain(
            plan, entry_premium=20, stop_premium=10, lot_sz=100)
        assert result["lots"] == 1  # MICRO always 1 lot

    def test_risk_capped(self):
        plan = announce_fund_plan(100000)  # risk = ₹2000
        result = size_trade_with_fund_brain(
            plan, entry_premium=100, stop_premium=90, lot_sz=100)
        # loss_per_lot = 10 * 100 = 1000
        # risk_budget = 2000 → lots_by_risk = 2
        assert result["max_loss"] <= 2000 + 1  # within risk budget

    def test_delivery_trade_less_capital(self):
        plan = announce_fund_plan(100000)
        intraday = size_trade_with_fund_brain(
            plan, entry_premium=50, stop_premium=30, lot_sz=100, is_delivery=False)
        delivery = size_trade_with_fund_brain(
            plan, entry_premium=50, stop_premium=30, lot_sz=100, is_delivery=True)
        # Delivery should use less capital (70% risk budget)
        assert delivery["max_loss"] <= intraday["max_loss"] + 1

    def test_exposure_limit_blocks(self):
        plan = announce_fund_plan(100000)
        # current_exposure already at max
        result = size_trade_with_fund_brain(
            plan, entry_premium=50, stop_premium=30, lot_sz=100,
            current_exposure=plan.max_total_exposure_rupees)
        assert result["quantity"] == 0
        assert "exposure" in result["reason"]

    def test_no_plan_blocks(self):
        result = size_trade_with_fund_brain(
            None, entry_premium=50, stop_premium=30, lot_sz=100)
        assert result["quantity"] == 0

    def test_invalid_premium_blocks(self):
        plan = announce_fund_plan(100000)
        result = size_trade_with_fund_brain(
            plan, entry_premium=0, stop_premium=30, lot_sz=100)
        assert result["quantity"] == 0


class TestGrowthProjection:
    def test_micro_growth_3months(self):
        projections = project_growth(10000, months=3)
        assert len(projections) == 3
        # 30% monthly: 10000 → 13000 → 16900 → 21970
        assert projections[0]["ending_capital"] == 13000
        assert projections[2]["ending_capital"] > 20000

    def test_custom_return_rate(self):
        projections = project_growth(100000, months=3, monthly_return_pct=10.0)
        assert projections[0]["ending_capital"] == 110000
        assert projections[2]["ending_capital"] > 130000

    def test_whale_uses_tier_default(self):
        projections = project_growth(10000000000, months=1)
        # Whale tier = 8% monthly
        assert projections[0]["return_pct"] == 8.0
