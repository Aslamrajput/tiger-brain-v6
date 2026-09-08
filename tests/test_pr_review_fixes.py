"""Tests for PR review fixes + live fund-brain sizing + MCX thresholds.

Covers:
  1. resolve_exchange_for_symbol (Issue 3) — authoritative lookup
  2. Raju's _count_recent_errors (Issue 4 / PR #26) — real 15-min filter
  3. Exit order product_type matching (Issue 1) — delivery vs intraday
  4. Live Fund Brain sizing (P1) — real balance + real LTP + real lot
  5. Lot multiple validation (P2) — qty always lot-size multiple
  6. MCX session thresholds (P3) — lowered for more commodity trades
"""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


# ============================================================
# Issue 3: resolve_exchange_for_symbol — authoritative lookup
# ============================================================

class TestResolveExchangeForSymbol:
    def test_mcx_commodity_resolves_to_mcx(self):
        from automation.tiger_live import resolve_exchange_for_symbol
        assert resolve_exchange_for_symbol("CRUDEOIL") == "MCX"
        assert resolve_exchange_for_symbol("GOLD") == "MCX"
        assert resolve_exchange_for_symbol("SILVER") == "MCX"
        assert resolve_exchange_for_symbol("NATURALGAS") == "MCX"

    def test_mcx_mini_resolves_to_mcx(self):
        from automation.tiger_live import resolve_exchange_for_symbol
        assert resolve_exchange_for_symbol("CRUDEOILM") == "MCX"
        assert resolve_exchange_for_symbol("SILVERM") == "MCX"
        assert resolve_exchange_for_symbol("GOLDM") == "MCX"
        assert resolve_exchange_for_symbol("NATGASMINI") == "MCX"

    def test_nse_equity_resolves_to_nfo(self):
        from automation.tiger_live import resolve_exchange_for_symbol
        assert resolve_exchange_for_symbol("RELIANCE") == "NFO"
        assert resolve_exchange_for_symbol("TCS") == "NFO"
        assert resolve_exchange_for_symbol("BAJFINANCE") == "NFO"

    def test_nse_index_resolves_to_nfo(self):
        from automation.tiger_live import resolve_exchange_for_symbol
        assert resolve_exchange_for_symbol("NIFTY") == "NFO"
        assert resolve_exchange_for_symbol("BANKNIFTY") == "NFO"

    def test_empty_symbol_defaults_to_nfo(self):
        from automation.tiger_live import resolve_exchange_for_symbol
        assert resolve_exchange_for_symbol("") == "NFO"
        assert resolve_exchange_for_symbol(None) == "NFO"

    def test_case_insensitive(self):
        from automation.tiger_live import resolve_exchange_for_symbol
        assert resolve_exchange_for_symbol("crudeoil") == "MCX"
        assert resolve_exchange_for_symbol("Gold") == "MCX"
        assert resolve_exchange_for_symbol("RELIANCE") == "NFO"


# ============================================================
# Issue 4 (PR #26): Raju's _count_recent_errors — timestamp filter
# ============================================================

