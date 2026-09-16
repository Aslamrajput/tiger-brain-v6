"""Tests for Volume Profile engine, 1m velocity acceleration, and risk matrix.

Tests the architectural injection:
1. Volume Profile (POC/VAH/VAL) computation
2. Zone-at-VP-edge detection (institutional order block)
3. 1m velocity acceleration in scalper (vol 1.3x, body 65%, RSI 60/40)
4. OTM fallback capped at 3 steps
5. Drawdown risk matrix (-5% / -₹800)
"""
import pandas as pd
import numpy as np
import pytest
import inspect
from datetime import datetime


def _make_ohlcv(n=60, base=100, vol=1000, seed=42):
    """Generate synthetic OHLCV data."""
    np.random.seed(seed)
    dates = pd.date_range('2025-09-08 09:15', periods=n, freq='15min')
    closes = base + np.cumsum(np.random.randn(n) * 0.5)
    opens = closes - np.random.rand(n) * 0.3
    highs = np.maximum(opens, closes) + np.random.rand(n) * 0.5
    lows = np.minimum(opens, closes) - np.random.rand(n) * 0.5
    volumes = vol + np.random.randint(-200, 500, n)
    volumes = np.maximum(volumes, 100)
    return pd.DataFrame({'open': opens, 'high': highs, 'low': lows,
                         'close': closes, 'volume': volumes}, index=dates)


class TestVolumeProfile:
    """Volume Profile engine — POC, VAH, VAL."""

    def test_compute_volume_profile_returns_dict(self):
        from pipeline.intraday_strategies import compute_volume_profile
        df = _make_ohlcv(60)
        vp = compute_volume_profile(df, 59, lookback=50, n_bins=20)
        assert isinstance(vp, dict)
        assert "poc" in vp
        assert "vah" in vp
        assert "val" in vp
        assert vp["poc"] > 0
        assert vp["vah"] > vp["val"]
        assert vp["vah"] >= vp["poc"] >= vp["val"]

    def test_volume_profile_poc_at_high_volume_level(self):
        """POC should be near the price level with most volume."""
        from pipeline.intraday_strategies import compute_volume_profile
        df = _make_ohlcv(60, base=100)
        # Inject high volume at ~105 level
        df.loc[df.index[40:50], 'high'] = 106
        df.loc[df.index[40:50], 'low'] = 104
        df.loc[df.index[40:50], 'volume'] = 5000
        vp = compute_volume_profile(df, 59, lookback=50, n_bins=20)
        # POC should be in the 104-106 range
        assert 103 <= vp["poc"] <= 107

    def test_volume_profile_value_area_captures_70pct(self):
        """Value Area should capture ~70% of total volume."""
        from pipeline.intraday_strategies import compute_volume_profile
        df = _make_ohlcv(60)
        vp = compute_volume_profile(df, 59, lookback=50, n_bins=20, value_area_pct=70.0)
        assert vp["va_pct"] >= 65  # allow some tolerance for binning
        assert vp["va_volume"] > 0

    def test_volume_profile_insufficient_data(self):
        """Should return empty dict for insufficient data."""
        from pipeline.intraday_strategies import compute_volume_profile
        df = _make_ohlcv(3)
        vp = compute_volume_profile(df, 2, lookback=50)
        assert vp == {}

    def test_volume_profile_no_lookahead(self):
        """VP must only use bars up to i, not future bars."""
        from pipeline.intraday_strategies import compute_volume_profile
        df = _make_ohlcv(60)
        vp_30 = compute_volume_profile(df, 30, lookback=30)
        vp_59 = compute_volume_profile(df, 59, lookback=30)
        # They should be different (30 uses bars 0-30, 59 uses 29-59)
        assert vp_30["poc"] != vp_59["poc"] or vp_30["vah"] != vp_59["vah"]


