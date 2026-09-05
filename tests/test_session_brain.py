"""Tests for Tiger Brain V18 Session Commander (Brain 7)."""

from __future__ import annotations

from datetime import time

import pytest

from backtest.tiger_session_brain import (
    SessionConfig,
    SESSION_SCHEDULE,
    SESSION_MORNING_BURST,
    SESSION_TREND_HUNT,
    SESSION_DISCOUNT_BUY,
    SESSION_POWER_HOUR,
    SESSION_COMMODITY_OPEN,
    SESSION_NIGHT_RUSH,
    SESSION_SQUARE_OFF,
    get_current_session,
    get_session_score_threshold,
    get_session_trade_quota,
    get_session_capital_pct,
    get_session_preferred_strike,
    should_force_hunt,
    HuntStatus,
    get_session_label,
    print_session_schedule,
)


class TestSessionDetection:
    def test_morning_burst_session(self):
        session = get_current_session(time(9, 30), "nse")
        assert session is not None
        assert session.name == SESSION_MORNING_BURST

    def test_trend_hunt_session(self):
        session = get_current_session(time(11, 0), "nse")
        assert session is not None
        assert session.name == SESSION_TREND_HUNT

    def test_discount_buy_session(self):
        session = get_current_session(time(13, 30), "nse")
        assert session is not None
        assert session.name == SESSION_DISCOUNT_BUY

    def test_power_hour_session(self):
        session = get_current_session(time(14, 45), "nse")
        assert session is not None
        assert session.name == SESSION_POWER_HOUR

    def test_commodity_open_session(self):
        session = get_current_session(time(18, 0), "mcx")
        assert session is not None
        assert session.name == SESSION_COMMODITY_OPEN

    def test_night_rush_session(self):
        session = get_current_session(time(21, 0), "mcx")
        assert session is not None
        assert session.name == SESSION_NIGHT_RUSH

    def test_square_off_session(self):
        session = get_current_session(time(23, 10), "mcx")
        assert session is not None
        assert session.name == SESSION_SQUARE_OFF

    def test_off_hours_nse_evening(self):
        # NSE is closed after 15:15
        session = get_current_session(time(16, 0), "nse")
        assert session is None

    def test_off_hours_morning_before_open(self):
        session = get_current_session(time(8, 0), "nse")
        assert session is None


class TestSessionParameters:
    def test_morning_burst_is_aggressive(self):
        session = get_current_session(time(9, 30), "nse")
        assert session.aggressiveness == "HIGH"
        assert session.max_trades_this_session == 5
        assert session.capital_allocation_pct == 35.0
        assert session.preferred_strike == "ITM"

    def test_discount_buy_uses_otm(self):
        session = get_current_session(time(13, 30), "nse")
        assert session.preferred_strike == "OTM"
        assert session.score_threshold == 75.0  # lower — IV discount compensates

    def test_power_hour_is_max_aggression(self):
        session = get_current_session(time(14, 45), "nse")
        assert session.aggressiveness == "MAX"

    def test_night_rush_is_aggressive(self):
        session = get_current_session(time(21, 0), "mcx")
        assert session.aggressiveness == "HIGH"
        assert session.max_trades_this_session == 5

    def test_square_off_no_entries(self):
        session = get_current_session(time(23, 10), "mcx")
        assert session.score_threshold == 999.0
        assert session.max_trades_this_session == 0


class TestSessionHelpers:
    def test_get_session_score_threshold(self):
        assert get_session_score_threshold(time(9, 30), "nse") == 78.0
        assert get_session_score_threshold(time(13, 30), "nse") == 75.0
        assert get_session_score_threshold(time(8, 0), "nse") == 999.0  # off-hours

    def test_get_session_trade_quota(self):
        assert get_session_trade_quota(time(9, 30), "nse") == 5
        assert get_session_trade_quota(time(23, 10), "mcx") == 0
        assert get_session_trade_quota(time(8, 0), "nse") == 0  # off-hours

    def test_get_session_capital_pct(self):
        assert get_session_capital_pct(time(9, 30), "nse") == 35.0
        assert get_session_capital_pct(time(23, 10), "mcx") == 0.0

    def test_get_session_preferred_strike(self):
        assert get_session_preferred_strike(time(13, 30), "nse") == "OTM"
        assert get_session_preferred_strike(time(9, 30), "nse") == "ITM"
        assert get_session_preferred_strike(time(8, 0), "nse") == "ATM"  # default


