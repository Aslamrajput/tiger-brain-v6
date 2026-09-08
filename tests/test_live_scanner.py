"""Tests for real-time live scanner (बैकटेस्ट से इन्डिपेंडेंट).

Covers:
  1. scan_live_signals — empty data, off-hours, daily quota
  2. _latest_15m_index — latest closed bar
  3. Real signal flow — 7 brains on latest bar
"""
import sys
from datetime import datetime, time, date
from pathlib import Path
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


# ============================================================
# _latest_15m_index
# ============================================================

class TestLatest15mIndex:
    def test_empty_df_returns_negative(self):
        from automation.live_scanner import _latest_15m_index
        assert _latest_15m_index(None, datetime.now()) == -1
        assert _latest_15m_index(pd.DataFrame(), datetime.now()) == -1

    def test_returns_latest_closed_bar(self):
        from automation.live_scanner import _latest_15m_index
        idx = pd.date_range("2026-09-09 09:15", periods=20, freq="15min")
        df = pd.DataFrame({
            "open": 100, "high": 101, "low": 99, "close": 100.5,
            "volume": 1000}, index=idx)
        # 10:30 → latest closed bar is 10:15 (index 5)
        i = _latest_15m_index(df, datetime(2026, 9, 9, 10, 30))
        assert i == 5

    def test_before_first_bar_returns_last(self):
        from automation.live_scanner import _latest_15m_index
        idx = pd.date_range("2026-09-09 09:15", periods=5, freq="15min")
        df = pd.DataFrame({"open": 100, "high": 101, "low": 99,
                           "close": 100.5, "volume": 1000}, index=idx)
        # 08:00 — before first bar
        i = _latest_15m_index(df, datetime(2026, 9, 9, 8, 0))
        assert i == 4  # falls back to last bar


# ============================================================
# scan_live_signals
# ============================================================

class TestScanLiveSignals:
    def test_empty_data_map_returns_empty(self):
        from automation.live_scanner import scan_live_signals
        signals = scan_live_signals({}, None, None,
                                     now=datetime(2026, 9, 9, 10, 30))
        assert signals == []

    def test_off_hours_returns_empty(self):
        """Night (02:00) — no session active → 0 signals."""
        from automation.live_scanner import scan_live_signals
        signals = scan_live_signals({}, None, None,
                                     now=datetime(2026, 9, 9, 2, 0))
        assert signals == []

    def test_daily_quota_full_returns_empty(self):
        """Brain 4: daily quota full → no new signals."""
        from automation.live_scanner import scan_live_signals
        idx = pd.date_range("2026-09-09 09:15", periods=50, freq="15min")
        df = pd.DataFrame({
            "open": np.random.uniform(99, 101, 50),
            "high": np.random.uniform(101, 102, 50),
            "low": np.random.uniform(98, 99, 50),
            "close": np.random.uniform(99, 101, 50),
            "volume": np.random.randint(500, 2000, 50),
        }, index=idx)
        data_map = {"NIFTY": df}
        # quota already 5/5
        signals = scan_live_signals(
            data_map, None, None,
            now=datetime(2026, 9, 9, 10, 30),
            daily_entries_taken=5, max_entries_per_day=5)
        assert signals == []

    def test_short_df_skipped(self):
        """DF with < 40 bars → skipped (need history for zones)."""
        from automation.live_scanner import scan_live_signals
        idx = pd.date_range("2026-09-09 09:15", periods=20, freq="15min")
        df = pd.DataFrame({
            "open": 100, "high": 101, "low": 99, "close": 100.5,
            "volume": 1000}, index=idx)
        data_map = {"NIFTY": df}
        signals = scan_live_signals(
            data_map, None, None,
            now=datetime(2026, 9, 9, 10, 30))
        assert signals == []

    def test_returns_signal_dict_structure(self):
        """When a signal is generated, it has the right fields."""
        from automation.live_scanner import scan_live_signals
        # Mock find_tiger_brain_entry_15m to return a signal
        mock_signal = {
            "direction": "BUY",
            "setup_score": 80.0,
            "score_details": "zone+vol",
            "strike_kind": "ATM",
            "entry_1m_ts": pd.Timestamp("2026-09-08 10:15"),
        }
        with patch("automation.live_scanner.find_tiger_brain_entry_15m",
                   return_value=mock_signal):
            # DF with 50 bars — 09:15 से नहीं, पहले दिन से शुरू
            # ताकि 10:30 पर latest bar index 40+ हो
            idx = pd.date_range("2026-09-07 09:15", periods=50, freq="15min")
            df = pd.DataFrame({
                "open": np.random.uniform(99, 101, 50),
                "high": np.random.uniform(101, 102, 50),
                "low": np.random.uniform(98, 99, 50),
                "close": np.random.uniform(99, 101, 50),
                "volume": np.random.randint(500, 2000, 50),
            }, index=idx)
            data_map = {"NIFTY": df}
            signals = scan_live_signals(
                data_map, None, None,
                now=datetime(2026, 9, 8, 10, 30))
            assert len(signals) >= 1
            s = signals[0]
            assert "symbol" in s
            assert "segment" in s
            assert "scan_time" in s
            assert s["exit_ts"] is None
            assert s["setup_score"] >= 75

    def test_below_threshold_signal_filtered(self):
        """Signal below session threshold → filtered out."""
        from automation.live_scanner import scan_live_signals
        mock_signal = {
            "direction": "BUY",
            "setup_score": 60.0,  # below threshold
            "score_details": "weak",
            "strike_kind": "ATM",
        }
        with patch("automation.live_scanner.find_tiger_brain_entry_15m",
                   return_value=mock_signal):
            idx = pd.date_range("2026-09-08 09:15", periods=50, freq="15min")
            df = pd.DataFrame({
                "open": 100, "high": 101, "low": 99, "close": 100.5,
                "volume": 1000}, index=idx)
            signals = scan_live_signals(
                {"NIFTY": df}, None, None,
                now=datetime(2026, 9, 8, 10, 30))
            assert signals == []

    def test_no_signal_returns_empty(self):
        """find_tiger_brain_entry returns None → no signals."""
        from automation.live_scanner import scan_live_signals
        with patch("automation.live_scanner.find_tiger_brain_entry_15m",
                   return_value=None):
            idx = pd.date_range("2026-09-08 09:15", periods=50, freq="15min")
            df = pd.DataFrame({
                "open": 100, "high": 101, "low": 99, "close": 100.5,
                "volume": 1000}, index=idx)
            signals = scan_live_signals(
                {"NIFTY": df}, None, None,
                now=datetime(2026, 9, 8, 10, 30))
            assert signals == []
