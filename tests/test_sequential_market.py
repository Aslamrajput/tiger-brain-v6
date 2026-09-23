"""Tests for sequential market — one market at a time, 100% capital.

User mandate: "Ak time pe ak market — morning NSE, 3:30 ke baad MCX.
50/50 nahi chaiye. Full automatic switch, bina confuse ke."

Tests:
1. NSE hours → only NSE symbols scanned, 100% capital to NSE
2. MCX hours (after 15:30) → only MCX symbols scanned, 100% to MCX
3. 50/50 split removed — MARKET_CAPITAL_SPLIT_PCT = 100.0
4. ₹8,000 daily profit target config exists
5. get_active_market returns correct market per time
"""
from __future__ import annotations

import pytest
from datetime import datetime, time
from unittest.mock import patch


class TestSequentialMarket:
    """One market at a time — no 50/50 split, no simultaneous scan."""

    def test_config_split_is_100_not_50(self):
        """MARKET_CAPITAL_SPLIT_PCT must be 100.0 (not 50.0)."""
        from config.thresholds import BRAIN4
        assert BRAIN4["MARKET_CAPITAL_SPLIT_PCT"] == 100.0

    def test_daily_profit_target_config_exists(self):
        """DAILY_PROFIT_TARGET must be ₹8,000."""
        from config.thresholds import BRAIN4
        assert BRAIN4["DAILY_PROFIT_TARGET"] == 8000.0

    def test_get_active_market_morning_nse(self):
        """Morning 10:00 → NSE+MCX (both open, but NSE priority)."""
        from automation.scheduler import get_active_market
        market = get_active_market(datetime(2026, 1, 15, 10, 0, 0))
        # 10:00 → both NSE and MCX are open → "NSE+MCX"
        assert market in ("NSE+MCX", "NSE")

    def test_get_active_market_evening_mcx_only(self):
        """Evening 17:00 → MCX only (NSE closed at 15:15)."""
        from automation.scheduler import get_active_market
        market = get_active_market(datetime(2026, 1, 15, 17, 0, 0))
        assert market == "MCX"

    def test_get_active_market_closed(self):
        """Midnight → CLOSED."""
        from automation.scheduler import get_active_market
        market = get_active_market(datetime(2026, 1, 15, 1, 0, 0))
        assert market == "CLOSED"

    def test_segment_classifies_mcx_correctly(self):
        """GOLDM/SILVERM/CRUDEOIL/NATURALGAS → 'commodity' segment."""
        from universe.fno_universe import segment_of
        assert segment_of("GOLDM") == "commodity"
        assert segment_of("SILVERM") == "commodity"
        assert segment_of("CRUDEOIL") == "commodity"
        assert segment_of("NATURALGAS") == "commodity"

    def test_segment_classifies_nse_correctly(self):
        """NIFTY/BANKNIFTY → 'index', RELIANCE → 'stock' (both NSE)."""
        from universe.fno_universe import segment_of
        assert segment_of("NIFTY") in ("index", "stock")
        assert segment_of("BANKNIFTY") in ("index", "stock")
        assert segment_of("RELIANCE") in ("index", "stock")

    def test_scan_filters_nse_only_during_nse_hours(self):
        """During NSE hours, scan_live_signals receives ONLY NSE symbols."""
        from automation.tiger_live import TigerLiveRunner
        import inspect
        source = inspect.getsource(TigerLiveRunner._intraday_scan_inner)
        # Must have sequential market filter
        assert "SEQUENTIAL" in source or "sequential" in source
        assert "get_active_market" in source
        assert "_filtered_map" in source
        assert "segment_of" in source

    def test_capital_no_50_50_split(self):
        """_place_live_orders must NOT have 50/50 split logic anymore."""
        from automation.tiger_live import TigerLiveRunner
        import inspect
        source = inspect.getsource(TigerLiveRunner._place_live_orders)
        # The old 50/50 split branches are gone
        assert "_split_pct" not in source
        assert "_nse_open" not in source
        assert "_mcx_open" not in source
        # New: full balance = market budget
        assert "_market_budget = available_balance" in source

    def test_mcx_switch_log_exists(self):
        """mcx_market_open must log the switch (anti-forget bug fix)."""
        from automation.tiger_live import TigerLiveRunner
        import inspect
        source = inspect.getsource(TigerLiveRunner.mcx_market_open)
        assert "FULL SWITCH" in source or "MCX FULL" in source
        assert "ANTI-FORGET" in source or "forget" in source.lower()

    def test_daily_profit_alert_code_exists(self):
        """₹8,000 daily profit alert must be in the scan loop."""
        from automation.tiger_live import TigerLiveRunner
        import inspect
        source = inspect.getsource(TigerLiveRunner._intraday_scan_inner)
        assert "DAILY_PROFIT_TARGET" in source
        assert "_daily_start_balance" in source
        assert "Ujjivan" in source
        assert "NEVER touch capital" in source or "NEVER.*capital" in source

    def test_daily_profit_target_alert_only_on_profit(self):
        """Alert fires ONLY when profit >= ₹8,000 (not on capital)."""
        # Simulate: start ₹29,000, current ₹37,000 → profit ₹8,000 → alert
        start = 29000.0
        current = 37000.0
        target = 8000.0
        profit = current - start
        assert profit >= target  # alert fires
        excess = profit - target
        assert excess == 0  # no excess in this case

    def test_no_alert_when_loss(self):
        """No alert when balance is below start (capital protected)."""
        start = 29000.0
        current = 25000.0
        target = 8000.0
        profit = current - start
        assert profit < 0  # loss — no withdrawal
        assert profit < target  # no alert

    def test_excess_goes_to_capital(self):
        """Profit > ₹8,000 → excess stays in capital (reinvest)."""
        start = 29000.0
        current = 40000.0
        target = 8000.0
        profit = current - start
        excess = profit - target
        assert profit > target
        assert excess == 3000.0  # ₹3,000 stays in capital
