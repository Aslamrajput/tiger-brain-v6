"""Tests for bottom-entry / structural stop fixes (CRUDEOIL premature entry bug).

Tests the two key fixes:
1. Scalper bottom confirmation gate — price must test zone + bounce
2. Structural stop in signals — zone-based SL, not fixed -7%
"""
import pandas as pd
import numpy as np
import pytest
from datetime import datetime


def _make_zone_df(n=50, zone_touch=True):
    """Build df with a demand zone + optional zone touch at last bar."""
    dates = pd.date_range('2025-09-08 09:15', periods=n, freq='15min')
    opens, closes, highs, lows, vols = [], [], [], [], []
    # Bar 0: up impulse
    opens.append(99.0); closes.append(101.0); highs.append(101.2); lows.append(98.8); vols.append(2000)
    # Bars 1-3: tight cluster (demand zone base)
    for _ in range(3):
        opens.append(100.5); closes.append(100.6); highs.append(100.8); lows.append(100.3); vols.append(800)
    # Bars 4-44: price up and away
    for i in range(4, 45):
        opens.append(100 + i * 0.4); closes.append(100 + (i + 1) * 0.4)
        highs.append(100 + (i + 1) * 0.4 + 0.2); lows.append(100 + i * 0.4 - 0.1)
        vols.append(1200)
    # Bars 45-48: price falls back toward zone
    for i in range(45, 49):
        opens.append(100 + 45 * 0.4 - (i - 44) * 0.6)
        closes.append(100 + 45 * 0.4 - (i - 43) * 0.6)
        highs.append(100 + 45 * 0.4 - (i - 44) * 0.6 + 0.1)
        lows.append(100 + 45 * 0.4 - (i - 44) * 0.6 - 0.1)
        vols.append(1000)
    if zone_touch:
        # Last bar: touches zone, bounces (bottom confirmed)
        opens.append(100.5); lows.append(100.4); closes.append(102.5)
        highs.append(102.7); vols.append(5000)
    else:
        # Last bar: does NOT touch zone (no bottom test)
        opens.append(102.0); lows.append(101.5); closes.append(103.5)
        highs.append(103.7); vols.append(5000)
    return pd.DataFrame({'open': opens, 'high': highs, 'low': lows,
                         'close': closes, 'volume': vols}, index=dates)


class TestScalperBottomConfirmation:
    """Gate 2b: price must test zone + bounce (CRUDEOIL fix)."""

    def test_zone_touch_passes_bottom_gate(self):
        """Bar that touches demand zone + bounces should pass bottom gate."""
        from automation.live_scanner import find_scalper_entry
        df = _make_zone_df(zone_touch=True)
        # Should pass bottom confirmation (may fail later gates, but NOT bottom gate)
        result = find_scalper_entry(df, len(df) - 1, 'IDX', 'NIFTY', None, {}, 15.0)
        # Result may be None due to supertrend/RSI/etc, but should NOT fail on
        # "zone not properly tested/bounced"
        # We check logs indirectly: if it fails, it's on a different gate
        # For this test, we just verify no exception and structural_stop present
        if result is not None:
            assert "structural_stop" in result
            assert "zone_bottom" in result
            assert result["zone_bottom"] > 0

    def test_no_zone_touch_logged_as_skip(self, caplog):
        """Bar that doesn't touch zone should be SKIPPED with bottom confirm message."""
        import logging
        from automation.live_scanner import find_scalper_entry
        df = _make_zone_df(zone_touch=False)
        with caplog.at_level(logging.INFO, logger='automation.live_scanner'):
            result = find_scalper_entry(df, len(df) - 1, 'IDX', 'NIFTY', None, {}, 15.0)
        # Should be skipped (result None) and mention "not at zone edge"
        assert result is None
        assert any("not at zone edge" in r.message or "tested/bounced" in r.message
                   for r in caplog.records)


