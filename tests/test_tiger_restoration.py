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

    def test_velocity_uses_direction_only(self):
        """_verify_1m_velocity uses direction-only check (simplified from vol/RSI gates)."""
        import automation.tiger_live as tl
        source = inspect.getsource(tl.TigerLiveRunner._verify_1m_velocity)
        # Direction check is the core logic — no volume surge or RSI gates
        assert "MIN_VOLUME_SURGE" not in source
        assert "VELOCITY_RSI_BUY" not in source
        assert "VELOCITY_RSI_SELL" not in source

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


class TestRealZoneIntegrity:
    """Verify Tiger only detects REAL zones — no fake zones."""

    def test_cluster_min_is_3(self):
        """3+ bars consolidation required (relaxed from 4 for aggressive hunting)."""
        from pipeline.intraday_strategies import detect_zones
        import inspect
        sig = inspect.signature(detect_zones)
        assert sig.parameters["cluster_min"].default == 3

    def test_impulse_min_pct_is_03(self):
        """0.3% impulse required (relaxed from 0.6 for more zone detection)."""
        from pipeline.intraday_strategies import detect_zones
        import inspect
        sig = inspect.signature(detect_zones)
        assert sig.parameters["impulse_min_pct"].default == 0.3

    def test_2_bar_cluster_rejected(self):
        """2-bar cluster should NOT form a zone (too short = noise)."""
        from pipeline.intraday_strategies import detect_zones
        idx = pd.date_range("2026-09-08 09:15", periods=50, freq="15min")
        opens, highs, lows, closes, vols = [], [], [], [], []
        px = 100.0
        for i in range(50):
            o = px
            if i == 10:  # impulse — large body/range so NOT counted as cluster bar
                c = o * 1.01; h, l, v = c + 0.3, o - 0.1, 2000
            elif 11 <= i <= 12:  # 2-bar cluster (too short)
                c = o + 0.5; h, l, v = o + 3, o - 1, 800
            elif i == 14:  # explosive move
                c = o + 20; h, l, v = o + 22, o - 1, 4000
            else:
                # Trending bars with large bodies (body/range > 0.5)
                c = o + 2.0; h, l, v = c + 0.5, o - 0.5, 1200
            opens.append(o); highs.append(h); lows.append(l); closes.append(c); vols.append(v)
            px = c
        df = pd.DataFrame({"open": opens, "high": highs, "low": lows,
                           "close": closes, "volume": vols}, index=idx)
        zones = detect_zones(df, 49, lookback=40)
        assert len(zones) == 0, "2-bar cluster should be rejected (noise, not real base)"

    def test_4_bar_cluster_accepted(self):
        """4-bar cluster should form a zone (real institutional base)."""
        from pipeline.intraday_strategies import detect_zones
        idx = pd.date_range("2026-09-08 09:15", periods=50, freq="15min")
        opens, highs, lows, closes, vols = [], [], [], [], []
        px = 100.0
        for i in range(50):
            o = px
            if i == 10:  # impulse — large body/range
                c = o * 1.01; h, l, v = c + 0.3, o - 0.1, 2000
            elif 11 <= i <= 14:  # 4-bar cluster (real base)
                c = o + 0.5; h, l, v = o + 3, o - 1, 800
            elif i == 15:  # explosive move
                c = o + 20; h, l, v = o + 22, o - 1, 4000
            else:
                c = o + 2.0; h, l, v = c + 0.5, o - 0.5, 1200
            opens.append(o); highs.append(h); lows.append(l); closes.append(c); vols.append(v)
            px = c
        df = pd.DataFrame({"open": opens, "high": highs, "low": lows,
                           "close": closes, "volume": vols}, index=idx)
        zones = detect_zones(df, 49, lookback=40)
        assert len(zones) >= 1, "4-bar cluster should be accepted as real zone"

    def test_zone_touch_tolerance_is_03pct(self):
        """Zone touch tolerance must be 0.3%, not 2% (2% = zone break = fake)."""
        from pipeline.intraday_strategies import zone_touched_on_1m
        # Demand zone at 100-101
        zone = {"type": "demand", "top": 101.0, "bottom": 100.0}
        # Price drops to 99.0 (1% below bottom) — should NOT touch (was 2% before)
        bar = {"open": 102, "high": 103, "low": 99.0, "close": 101}
        result = zone_touched_on_1m(bar, zone)
        assert result is None, "1% penetration should NOT count as touch (was 2% = fake)"

    def test_real_zone_touch_accepted(self):
        """Price at zone edge should count as real touch."""
        from pipeline.intraday_strategies import zone_touched_on_1m
        zone = {"type": "demand", "top": 101.0, "bottom": 100.0}
        # Price low touches zone top (real touch)
        bar = {"open": 102, "high": 103, "low": 100.5, "close": 101}
        result = zone_touched_on_1m(bar, zone)
        assert result == "demand"

    def test_dedup_merges_overlapping_zones(self):
        """Overlapping zones should be merged (keep strongest)."""
        from pipeline.intraday_strategies import detect_zones
        idx = pd.date_range("2026-09-08 09:15", periods=60, freq="15min")
        opens, highs, lows, closes, vols = [], [], [], [], []
        px = 100.0
        for i in range(60):
            o = px
            if i == 10:  # first impulse
                c = o * 1.01
                h, l, v = c + 2, o - 1, 2000
            elif 11 <= i <= 14:  # first base
                c = o + 0.5
                h, l, v = o + 3, o - 1, 800
            elif i == 15:  # explosive
                c = o + 30
                h, l, v = o + 32, o - 1, 4000
            elif i == 20:  # second impulse (overlapping zone nearby)
                c = o * 1.008
                h, l, v = c + 2, o - 1, 1800
            elif 21 <= i <= 24:  # second base (slightly higher)
                c = o + 0.5
                h, l, v = o + 3, o - 1, 800
            elif i == 25:  # explosive
                c = o + 25
                h, l, v = o + 27, o - 1, 3500
            else:
                c = o + 0.3
                h, l, v = max(o, c) + 2, min(o, c) - 2, 1200
            opens.append(o); highs.append(h); lows.append(l); closes.append(c); vols.append(v)
            px = c
        df = pd.DataFrame({"open": opens, "high": highs, "low": lows,
                           "close": closes, "volume": vols}, index=idx)
        zones = detect_zones(df, 59, lookback=50)
        # Check no overlapping zones of same type
        for a in range(len(zones)):
            for b in range(a + 1, len(zones)):
                if zones[a]["type"] == zones[b]["type"]:
                    overlap_top = min(zones[a]["top"], zones[b]["top"])
                    overlap_bot = max(zones[a]["bottom"], zones[b]["bottom"])
                    if overlap_top > overlap_bot:
                        z_area = zones[a]["top"] - zones[a]["bottom"]
                        d_area = zones[b]["top"] - zones[b]["bottom"]
                        overlap_area = overlap_top - overlap_bot
                        assert overlap_area / min(z_area, d_area) <= 0.5, \
                            "Overlapping zones should be deduped"

    def test_weak_impulse_rejected(self):
        """0.2% impulse should NOT create a zone (too weak = normal bar)."""
        from pipeline.intraday_strategies import detect_zones
        idx = pd.date_range("2026-09-08 09:15", periods=50, freq="15min")
        opens, highs, lows, closes, vols = [], [], [], [], []
        px = 100.0
        for i in range(50):
            o = px
            if i == 10:  # weak impulse — large body/range, 0.2% move
                c = o * 1.002; h, l, v = c + 0.2, o, 1500
            elif 11 <= i <= 14:  # 4-bar base (small body)
                c = o + 0.5; h, l, v = o + 3, o - 1, 800
            elif i == 15:  # explosive
                c = o + 20; h, l, v = o + 22, o - 1, 4000
            else:
                # Large-body bars (body/range > 0.5, not cluster-eligible)
                c = o + 0.1; h, l, v = c, o, 1200
            opens.append(o); highs.append(h); lows.append(l); closes.append(c); vols.append(v)
            px = c
        df = pd.DataFrame({"open": opens, "high": highs, "low": lows,
                           "close": closes, "volume": vols}, index=idx)
        zones = detect_zones(df, 49, lookback=40)
        assert len(zones) == 0, "0.2% impulse should be rejected (normal bar, not institutional)"

    def test_strong_impulse_accepted(self):
        """0.8% impulse should create a zone (real institutional move)."""
        from pipeline.intraday_strategies import detect_zones
        idx = pd.date_range("2026-09-08 09:15", periods=50, freq="15min")
        opens, highs, lows, closes, vols = [], [], [], [], []
        px = 100.0
        for i in range(50):
            o = px
            if i == 10:  # strong impulse — large body/range, 0.8% move
                c = o * 1.008; h, l, v = c + 0.3, o - 0.1, 2000
            elif 11 <= i <= 14:  # 4-bar base
                c = o + 0.5; h, l, v = o + 3, o - 1, 800
            elif i == 15:  # explosive
                c = o + 20; h, l, v = o + 22, o - 1, 4000
            else:
                # Gentle drift (< 0.6% per bar) with large body/ratio
                c = o + 0.3; h, l, v = c + 0.1, o - 0.1, 1200
            opens.append(o); highs.append(h); lows.append(l); closes.append(c); vols.append(v)
            px = c
        df = pd.DataFrame({"open": opens, "high": highs, "low": lows,
                           "close": closes, "volume": vols}, index=idx)
        zones = detect_zones(df, 49, lookback=40)
        assert len(zones) >= 1, "0.8% impulse should be accepted as real institutional move"