class TestHuntStatus:
    def test_empty_hunt(self):
        h = HuntStatus(date="2026-09-04")
        assert h.is_empty_hunt is True
        assert h.needs_aggressive_hunt is True
        assert h.total_trades == 0

    def test_record_nse_trade(self):
        h = HuntStatus(date="2026-09-04")
        h.record_trade("stock", pnl=5000)
        assert h.nse_trades == 1
        assert h.mcx_trades == 0
        assert h.total_trades == 1
        assert h.nse_profit == 5000
        assert h.is_empty_hunt is False

    def test_record_mcx_trade(self):
        h = HuntStatus(date="2026-09-04")
        h.record_trade("commodity", pnl=10000)
        assert h.mcx_trades == 1
        assert h.nse_trades == 0
        assert h.total_trades == 1
        assert h.mcx_profit == 10000

    def test_needs_aggressive_with_few_trades(self):
        h = HuntStatus(date="2026-09-04")
        h.record_trade("stock")
        h.record_trade("stock")
        assert h.needs_aggressive_hunt is True  # < 3 trades
        h.record_trade("stock")
        assert h.needs_aggressive_hunt is False  # 3 trades

    def test_summary(self):
        h = HuntStatus(date="2026-09-04")
        h.record_trade("stock", 5000)
        h.record_trade("commodity", 10000)
        s = h.summary()
        assert "NSE: 1" in s
        assert "MCX: 1" in s
        assert "Total: 2" in s


class TestForceHunt:
    def test_no_force_hunt_when_enough_trades(self):
        h = HuntStatus(date="2026-09-04")
        for _ in range(5):
            h.record_trade("stock")
        force, threshold, reason = should_force_hunt(time(14, 45), h, "nse")
        assert force is False

    def test_force_hunt_nse_power_hour_few_trades(self):
        h = HuntStatus(date="2026-09-04")
        h.record_trade("stock")  # only 1 trade
        force, threshold, reason = should_force_hunt(time(14, 45), h, "nse")
        assert force is True
        assert threshold < 78.0  # lowered
        assert "NSE closing" in reason

    def test_force_hunt_mcx_night_rush_few_trades(self):
        h = HuntStatus(date="2026-09-04")
        h.record_trade("stock")
        force, threshold, reason = should_force_hunt(time(20, 30), h, "mcx")
        assert force is True
        assert threshold < 78.0
        assert "night rush" in reason

    def test_force_hunt_mcx_near_close_zero_trades(self):
        h = HuntStatus(date="2026-09-04")
        # Zero trades → Tiger MUST hunt
        force, threshold, reason = should_force_hunt(time(22, 30), h, "mcx")
        assert force is True
        assert threshold == 65.0
        assert "ZERO" in reason or "last chance" in reason

    def test_no_force_hunt_during_morning(self):
        h = HuntStatus(date="2026-09-04")
        # Morning — no force hunt even with 0 trades
        force, threshold, reason = should_force_hunt(time(9, 30), h, "nse")
        assert force is False


class TestSessionLabelAndSchedule:
    def test_session_label_morning(self):
        label = get_session_label(time(9, 30), "nse")
        assert "MORNING" in label

    def test_session_label_discount(self):
        label = get_session_label(time(13, 30), "nse")
        assert "DISCOUNT" in label

    def test_session_label_off_hours(self):
        label = get_session_label(time(8, 0), "nse")
        assert "OFF" in label

    def test_session_label_transition(self):
        label = get_session_label(time(16, 0), "nse")
        assert "TRANSITION" in label

    def test_print_session_schedule(self):
        output = print_session_schedule()
        assert "TIGER SESSION COMMANDER" in output
        assert "MORNING BURST" in output
        assert "NIGHT RUSH" in output
        assert "BINA SHIKAR LIYE GHAR NAHI" in output


class TestSessionConfig:
    def test_is_active_within_window(self):
        session = SessionConfig(
            name="TEST", label="Test", time_start=time(10, 0),
            time_end=time(11, 0), segment_focus="nse", score_threshold=75,
            max_trades_this_session=3, capital_allocation_pct=20,
            preferred_strike="ATM", aggressiveness="MEDIUM",
            description="test")
        assert session.is_active(time(10, 0)) is True
        assert session.is_active(time(10, 30)) is True
        assert session.is_active(time(10, 59)) is True

    def test_is_active_outside_window(self):
        session = SessionConfig(
            name="TEST", label="Test", time_start=time(10, 0),
            time_end=time(11, 0), segment_focus="nse", score_threshold=75,
            max_trades_this_session=3, capital_allocation_pct=20,
            preferred_strike="ATM", aggressiveness="MEDIUM",
            description="test")
        assert session.is_active(time(9, 59)) is False
        assert session.is_active(time(11, 0)) is False