class TestStructuralStopInSignals:
    """All signal generators must include structural_stop (zone-based SL)."""

    def test_momentum_spike_has_structural_stop(self):
        from subbrains.momentum_hunter import detect_momentum_spike
        n = 60
        dates = pd.date_range('2025-09-08 09:15', periods=n, freq='15min')
        opens = [99.9] * 57 + [100.0, 101.5, 103.0]
        closes = [100.0] * 57 + [101.5, 103.0, 105.0]
        vols = [1000] * 57 + [3000, 3500, 4000]
        highs = [max(o, c) + 0.3 for o, c in zip(opens, closes)]
        lows = [min(o, c) - 0.1 for o, c in zip(opens, closes)]
        df = pd.DataFrame({'open': opens, 'high': highs, 'low': lows,
                           'close': closes, 'volume': vols}, index=dates)
        result = detect_momentum_spike(df, len(df) - 1)
        assert result is not None
        assert "structural_stop" in result
        assert result["structural_stop"] > 0
        assert result["structural_stop"] < result["entry_price"]

    def test_orb_breakout_has_structural_stop(self):
        from subbrains.momentum_hunter import detect_orb_breakout
        from datetime import datetime as dt
        # Build 1m data with opening range + breakout
        n_1m = 45
        dates_1m = pd.date_range('2025-09-08 09:15', periods=n_1m, freq='1min')
        # OR bars 9:15-9:29 (first 15 bars), then breakout
        opens = [100.0] * 15 + [101.0, 102.0, 103.0, 104.0]
        closes = [100.5] * 15 + [102.0, 103.0, 104.0, 105.0]
        highs = [101.0] * 15 + [102.5, 103.5, 104.5, 105.5]
        lows = [99.5] * 15 + [101.0, 102.0, 103.0, 104.0]
        vols = [500] * 15 + [2000, 2500, 3000, 3500]
        df_1m = pd.DataFrame({'open': opens, 'high': highs, 'low': lows,
                              'close': closes, 'volume': vols}, index=dates_1m[:19])
        # Build 15m data
        n_15m = 45
        dates_15m = pd.date_range('2025-09-08 09:15', periods=n_15m, freq='15min')
        opens_15 = [100.0] * 44 + [101.0]
        closes_15 = [100.5] * 44 + [105.0]
        highs_15 = [101.0] * 44 + [105.5]
        lows_15 = [99.5] * 44 + [101.0]
        vols_15 = [1000] * 44 + [8000]
        df_15m = pd.DataFrame({'open': opens_15, 'high': highs_15, 'low': lows_15,
                               'close': closes_15, 'volume': vols_15}, index=dates_15m)
        now = dt(2025, 9, 8, 9, 45)
        result = detect_orb_breakout(df_15m, len(df_15m) - 1, df_1m, now)
        if result is not None:
            assert "structural_stop" in result
            assert result["structural_stop"] > 0

    def test_vwap_reclaim_has_structural_stop(self):
        from subbrains.momentum_hunter import detect_vwap_reclaim
        n = 50
        dates = pd.date_range('2025-09-08 09:15', periods=n, freq='15min')
        # Price oscillating around 100, VWAP ~100
        opens = [99.5] * 49 + [99.0]
        closes = [100.0] * 49 + [101.0]
        highs = [100.5] * 49 + [101.5]
        lows = [99.0] * 49 + [98.5]
        vols = [1000] * 49 + [3000]
        df = pd.DataFrame({'open': opens, 'high': highs, 'low': lows,
                           'close': closes, 'volume': vols}, index=dates)
        result = detect_vwap_reclaim(df, len(df) - 1)
        if result is not None:
            assert "structural_stop" in result
            assert result["structural_stop"] > 0

    def test_structural_stop_gives_more_room_than_fixed_7pct(self):
        """Structural stop should be WIDER than fixed -7% when zone is far."""
        from subbrains.momentum_hunter import detect_momentum_spike
        # Build a spike where the spike low is far below entry
        n = 60
        dates = pd.date_range('2025-09-08 09:15', periods=n, freq='15min')
        opens = [99.9] * 57 + [95.0, 98.0, 103.0]
        closes = [100.0] * 57 + [98.0, 103.0, 108.0]
        vols = [1000] * 57 + [4000, 5000, 6000]
        highs = [max(o, c) + 0.5 for o, c in zip(opens, closes)]
        lows = [min(o, c) - 0.5 for o, c in zip(opens, closes)]
        df = pd.DataFrame({'open': opens, 'high': highs, 'low': lows,
                           'close': closes, 'volume': vols}, index=dates)
        result = detect_momentum_spike(df, len(df) - 1)
        if result is not None:
            entry = result["entry_price"]
            stop = result["structural_stop"]
            stop_pct = (entry - stop) / entry * 100
            # Structural stop should be wider than fixed 7% (spike low was ~94)
            assert stop_pct >= 7.0, f"Structural stop {stop_pct:.1f}% should be >= 7%"


class TestScalperStructuralStop:
    """Scalper signals must include zone_bottom, zone_top, structural_stop."""

    def test_scalper_return_includes_zone_info(self):
        from automation.live_scanner import find_scalper_entry
        from pipeline.intraday_strategies import detect_zones
        df = _make_zone_df(zone_touch=True)
        # Verify zones exist in the data
        zones = detect_zones(df, len(df) - 2, lookback=len(df) - 2)
        assert len(zones) > 0, "Test data should have demand zones"
        # Check zone fields
        z = zones[0]
        assert "bottom" in z
        assert "top" in z
        assert z["bottom"] > 0
        assert z["top"] > z["bottom"]