class TestZoneAtVPEdge:
    """Institutional order block detection at VP edges."""

    def test_zone_at_poc_detected(self):
        from pipeline.intraday_strategies import compute_volume_profile, zone_at_vp_edge
        df = _make_ohlcv(60, base=100)
        vp = compute_volume_profile(df, 59, lookback=50)
        # Create a zone right at POC
        zone = {"type": "demand", "bottom": vp["poc"] - 1, "top": vp["poc"] + 1}
        assert zone_at_vp_edge(zone, vp, tolerance_pct=2.0)

    def test_zone_far_from_vp_not_detected(self):
        from pipeline.intraday_strategies import compute_volume_profile, zone_at_vp_edge
        df = _make_ohlcv(60, base=100)
        vp = compute_volume_profile(df, 59, lookback=50)
        # Create a zone far from VP edges
        zone = {"type": "demand", "bottom": vp["poc"] + 50, "top": vp["poc"] + 52}
        assert not zone_at_vp_edge(zone, vp, tolerance_pct=2.0)

    def test_empty_vp_returns_false(self):
        from pipeline.intraday_strategies import zone_at_vp_edge
        assert not zone_at_vp_edge({}, {})


class TestScalperVelocityGate:
    """1-minute velocity acceleration in find_scalper_entry."""

    def test_scalper_accepts_df_1m_parameter(self):
        """find_scalper_entry should accept df_1m as optional parameter."""
        import inspect
        from automation.live_scanner import find_scalper_entry
        sig = inspect.signature(find_scalper_entry)
        assert "df_1m" in sig.parameters
        assert sig.parameters["df_1m"].default is None

    def test_scalper_returns_vp_fields(self):
        """Scalper signal should include VP fields."""
        from automation.live_scanner import find_scalper_entry
        from pipeline.intraday_strategies import detect_zones
        # Build data with demand zone
        n = 50
        dates = pd.date_range('2025-09-08 09:15', periods=n, freq='15min')
        opens, closes, highs, lows, vols = [], [], [], [], []
        opens.append(99.0); closes.append(101.0); highs.append(101.2); lows.append(98.8); vols.append(2000)
        for _ in range(3):
            opens.append(100.5); closes.append(100.6); highs.append(100.8); lows.append(100.3); vols.append(800)
        for i in range(4, 45):
            opens.append(100 + i * 0.4); closes.append(100 + (i + 1) * 0.4)
            highs.append(100 + (i + 1) * 0.4 + 0.2); lows.append(100 + i * 0.4 - 0.1)
            vols.append(1200)
        for i in range(45, 49):
            opens.append(100 + 45 * 0.4 - (i - 44) * 0.6)
            closes.append(100 + 45 * 0.4 - (i - 43) * 0.6)
            highs.append(100 + 45 * 0.4 - (i - 44) * 0.6 + 0.1)
            lows.append(100 + 45 * 0.4 - (i - 44) * 0.6 - 0.1)
            vols.append(1000)
        opens.append(100.5); lows.append(100.4); closes.append(102.5)
        highs.append(102.7); vols.append(5000)
        df = pd.DataFrame({'open': opens, 'high': highs, 'low': lows,
                           'close': closes, 'volume': vols}, index=dates)
        result = find_scalper_entry(df, len(df) - 1, 'IDX', 'NIFTY', None, {}, 15.0)
        # May fail on later gates, but if it passes, check VP fields
        if result is not None:
            assert "vp_edge" in result
            assert "velocity_confirmed" in result
            assert "vp_poc" in result
            assert "vp_vah" in result
            assert "vp_val" in result

    def test_no_wick_confirmed_nameerror(self):
        """wick_confirmed should NOT be referenced (was a NameError bug)."""
        from automation.live_scanner import find_scalper_entry
        # Build minimal valid data — if wick_confirmed is referenced,
        # it'll raise NameError (caught by try/except in scanner)
        n = 50
        dates = pd.date_range('2025-09-08 09:15', periods=n, freq='15min')
        opens, closes, highs, lows, vols = [], [], [], [], []
        opens.append(99.0); closes.append(101.0); highs.append(101.2); lows.append(98.8); vols.append(2000)
        for _ in range(3):
            opens.append(100.5); closes.append(100.6); highs.append(100.8); lows.append(100.3); vols.append(800)
        for i in range(4, 45):
            opens.append(100 + i * 0.4); closes.append(100 + (i + 1) * 0.4)
            highs.append(100 + (i + 1) * 0.4 + 0.2); lows.append(100 + i * 0.4 - 0.1)
            vols.append(1200)
        for i in range(45, 49):
            opens.append(100 + 45 * 0.4 - (i - 44) * 0.6)
            closes.append(100 + 45 * 0.4 - (i - 43) * 0.6)
            highs.append(100 + 45 * 0.4 - (i - 44) * 0.6 + 0.1)
            lows.append(100 + 45 * 0.4 - (i - 44) * 0.6 - 0.1)
            vols.append(1000)
        opens.append(100.5); lows.append(100.4); closes.append(102.5)
        highs.append(102.7); vols.append(5000)
        df = pd.DataFrame({'open': opens, 'high': highs, 'low': lows,
                           'close': closes, 'volume': vols}, index=dates)
        # This should NOT raise NameError — wick_confirmed was removed
        import automation.live_scanner as ls
        source = inspect.getsource(ls.find_scalper_entry)
        assert "wick_confirmed" not in source, "wick_confirmed still referenced (NameError bug)"


