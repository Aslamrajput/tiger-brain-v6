"""Tests for Tiger restoration — Volume Profile (informational), original exit rules.

Tiger is restored to its powerful pre-injection state:
- 7% stop loss (gives pullback room for cheap options)
- 15-step OTM walk (catches cheap strikes that rocket)
- No RSI restriction on 1m velocity (catches EARLY momentum, not late)
- Volume Profile is informational only (no score boost, no blocking)
- wick_confirmed NameError bugfix retained
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
    """Volume Profile engine — POC/VAH/VAL (informational only)."""

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
        df.loc[df.index[40:50], 'high'] = 106
        df.loc[df.index[40:50], 'low'] = 104
        df.loc[df.index[40:50], 'volume'] = 5000
        vp = compute_volume_profile(df, 59, lookback=50, n_bins=20)
        assert 103 <= vp["poc"] <= 107

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
        assert vp_30["poc"] != vp_59["poc"] or vp_30["vah"] != vp_59["vah"]


class TestZoneAtVPEdge:
    """Zone-at-VP-edge detection (used for informational logging)."""

    def test_zone_at_poc_detected(self):
        from pipeline.intraday_strategies import compute_volume_profile, zone_at_vp_edge
        df = _make_ohlcv(60, base=100)
        vp = compute_volume_profile(df, 59, lookback=50)
        zone = {"type": "demand", "bottom": vp["poc"] - 1, "top": vp["poc"] + 1}
        assert zone_at_vp_edge(zone, vp, tolerance_pct=2.0)

    def test_zone_far_from_vp_not_detected(self):
        from pipeline.intraday_strategies import compute_volume_profile, zone_at_vp_edge
        df = _make_ohlcv(60, base=100)
        vp = compute_volume_profile(df, 59, lookback=50)
        zone = {"type": "demand", "bottom": vp["poc"] + 50, "top": vp["poc"] + 52}
        assert not zone_at_vp_edge(zone, vp, tolerance_pct=2.0)


class TestTigerRestoration:
    """Tiger is restored to its powerful pre-injection state."""

    def test_max_stop_pct_is_7(self):
        """7% stop gives pullback room for cheap options (₹36 premium → ₹2.5 SL)."""
        from config.thresholds import SCALPER
        assert SCALPER["MAX_STOP_PCT"] == 7.0

    def test_max_stop_rupees_is_1500(self):
        """₹1500 cap gives room — not too tight."""
        from config.thresholds import SCALPER
        assert SCALPER["MAX_STOP_RUPEES"] == 1500

    def test_catastrophic_stop_remains_12(self):
        """Black swan protection stays at 12%."""
        from config.thresholds import SCALPER
        assert SCALPER["CATASTROPHIC_STOP_PCT"] == 12.0

    def test_no_max_otm_steps_restriction(self):
        """OTM walk must NOT be capped at 3 — Tiger catches cheap strikes."""
        from config.thresholds import SCALPER
        # MAX_OTM_STEPS should NOT exist in config (reverted to original 15-step)
        assert "MAX_OTM_STEPS" not in SCALPER, \
            "MAX_OTM_STEPS must be removed — Tiger needs full 15-step OTM walk"

    def test_no_velocity_rsi_restriction(self):
        """No RSI ≥60/≤40 velocity gate — Tiger catches EARLY momentum, not late."""
        from config.thresholds import SCALPER
        assert "VELOCITY_RSI_BUY" not in SCALPER, \
            "VELOCITY_RSI_BUY must be removed — RSI restriction causes late entries"
        assert "VELOCITY_RSI_SELL" not in SCALPER, \
            "VELOCITY_RSI_SELL must be removed — RSI restriction causes late entries"
        assert "VELOCITY_VOL_MIN" not in SCALPER, \
            "VELOCITY_VOL_MIN must be removed — use original MIN_VOLUME_SURGE"
        assert "VELOCITY_BODY_PCT" not in SCALPER, \
            "VELOCITY_BODY_PCT must be removed — use original MIN_BODY_PCT"

    def test_no_1m_velocity_gate_in_scalper(self):
        """find_scalper_entry must NOT have Gate 5b (1m velocity blocking).
        Downstream _verify_1m_velocity at order placement is sufficient."""
        from automation.live_scanner import find_scalper_entry
        source = inspect.getsource(find_scalper_entry)
        assert "GATE 5b" not in source, "Gate 5b must be removed from scalper"
        assert "velocity_confirmed" not in source, "velocity_confirmed must be removed"

    def test_no_vp_score_boost_in_scalper(self):
        """VP must NOT boost zone scores — zone research is primary, VP is informational."""
        from automation.live_scanner import find_scalper_entry
        source = inspect.getsource(find_scalper_entry)
        assert "vp_edge" not in source, "vp_edge scoring must be removed"
        assert "score.*+.*10" not in source.replace(" ", ""), "VP score boost must be removed"

    def test_scalper_accepts_df_1m_parameter(self):
        """find_scalper_entry should still accept df_1m (for future use)."""
        from automation.live_scanner import find_scalper_entry
        sig = inspect.signature(find_scalper_entry)
        assert "df_1m" in sig.parameters
        assert sig.parameters["df_1m"].default is None

    def test_no_wick_confirmed_nameerror(self):
        """wick_confirmed NameError bug must be fixed (replaced with clean return)."""
        from automation.live_scanner import find_scalper_entry
        source = inspect.getsource(find_scalper_entry)
        assert "wick_confirmed" not in source, "wick_confirmed still referenced (NameError bug)"

    def test_breakeven_lock_at_3pct(self):
        """Breakeven lock at +3% peak gain retained."""
        import automation.tiger_live as tl
        source = inspect.getsource(tl.TigerLiveRunner)
        assert "peak_gain_pct >= 3.0" in source or "peak_gain_pct >= 3" in source

    def test_trail_lock_70pct(self):
        """70% peak profit trailing rule retained."""
        import automation.tiger_live as tl
        source = inspect.getsource(tl.TigerLiveRunner)
        assert "0.70" in source or "0.7" in source

    def test_velocity_uses_original_vol_threshold(self):
        """_verify_1m_velocity must use MIN_VOLUME_SURGE (1.4x), not VELOCITY_VOL_MIN."""
        import automation.tiger_live as tl
        source = inspect.getsource(tl.TigerLiveRunner)
        assert "MIN_VOLUME_SURGE" in source
        assert "VELOCITY_VOL_MIN" not in source

    def test_no_rsi_gate_in_velocity(self):
        """_verify_1m_velocity must NOT have RSI burst gate (Gate 4)."""
        import automation.tiger_live as tl
        source = inspect.getsource(tl.TigerLiveRunner._verify_1m_velocity)
        assert "VELOCITY_RSI_BUY" not in source
        assert "VELOCITY_RSI_SELL" not in source


class TestVolumeProfileConfig:
    """Volume Profile config values (informational only)."""

    def test_vp_lookback_is_50(self):
        from config.thresholds import SCALPER
        assert SCALPER["VP_LOOKBACK"] == 50

    def test_vp_bins_is_20(self):
        from config.thresholds import SCALPER
        assert SCALPER["VP_BINS"] == 20

    def test_vp_value_area_pct_is_70(self):
        from config.thresholds import SCALPER
        assert SCALPER["VP_VALUE_AREA_PCT"] == 70.0