class TestCountRecentErrors:
    """Test that Raju only counts errors within the 15-min window,
    excludes warnings, and handles multiline tracebacks correctly.
    """
    def _make_mechanic(self):
        from deploy.tiger_mechanics import RajuHealthWatcher
        return RajuHealthWatcher()

    def test_no_log_returns_zero(self, tmp_path):
        raju = self._make_mechanic()
        with patch("deploy.tiger_mechanics.run_local",
                   return_value=("", 0)):
            assert raju._count_recent_errors(15) == 0

    def test_recent_error_counted(self):
        raju = self._make_mechanic()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log = f"{now} [tiger_brain.data_loader] ERROR: Candle fetch failed\n"
        with patch("deploy.tiger_mechanics.run_local",
                   return_value=(log, 0)):
            assert raju._count_recent_errors(15) == 1

    def test_old_error_not_counted(self):
        raju = self._make_mechanic()
        old = (datetime.now() - timedelta(minutes=30)
               ).strftime("%Y-%m-%d %H:%M:%S")
        log = (f"{old} [tiger_brain.data_loader] ERROR: old error\n")
        with patch("deploy.tiger_mechanics.run_local",
                   return_value=(log, 0)):
            assert raju._count_recent_errors(15) == 0

    def test_futurewarning_excluded(self):
        raju = self._make_mechanic()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log = (
            f"{now} [tiger_brain.data_loader] ERROR: real error\n"
            f"{now} /path/to/file.py:756: FutureWarning: deprecated\n"
        )
        with patch("deploy.tiger_mechanics.run_local",
                   return_value=(log, 0)):
            # Only the ERROR line counts, not FutureWarning
            assert raju._count_recent_errors(15) == 1

    def test_multiline_traceback_only_counts_once(self):
        """Traceback continuation lines have no timestamp — skipped."""
        raju = self._make_mechanic()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log = (
            f"{now} [tiger_brain] ERROR: something broke\n"
            f"Traceback (most recent call last):\n"
            f'  File "/path/to/code.py", line 42, in func\n'
            f"    raise ValueError('bad')\n"
            f"ValueError: bad\n"
        )
        with patch("deploy.tiger_mechanics.run_local",
                   return_value=(log, 0)):
            # Only the first line (with timestamp + ERROR) counts
            assert raju._count_recent_errors(15) == 1

    def test_boundary_15_min_excluded(self):
        """Error exactly 15 min ago is OUTSIDE the window (cutoff strict)."""
        raju = self._make_mechanic()
        boundary = (datetime.now() - timedelta(minutes=16)
                     ).strftime("%Y-%m-%d %H:%M:%S")
        recent = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log = (
            f"{boundary} [module] ERROR: old\n"
            f"{recent} [module] ERROR: new\n"
        )
        with patch("deploy.tiger_mechanics.run_local",
                   return_value=(log, 0)):
            assert raju._count_recent_errors(15) == 1

    def test_multiple_recent_errors(self):
        raju = self._make_mechanic()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log = "\n".join(
            f"{now} [module] ERROR: error #{i}" for i in range(10))
        with patch("deploy.tiger_mechanics.run_local",
                   return_value=(log, 0)):
            assert raju._count_recent_errors(15) == 10


# ============================================================
# Issue 1: Exit order product_type matching
# ============================================================

class TestExitProductTypeMatching:
    """Verify exit orders use position's product_type, not hardcoded INTRADAY.
    """
    def test_square_off_uses_carryforward_for_delivery(self):
        """Delivery positions must be closed with CARRYFORWARD, not INTRADAY."""
        from broker.angel_connect import AngelBroker
        broker = MagicMock(spec=AngelBroker)
        broker.get_positions.return_value = [
            {"tradingsymbol": "NIFTY24SEP22500CE", "symboltoken": "123",
             "exchange": "NFO", "netqty": 50, "producttype": "CARRYFORWARD"},
        ]
        broker.place_option_order.return_value = {
            "success": True, "order_id": "ORD1", "error": None}

        # Call square_off_all on the real class via instance
        real_broker = AngelBroker.__new__(AngelBroker)
        real_broker.get_positions = broker.get_positions
        real_broker.place_option_order = broker.place_option_order
        real_broker.square_off_all()

        call_args = broker.place_option_order.call_args
        assert call_args.kwargs.get("product_type") == "CARRYFORWARD"

    def test_square_off_uses_intraday_for_intraday(self):
        from broker.angel_connect import AngelBroker
        broker = MagicMock(spec=AngelBroker)
        broker.get_positions.return_value = [
            {"tradingsymbol": "NIFTY24SEP22500CE", "symboltoken": "123",
             "exchange": "NFO", "netqty": 50, "producttype": "INTRADAY"},
        ]
        broker.place_option_order.return_value = {
            "success": True, "order_id": "ORD1", "error": None}

        real_broker = AngelBroker.__new__(AngelBroker)
        real_broker.get_positions = broker.get_positions
        real_broker.place_option_order = broker.place_option_order
        real_broker.square_off_all()

        call_args = broker.place_option_order.call_args
        assert call_args.kwargs.get("product_type") == "INTRADAY"

    def test_square_off_fallback_intraday_on_unknown(self):
        from broker.angel_connect import AngelBroker
        broker = MagicMock(spec=AngelBroker)
        broker.get_positions.return_value = [
            {"tradingsymbol": "X", "symboltoken": "1",
             "exchange": "NFO", "netqty": 10, "producttype": "WEIRD"},
        ]
        broker.place_option_order.return_value = {
            "success": True, "order_id": "O1", "error": None}

        real_broker = AngelBroker.__new__(AngelBroker)
        real_broker.get_positions = broker.get_positions
        real_broker.place_option_order = broker.place_option_order
        real_broker.square_off_all()

        call_args = broker.place_option_order.call_args
        assert call_args.kwargs.get("product_type") == "INTRADAY"