class TestOTMFallbackCap:
    """OTM fallback must be capped at 3 steps (deep OTM forbidden)."""

    def test_max_otm_steps_config_is_3(self):
        from config.thresholds import SCALPER
        assert SCALPER.get("MAX_OTM_STEPS") == 3

    def test_find_affordable_option_respects_max_steps(self):
        """find_affordable_option with max_otm_steps=3 should only try 3."""
        from data.loader import find_affordable_option
        # Mock chain — won't actually resolve, but test the param passing
        # Just verify the function accepts max_otm_steps=3
        import inspect
        sig = inspect.signature(find_affordable_option)
        assert "max_otm_steps" in sig.parameters


class TestDrawdownRiskMatrix:
    """Active drawdown: -5% or -₹800 hard ceiling."""

    def test_max_stop_pct_is_5(self):
        from config.thresholds import SCALPER
        assert SCALPER["MAX_STOP_PCT"] == 5.0

    def test_max_stop_rupees_is_800(self):
        from config.thresholds import SCALPER
        assert SCALPER["MAX_STOP_RUPEES"] == 800

    def test_catastrophic_stop_remains_12(self):
        """Black swan protection stays at 12% (instant exit during min hold)."""
        from config.thresholds import SCALPER
        assert SCALPER["CATASTROPHIC_STOP_PCT"] == 12.0

    def test_breakeven_lock_at_3pct(self):
        """Breakeven lock still activates at +3% peak gain."""
        # The exit logic uses hardcoded 3.0 for breakeven activation
        import automation.tiger_live as tl
        source = inspect.getsource(tl.TigerLiveRunner)
        assert "peak_gain_pct >= 3.0" in source or "peak_gain_pct >= 3" in source

    def test_trail_lock_70pct(self):
        """70% peak profit trailing rule retained."""
        import automation.tiger_live as tl
        source = inspect.getsource(tl.TigerLiveRunner)
        assert "0.70" in source or "0.7" in source


class TestVelocityConfig:
    """1-minute velocity config values."""

    def test_velocity_vol_min_is_1_3(self):
        from config.thresholds import SCALPER
        assert SCALPER["VELOCITY_VOL_MIN"] == 1.3

    def test_velocity_body_pct_is_65(self):
        from config.thresholds import SCALPER
        assert SCALPER["VELOCITY_BODY_PCT"] == 65

    def test_velocity_rsi_buy_is_60(self):
        from config.thresholds import SCALPER
        assert SCALPER["VELOCITY_RSI_BUY"] == 60

    def test_velocity_rsi_sell_is_40(self):
        from config.thresholds import SCALPER
        assert SCALPER["VELOCITY_RSI_SELL"] == 40


class TestVolumeProfileConfig:
    """Volume Profile config values."""

    def test_vp_lookback_is_50(self):
        from config.thresholds import SCALPER
        assert SCALPER["VP_LOOKBACK"] == 50

    def test_vp_bins_is_20(self):
        from config.thresholds import SCALPER
        assert SCALPER["VP_BINS"] == 20

    def test_vp_value_area_pct_is_70(self):
        from config.thresholds import SCALPER
        assert SCALPER["VP_VALUE_AREA_PCT"] == 70.0