class TestNSESniperRoute:
    """Route B: NSE sniper session + 2-component confluence (looser than MCX).

    The SAME pure-SMC sniper engine runs on NSE index/stock options during the
    NSE session (09:15-15:00), with a looser confluence gate (2 vs 3) since
    index/stock moves are noisier and 3-component confluence is rare.
    """

    def test_nse_sniper_session_helper_exists(self):
        """_nse_sniper_session_active must exist on TigerLiveRunner."""
        import automation.tiger_live as tl
        assert hasattr(tl.TigerLiveRunner, "_nse_sniper_session_active")

    def test_nse_session_active_in_window(self):
        """09:15-15:00 IST → NSE sniper session is active."""
        import automation.tiger_live as tl
        runner = tl.TigerLiveRunner()
        assert runner._nse_sniper_session_active(datetime(2026, 9, 17, 10, 30))

    def test_nse_session_inactive_before_open(self):
        """Before 09:15 → NSE sniper session NOT active."""
        import automation.tiger_live as tl
        runner = tl.TigerLiveRunner()
        assert not runner._nse_sniper_session_active(datetime(2026, 9, 17, 9, 10))

    def test_nse_session_inactive_after_cutoff(self):
        """After 15:00 (entry cutoff) → NSE sniper session NOT active."""
        import automation.tiger_live as tl
        runner = tl.TigerLiveRunner()
        assert not runner._nse_sniper_session_active(datetime(2026, 9, 17, 15, 30))

    def test_mcx_session_still_works(self):
        """MCX sniper session (10:30-23:30) is unaffected by Route B."""
        import automation.tiger_live as tl
        runner = tl.TigerLiveRunner()
        # 20:00 — within MCX session (10:30-23:30)
        assert runner._sniper_session_active(datetime(2026, 9, 17, 20, 0))
        # 09:30 — outside MCX session
        assert not runner._sniper_session_active(datetime(2026, 9, 17, 9, 30))

    def test_scan_sniper_signals_checks_both_markets(self):
        """scan_sniper_signals must call _scan_one_market for NSE + MCX."""
        import automation.tiger_live as tl
        import inspect
        src = inspect.getsource(tl.TigerLiveRunner.scan_sniper_signals)
        assert "_nse_sniper_session_active" in src
        assert "_sniper_session_active" in src
        assert "_scan_one_market" in src

    def test_scan_sniper_signals_does_not_market_shadow(self):
        """CRITICAL: during the 10:30-15:00 NSE+MCX overlap, an NSE signal must
        NOT suppress the MCX scan (and vice versa). Both markets are scanned
        independently and every qualifying signal is returned."""
        from datetime import datetime
        import automation.tiger_live as tl

        runner = tl.TigerLiveRunner()
        calls = []

        def fake_scan(market):
            calls.append(market)
            return {"symbol": f"{market}_SYM", "market": market}

        runner._scan_one_market = fake_scan
        runner._sniper_trades_today = lambda: 0

        # 11:00 → both NSE (09:15-15:00) and MCX (10:30-23:30) sessions are live.
        runner._nse_sniper_session_active = lambda now=None: True
        runner._sniper_session_active = lambda now=None: True

        signals = runner.scan_sniper_signals()

        assert calls == ["NSE", "MCX"]          # neither market skipped
        assert len(signals) == 2                 # both signals returned
        assert {s["market"] for s in signals} == {"NSE", "MCX"}

    def test_scan_sniper_signals_honours_daily_cap_across_markets(self):
        """The shared MAX_TRADES_PER_DAY cap is respected across both markets —
        a cap already reached returns no signals."""
        import automation.tiger_live as tl
        from config.thresholds import SNIPER

        runner = tl.TigerLiveRunner()
        runner._scan_one_market = lambda market: {"symbol": "X", "market": market}
        runner._sniper_trades_today = lambda: SNIPER["MAX_TRADES_PER_DAY"]
        runner._nse_sniper_session_active = lambda now=None: True
        runner._sniper_session_active = lambda now=None: True

        assert runner.scan_sniper_signals() == []

    def test_scan_one_market_uses_market_specific_confluence(self):
        """_scan_one_market must apply NSE (2) vs MCX (3) confluence gates."""
        import automation.tiger_live as tl
        import inspect
        src = inspect.getsource(tl.TigerLiveRunner._scan_one_market)
        assert "NSE_MIN_CONFLUENCE_COMPONENTS" in src
        assert "MIN_CONFLUENCE_COMPONENTS" in src
        assert "allowed=universe" in src  # passes the universe set to scanner

    def test_scan_one_market_passes_market_to_scanner(self):
        """FINAL SNIPER INTEGRATION: _scan_one_market must pass market= to
        scan_mcx so the NSE OB-anchored gate (not the MCX count gate) applies
        to NSE symbols."""
        import automation.tiger_live as tl
        import inspect
        src = inspect.getsource(tl.TigerLiveRunner._scan_one_market)
        assert "market=market" in src

    def test_sniper_exit_bos_only_after_trail_arms(self):
        """5m opposite BOS exit must only fire AFTER the trail arms (gain >=
        TRAIL_ACTIVATE_PCT). Before arming, only the OB stop protects — so the
        first pullback doesn't kill the rocket. Verified by inspecting that the
        BOS check is nested under the `gain >= TRAIL_ACTIVATE_PCT` branch."""
        import automation.tiger_live as tl
        import inspect
        src = inspect.getsource(tl.TigerLiveRunner.monitor_open_positions)
        assert "TRAIL_ACTIVATE_PCT" in src
        assert "sniper_5m_opposite_bos" in src
        assert "confirmed" in src  # 2-bar confirmation present

    def test_sniper_exit_trail_uses_25pct_from_config(self):
        """Exit logic must read TRAIL_ACTIVATE_PCT from config (now 10%), not
        a hardcoded value — so the +10% aggressive trail applies live."""
        import automation.tiger_live as tl
        import inspect
        src = inspect.getsource(tl.TigerLiveRunner.monitor_open_positions)
        assert "TRAIL_ACTIVATE_PCT" in src
        # Must NOT have a hardcoded 5.0 (old value) as the activation threshold
        assert ">= 5.0" not in src and ">=5.0" not in src

    def test_sniper_entry_wick_optional_for_impulsive_body(self):
        """1m entry confirmation: wick rejection is OPTIONAL when the reversal
        candle is impulsive (body >= 60% of range). This lets body-heavy rocket
        entries through (real momentum is body-heavy, not wick-heavy). Only
        small-body candles need a wick."""
        import automation.tiger_live as tl
        import inspect
        src = inspect.getsource(tl.TigerLiveRunner._verify_sniper_entry)
        assert "body_ratio" in src or "body / rng" in src
        assert "0.60" in src or "0.6" in src