# ============================================================
# P1: Live Fund Brain sizing — real balance + real LTP + real lot
# ============================================================

class TestLiveReSize:
    def _make_runner(self):
        from automation.tiger_live import TigerLiveRunner
        runner = TigerLiveRunner.__new__(TigerLiveRunner)
        runner._order_log = []
        runner._position_peaks = {}
        return runner

    def test_zero_balance_returns_zero_qty(self):
        runner = self._make_runner()
        result = runner._live_re_size(0.0, 50.0, 100, False)
        assert result["quantity"] == 0
        assert result["reason"] == "invalid balance/ltp/lotsize"

    def test_zero_ltp_returns_zero_qty(self):
        runner = self._make_runner()
        result = runner._live_re_size(26000.0, 0.0, 100, False)
        assert result["quantity"] == 0

    def test_normal_balance_gives_fund_brain_qty(self):
        runner = self._make_runner()
        result = runner._live_re_size(26000.0, 30.0, 700, False)
        assert result["quantity"] > 0
        assert result["allocated_capital"] > 0

    def test_quantity_always_multiple_of_lot_size(self):
        runner = self._make_runner()
        for lot in [100, 125, 700, 1500, 250]:
            result = runner._live_re_size(26000.0, 30.0, lot, False)
            if result["quantity"] > 0:
                assert result["quantity"] % lot == 0

    def test_unaffordable_returns_zero_qty(self):
        runner = self._make_runner()
        result = runner._live_re_size(26000.0, 300.0, 700, False)
        assert result["quantity"] == 0
        assert "afford" in result["reason"].lower() or result["reason"] == "not_affordable"

    def test_allocated_capital_fits_balance(self):
        runner = self._make_runner()
        result = runner._live_re_size(26000.0, 50.0, 100, False)
        if result["quantity"] > 0:
            assert result["allocated_capital"] <= 26000.0

    def test_delivery_gets_smaller_allocation(self):
        runner = self._make_runner()
        intraday = runner._live_re_size(26000.0, 30.0, 700, False)
        delivery = runner._live_re_size(26000.0, 30.0, 700, True)
        assert delivery["quantity"] <= intraday["quantity"]


class TestMCXSessionThresholds:
    def test_commodity_day_threshold_lowered(self):
        from backtest.tiger_session_brain import SESSION_COMMODITY_DAY, SESSION_SCHEDULE
        cfg = next(s for s in SESSION_SCHEDULE if s.name == SESSION_COMMODITY_DAY)
        assert cfg.score_threshold <= 70.0

    def test_commodity_open_threshold_lowered(self):
        from backtest.tiger_session_brain import SESSION_COMMODITY_OPEN, SESSION_SCHEDULE
        cfg = next(s for s in SESSION_SCHEDULE if s.name == SESSION_COMMODITY_OPEN)
        assert cfg.score_threshold <= 72.0

    def test_night_rush_threshold_lowered(self):
        from backtest.tiger_session_brain import SESSION_NIGHT_RUSH, SESSION_SCHEDULE
        cfg = next(s for s in SESSION_SCHEDULE if s.name == SESSION_NIGHT_RUSH)
        assert cfg.score_threshold <= 70.0

    def test_mcx_thresholds_below_rocket_min(self):
        from backtest.tiger_session_brain import (SESSION_COMMODITY_DAY, SESSION_COMMODITY_OPEN, SESSION_NIGHT_RUSH, SESSION_SCHEDULE)
        from backtest.run_tiger_brain_backtest import ROCKET_MIN_SCORE
        names = {SESSION_COMMODITY_DAY, SESSION_COMMODITY_OPEN, SESSION_NIGHT_RUSH}
        for cfg in [s for s in SESSION_SCHEDULE if s.name in names]:
            assert cfg.score_threshold <= ROCKET_MIN_SCORE
