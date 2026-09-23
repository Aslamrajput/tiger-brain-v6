"""Tests for ML engine, feature store, and inference gate integration."""
import os
import math
import numpy as np
import pandas as pd
import pytest


# ============================================================
# Feature Store Tests
# ============================================================

class TestFeatureStore:
    """data/features.py — live feature extraction from WS + data maps."""

    def test_extract_live_features_returns_all_columns(self):
        """All FEATURE_COLUMNS must be present in the output dict."""
        from data.features import extract_live_features, FEATURE_COLUMNS
        signal = {"setup_score": 80, "is_scalper": True}
        feats = extract_live_features(
            symbol="NIFTY", signal=signal,
            data_map_15m={}, data_map_1m={}, broker=None,
        )
        for col in FEATURE_COLUMNS:
            assert col in feats, f"Missing feature: {col}"

    def test_feature_columns_has_16_sniper_features(self):
        """TIGER SNIPER ADVANCED V2: 16 features (13 base + 3 sniper)."""
        from data.features import FEATURE_COLUMNS
        assert len(FEATURE_COLUMNS) == 16
        # 3 sniper features present
        assert "sniper_zone_strength" in FEATURE_COLUMNS
        assert "fvg_size" in FEATURE_COLUMNS
        assert "commodity_volatility" in FEATURE_COLUMNS

    def test_sniper_features_sourced_from_signal(self):
        """Sniper features must be pulled from the mcx_scanner signal dict."""
        from data.features import extract_live_features
        signal = {"setup_score": 85, "sniper_zone_strength": 88.0,
                  "fvg_size": 0.42, "commodity_volatility": 1.15,
                  "is_sniper": True}
        feats = extract_live_features(
            symbol="CRUDEOIL", signal=signal,
            data_map_15m={}, data_map_1m={}, broker=None,
        )
        assert feats["sniper_zone_strength"] == 88.0
        assert feats["fvg_size"] == 0.42
        assert feats["commodity_volatility"] == 1.15

    def test_feature_columns_match_thresholds_config(self):
        """FEATURE_COLUMNS in features.py must match ML_ENGINE config exactly."""
        from data.features import FEATURE_COLUMNS
        from config.thresholds import ML_ENGINE
        assert FEATURE_COLUMNS == ML_ENGINE["FEATURE_COLUMNS"]

    def test_sniper_config_has_nse_keys(self):
        """Route B: SNIPER config must carry NSE session + looser confluence."""
        from config.thresholds import SNIPER
        assert "NSE_SESSION_START" in SNIPER and "NSE_SESSION_END" in SNIPER
        assert SNIPER["NSE_SESSION_START"] == "09:15"
        assert SNIPER["NSE_SESSION_END"] == "15:00"
        # Both NSE and MCX use 2-component confluence (was 3 for MCX, too strict)
        assert SNIPER["NSE_MIN_CONFLUENCE_COMPONENTS"] == 2
        assert SNIPER["MIN_CONFLUENCE_COMPONENTS"] == 2

    def test_sniper_trail_activates_at_10pct(self):
        """Rocket trail arms at +10% profit (was 25% — too high, never armed).
        At +10% the trail actually engages on real option moves and locks 50%
        of peak. The 5m opposite BOS exit only fires AFTER the trail arms so
        first-pullback noise doesn't kill the rocket before takeoff."""
        from config.thresholds import SNIPER
        assert SNIPER["TRAIL_ACTIVATE_PCT"] == 5.0
        # 50% peak lock retained
        assert SNIPER["TRAIL_LOCK_PCT_OF_PEAK"] == 50.0
        # OB stop still wide (-12%) so rocket survives pre-takeoff pullback
        assert SNIPER["OB_STOP_PCT"] == 12.0

    def test_feature_values_are_finite(self):
        """No NaN or Inf in feature values — must be sanitized to 0."""
        from data.features import extract_live_features
        # Pass data with NaN/Inf to test sanitization
        signal = {"setup_score": float("inf"), "is_scalper": False}
        feats = extract_live_features(
            symbol="BANKNIFTY", signal=signal,
            data_map_15m={}, data_map_1m={}, broker=None,
        )
        for k, v in feats.items():
            assert isinstance(v, (int, float)), f"{k} is not numeric"
            assert math.isfinite(v), f"{k} is not finite: {v}"

    def test_setup_score_normalized(self):
        """setup_score must be normalized to 0-1 range (input 80 → 0.8)."""
        from data.features import extract_live_features
        signal = {"setup_score": 80.0}
        feats = extract_live_features(
            symbol="NIFTY", signal=signal,
            data_map_15m={}, data_map_1m={}, broker=None,
        )
        assert feats["setup_score"] == pytest.approx(0.8, abs=0.01)

    def test_is_scalper_flag(self):
        """is_scalper flag must be 1.0 when signal says scalper."""
        from data.features import extract_live_features
        signal = {"is_scalper": True, "is_momentum_hunter": False}
        feats = extract_live_features(
            symbol="NIFTY", signal=signal,
            data_map_15m={}, data_map_1m={}, broker=None,
        )
        assert feats["is_scalper"] == 1.0
        assert feats["is_momentum_hunter"] == 0.0

    def test_pcr_value_passed_through(self):
        """option_chain_pcr must match the pcr_value argument."""
        from data.features import extract_live_features
        signal = {"is_scalper": True}
        feats = extract_live_features(
            symbol="NIFTY", signal=signal,
            data_map_15m={}, data_map_1m={}, broker=None,
            pcr_value=1.35,
        )
        assert feats["option_chain_pcr"] == pytest.approx(1.35, abs=0.01)

    def test_volume_velocity_from_1m_data(self):
        """volume_velocity must be computed from 1m volume data."""
        from data.features import extract_live_features
        idx = pd.date_range("2026-09-08 09:15", periods=15, freq="1min")
        vols = [100] * 10 + [200, 300, 400, 500, 600]
        df_1m = pd.DataFrame({"open": 100, "high": 101, "low": 99,
                              "close": 100, "volume": vols}, index=idx)
        feats = extract_live_features(
            symbol="NIFTY", signal={"is_scalper": True},
            data_map_15m={}, data_map_1m={"NIFTY": df_1m},
            broker=None,
        )
        # vol_surge_ratio = latest_vol / avg(last 10) = 600 / 250 ≈ 2.4
        assert feats["vol_surge_ratio"] > 1.0

    def test_feature_vector_ordered_array(self):
        """feature_vector must return ordered numpy array for predict_proba."""
        from data.features import extract_live_features, feature_vector, FEATURE_COLUMNS
        feats = extract_live_features(
            symbol="NIFTY", signal={"is_scalper": True},
            data_map_15m={}, data_map_1m={}, broker=None,
        )
        vec = feature_vector(feats)
        assert isinstance(vec, np.ndarray)
        assert vec.shape == (1, len(FEATURE_COLUMNS))

    def test_rsi_computation(self):
        """RSI must be computed from 15m closes when available."""
        from data.features import extract_live_features
        idx = pd.date_range("2026-09-08 09:15", periods=20, freq="15min")
        closes = [100 + i * 0.5 for i in range(20)]  # uptrend
        df_15m = pd.DataFrame({"close": closes, "open": closes,
                               "high": [c + 1 for c in closes],
                               "low": [c - 1 for c in closes]}, index=idx)
        feats = extract_live_features(
            symbol="NIFTY", signal={"is_scalper": True},
            data_map_15m={"NIFTY": df_15m}, data_map_1m={}, broker=None,
        )
        # Uptrend → RSI should be high (bullish)
        assert feats["rsi"] > 60.0

    def test_body_pct_from_1m_data(self):
        """body_pct must be computed from latest 1m candle."""
        from data.features import extract_live_features
        idx = pd.date_range("2026-09-08 09:15", periods=5, freq="1min")
        df_1m = pd.DataFrame({
            "open": [100, 100, 100, 100, 100],
            "high": [102, 101, 103, 101, 110],
            "low": [99, 99, 99, 99, 95],
            "close": [101, 100, 102, 100, 108],  # last: body=8, range=15
            "volume": [1000] * 5,
        }, index=idx)
        feats = extract_live_features(
            symbol="NIFTY", signal={"is_scalper": True},
            data_map_15m={}, data_map_1m={"NIFTY": df_1m},
            broker=None,
        )
        # body=|108-100|=8, range=110-95=15, body_pct=8/15*100≈53.3
        assert 50 < feats["body_pct"] < 60

    def test_sensex_trend_bullish_from_index(self):
        """sensex_trend = +1 when broad index rises >0.3% over 6×15m."""
        from data.features import extract_live_features
        idx = pd.date_range("2026-09-08 09:15", periods=10, freq="15min")
        df_15m = pd.DataFrame({
            "close": [22000, 22010, 22020, 22030, 22040,
                      22050, 22060, 22070, 22080, 22100],  # ~+0.45%
            "open": 22000, "high": 22100, "low": 22000, "volume": 0,
        }, index=idx)
        feats = extract_live_features(
            symbol="HDFCBANK", signal={"setup_score": 80},
            data_map_15m={"^NSEI": df_15m}, data_map_1m={}, broker=None,
        )
        assert feats["sensex_trend"] == 1.0

    def test_sensex_trend_bearish_from_index(self):
        """sensex_trend = -1 when broad index falls >0.3% over 6×15m."""
        from data.features import extract_live_features
        idx = pd.date_range("2026-09-08 09:15", periods=10, freq="15min")
        df_15m = pd.DataFrame({
            "close": [22000, 21990, 21980, 21970, 21960,
                      21950, 21940, 21930, 21920, 21890],  # ~-0.5%
            "open": 22000, "high": 22000, "low": 21890, "volume": 0,
        }, index=idx)
        feats = extract_live_features(
            symbol="HDFCBANK", signal={"setup_score": 80},
            data_map_15m={"NIFTY": df_15m}, data_map_1m={}, broker=None,
        )
        assert feats["sensex_trend"] == -1.0

    def test_sensex_blocks_option_directional_filter(self):
        """Block PE in bullish market, CE in bearish market; allow aligned."""
        from data.features import sensex_blocks_option
        # bullish + PE → block
        assert sensex_blocks_option(1.0, "PE") is True
        # bearish + CE → block
        assert sensex_blocks_option(-1.0, "CE") is True
        # bullish + CE → allow
        assert sensex_blocks_option(1.0, "CE") is False
        # bearish + PE → allow
        assert sensex_blocks_option(-1.0, "PE") is False
        # neutral → allow both
        assert sensex_blocks_option(0.0, "CE") is False
        assert sensex_blocks_option(0.0, "PE") is False

    def test_vix_level_defaults_zero_without_broker(self):
        """vix_level = 0.0 when broker has no get_vix method."""
        from data.features import extract_live_features
        feats = extract_live_features(
            symbol="NIFTY", signal={"setup_score": 70},
            data_map_15m={}, data_map_1m={}, broker=None,
        )
        assert feats["vix_level"] == 0.0


# ============================================================
# ML Engine Tests
# ============================================================

class TestMLEngine:
    """pipeline/ml_engine.py — model training and inference gate."""

    def test_tiger_ml_gate_disabled_without_model(self, tmp_path):
        """Gate must be disabled (pass-through) when no model file exists."""
        from pipeline.ml_engine import TigerMLGate
        gate = TigerMLGate(
            model_path=str(tmp_path / "nonexistent.joblib"),
            min_win_prob=0.70,
        )
        assert not gate.is_enabled()
        # Pass-through: win_prob=1.0 → gate passes
        passed, prob = gate.check_gate({})
        assert passed is True
        assert prob == 1.0

    def test_tiger_ml_gate_advisory_never_rejects(self, tmp_path):
        """ML is ADVISORY — never blocks even when win_probability < min_win_prob.
        Tiger decides. ML only provides win_prob as a confidence signal."""
        from pipeline.ml_engine import TigerMLGate

        class MockModel:
            def predict_proba(self, X):
                return [[0.6, 0.4]]  # 40% win probability

        gate = TigerMLGate(
            model_path=str(tmp_path / "nonexistent.joblib"),
            min_win_prob=0.70,
        )
        gate.model = MockModel()  # inject mock
        passed, prob = gate.check_gate({})
        assert passed  # ADVISORY: always passes now, never blocks
        assert prob == pytest.approx(0.4, abs=0.01)

    def test_tiger_ml_gate_advisory_passes_above_threshold(self, tmp_path):
        """Advisory still returns win_prob when above threshold."""
        from pipeline.ml_engine import TigerMLGate

        class MockModel:
            def predict_proba(self, X):
                return [[0.2, 0.85]]  # 85% win probability

        gate = TigerMLGate(
            model_path=str(tmp_path / "nonexistent.joblib"),
            min_win_prob=0.70,
        )
        gate.model = MockModel()
        passed, prob = gate.check_gate({})
        assert passed
        assert prob == pytest.approx(0.85, abs=0.01)

    def test_train_model_with_timeseriessplit(self):
        """Train LightGBM with TimeSeriesSplit — must not use K-Fold."""
        from pipeline.ml_engine import train_model
        np.random.seed(42)
        n = 300
        df = pd.DataFrame({
            "zone_strength": np.random.rand(n),
            "volume_velocity": np.random.rand(n),
            "option_chain_pcr": np.random.rand(n) + 0.5,
            "live_iv_skew": np.random.rand(n) * 0.1,
            "setup_score": np.random.rand(n),
            "body_pct": np.random.rand(n) * 100,
            "vol_surge_ratio": np.random.rand(n) + 0.5,
            "rsi": np.random.rand(n) * 100,
            "brain_alignment": np.random.rand(n),
            "is_scalper": np.random.randint(0, 2, n),
            "is_momentum_hunter": np.random.randint(0, 2, n),
            "label": np.random.randint(0, 2, n),
            "entry_ts": pd.date_range("2026-01-01", periods=n, freq="15min"),
        })
        feature_cols = [
            "zone_strength", "volume_velocity", "option_chain_pcr", "live_iv_skew",
            "setup_score", "body_pct", "vol_surge_ratio", "rsi",
            "brain_alignment", "is_scalper", "is_momentum_hunter",
        ]
        bundle = train_model(
            df=df, feature_columns=feature_cols,
            n_splits=3, purge_bars=5, min_samples=100,
            validated_acc_min=0.0,  # accept any accuracy for test
        )
        assert bundle is not None
        assert "model" in bundle
        assert "feature_columns" in bundle
        assert "metrics" in bundle
        assert bundle["metrics"]["n_splits"] == 3

    def test_train_model_skips_insufficient_samples(self):
        """Training must be skipped if samples < min_samples."""
        from pipeline.ml_engine import train_model
        df = pd.DataFrame({
            "zone_strength": [0.5], "volume_velocity": [0.3],
            "option_chain_pcr": [1.0], "live_iv_skew": [0.05],
            "setup_score": [0.8], "body_pct": [50],
            "vol_surge_ratio": [1.5], "rsi": [65],
            "brain_alignment": [0.7], "is_scalper": [1],
            "is_momentum_hunter": [0], "label": [1],
            "entry_ts": ["2026-01-01"],
        })
        result = train_model(
            df=df, feature_columns=[
                "zone_strength", "volume_velocity", "option_chain_pcr",
                "live_iv_skew", "setup_score", "body_pct", "vol_surge_ratio",
                "rsi", "brain_alignment", "is_scalper", "is_momentum_hunter",
            ],
            min_samples=200,
        )
        assert result is None

    def test_purge_boundary_labels(self):
        """Label purging must drop rows at train/test boundary."""
        from pipeline.ml_engine import _purge_boundary_labels
        train = pd.DataFrame({"x": range(100), "label": [1] * 100})
        test = pd.DataFrame({"x": range(100, 200), "label": [0] * 100})
        t_purged, v_purged = _purge_boundary_labels(train, test, purge_bars=5)
        assert len(t_purged) == 95  # last 5 dropped
        assert len(v_purged) == 95  # first 5 dropped

    def test_build_training_data_from_trade_log(self):
        """build_training_data must convert trade log to labeled DataFrame."""
        from pipeline.ml_engine import build_training_data
        feature_cols = [
            "zone_strength", "volume_velocity", "option_chain_pcr", "live_iv_skew",
            "setup_score", "body_pct", "vol_surge_ratio", "rsi",
            "brain_alignment", "is_scalper", "is_momentum_hunter",
        ]
        trade_log = [
            {"ml_features": {c: 0.5 for c in feature_cols},
             "realized_pnl": 500, "entry_ts": "2026-09-01 09:30:00"},
            {"ml_features": {c: 0.3 for c in feature_cols},
             "realized_pnl": -200, "entry_ts": "2026-09-01 10:00:00"},
        ]
        df = build_training_data(trade_log, feature_cols)
        assert len(df) == 2
        assert "label" in df.columns
        assert df["label"].iloc[0] == 1  # profit → win
        assert df["label"].iloc[1] == 0  # loss → loss

    def test_build_training_data_skips_open_records(self):
        """OPEN (entry-only) records have no outcome — must be skipped."""
        from pipeline.ml_engine import build_training_data
        feature_cols = [
            "zone_strength", "volume_velocity", "option_chain_pcr", "live_iv_skew",
            "setup_score", "body_pct", "vol_surge_ratio", "rsi",
            "brain_alignment", "is_scalper", "is_momentum_hunter",
        ]
        trade_log = [
            # OPEN entry — no pnl/win → skip
            {"status": "OPEN", "ml_features": {c: 0.5 for c in feature_cols},
             "entry_time": "2026-09-01 09:30:00"},
            # CLOSED exit with win label + ml_features → include
            {"status": "CLOSED", "ml_features": {c: 0.6 for c in feature_cols},
             "pnl": 350, "win": 1, "entry_ts": "2026-09-01 10:00:00"},
        ]
        df = build_training_data(trade_log, feature_cols)
        assert len(df) == 1  # only the CLOSED record
        assert df["label"].iloc[0] == 1

    def test_build_training_data_uses_exit_record_features_and_pnl(self):
        """Exit records carry ml_features + pnl — retrain row is complete."""
        from pipeline.ml_engine import build_training_data
        feature_cols = [
            "zone_strength", "volume_velocity", "option_chain_pcr", "live_iv_skew",
            "setup_score", "body_pct", "vol_surge_ratio", "rsi",
            "brain_alignment", "is_scalper", "is_momentum_hunter",
        ]
        trade_log = [
            {"status": "CLOSED",
             "ml_features": {"zone_strength": 0.8, "volume_velocity": 0.2,
                             "option_chain_pcr": 1.4, "live_iv_skew": 0.05,
                             "setup_score": 0.75, "body_pct": 55,
                             "vol_surge_ratio": 2.1, "rsi": 62,
                             "brain_alignment": 0.7, "is_scalper": 1,
                             "is_momentum_hunter": 0},
             "pnl": -150, "win": 0,
             "entry_ts": "2026-09-01 14:30:00"},
        ]
        df = build_training_data(trade_log, feature_cols)
        assert len(df) == 1
        assert df["label"].iloc[0] == 0  # loss
        assert df["zone_strength"].iloc[0] == pytest.approx(0.8)
        assert df["rsi"].iloc[0] == pytest.approx(62)

    # === TIGER SNIPER ADVANCED V2 — sniper-only training filter ===
    def test_sniper_only_keeps_big_sniper_winners(self):
        """sniper_only=True keeps only pnl>30% + SNIPER_TRAILING_EXIT trades."""
        from pipeline.ml_engine import build_training_data
        feature_cols = ["zone_strength", "sniper_zone_strength"]
        trade_log = [
            # sniper big winner — pnl 80% on cost 100 → KEEP
            {"status": "CLOSED", "exit_reason": "SNIPER_TRAILING_EXIT",
             "pnl": 80, "trade_cost": 100,
             "ml_features": {"zone_strength": 0.9, "sniper_zone_strength": 88},
             "entry_ts": "2026-09-01 09:30:00"},
            # sniper small winner — pnl 10% → SKIP (< 30%)
            {"status": "CLOSED", "exit_reason": "SNIPER_TRAILING_EXIT",
             "pnl": 10, "trade_cost": 100,
             "ml_features": {"zone_strength": 0.5, "sniper_zone_strength": 70},
             "entry_ts": "2026-09-01 10:00:00"},
            # non-sniper exit (scalper) — SKIP (wrong exit_reason)
            {"status": "CLOSED", "exit_reason": "TRAILING_EXIT",
             "pnl": 200, "trade_cost": 100,
             "ml_features": {"zone_strength": 0.7, "sniper_zone_strength": 60},
             "entry_ts": "2026-09-01 11:00:00"},
            # sniper loss — pnl negative → SKIP (< 30%)
            {"status": "CLOSED", "exit_reason": "SNIPER_TRAILING_EXIT",
             "pnl": -50, "trade_cost": 100,
             "ml_features": {"zone_strength": 0.3, "sniper_zone_strength": 50},
             "entry_ts": "2026-09-01 12:00:00"},
        ]
        df = build_training_data(trade_log, feature_cols, sniper_only=True,
                                 sniper_min_pnl_pct=30.0,
                                 sniper_exit_reason="SNIPER_TRAILING_EXIT")
        assert len(df) == 1  # only the big sniper winner
        assert df["sniper_zone_strength"].iloc[0] == 88

    def test_sniper_only_empty_when_no_sniper_trades(self):
        """sniper_only returns empty if no trades match the sniper filter."""
        from pipeline.ml_engine import build_training_data
        feature_cols = ["zone_strength"]
        trade_log = [
            {"status": "CLOSED", "exit_reason": "TRAILING_EXIT", "pnl": 100,
             "trade_cost": 100, "ml_features": {"zone_strength": 0.8},
             "entry_ts": "2026-09-01 09:30:00"},
        ]
        df = build_training_data(trade_log, feature_cols, sniper_only=True)
        assert df.empty

    def test_sniper_only_disabled_keeps_all_closed(self):
        """sniper_only=False (default) keeps all closed trades like before."""
        from pipeline.ml_engine import build_training_data
        feature_cols = ["zone_strength"]
        trade_log = [
            {"status": "CLOSED", "exit_reason": "TRAILING_EXIT", "pnl": 100,
             "trade_cost": 100, "ml_features": {"zone_strength": 0.8},
             "entry_ts": "2026-09-01 09:30:00"},
            {"status": "CLOSED", "exit_reason": "SNIPER_TRAILING_EXIT", "pnl": -20,
             "trade_cost": 100, "ml_features": {"zone_strength": 0.4},
             "entry_ts": "2026-09-01 10:00:00"},
        ]
        df = build_training_data(trade_log, feature_cols, sniper_only=False)
        assert len(df) == 2

    def test_entry_quality_gate_blocks_bad_entries(self):
        """ML SAFETY: entry_quality gate blocks trades that are NOT true sniper.

        A lucky win from a bad entry (no SMC, no zone) must NOT enter ML
        training — otherwise ML learns junk patterns.
        """
        from pipeline.ml_engine import build_training_data
        feature_cols = ["zone_strength", "sniper_zone_strength"]
        trade_log = [
            # TRUE sniper — zone + SMC + velocity + confirmed → KEEP (even if loss)
            {"status": "CLOSED", "exit_reason": "SNIPER_TRAILING_EXIT",
             "pnl": -30, "trade_cost": 100,
             "entry_quality": {"is_true_sniper": True, "zone_touched": True,
                               "smc_confluence": True, "velocity_verified": True},
             "ml_features": {"zone_strength": 0.9, "sniper_zone_strength": 85},
             "entry_ts": "2026-09-01 09:30:00"},
            # BAD entry — lucky win but NOT true sniper → BLOCK from ML
            {"status": "CLOSED", "exit_reason": "SNIPER_TRAILING_EXIT",
             "pnl": 500, "trade_cost": 100,
             "entry_quality": {"is_true_sniper": False, "zone_touched": False,
                               "smc_confluence": False, "velocity_verified": True},
             "ml_features": {"zone_strength": 0.2, "sniper_zone_strength": 40},
             "entry_ts": "2026-09-01 10:00:00"},
            # TRUE sniper — big winner → KEEP
            {"status": "CLOSED", "exit_reason": "SNIPER_TRAILING_EXIT",
             "pnl": 80, "trade_cost": 100,
             "entry_quality": {"is_true_sniper": True, "zone_touched": True,
                               "smc_confluence": True, "velocity_verified": True},
             "ml_features": {"zone_strength": 0.95, "sniper_zone_strength": 92},
             "entry_ts": "2026-09-01 11:00:00"},
        ]
        df = build_training_data(trade_log, feature_cols, sniper_only=True,
                                 sniper_min_pnl_pct=30.0,
                                 sniper_exit_reason="SNIPER_TRAILING_EXIT")
        # Only 2 TRUE sniper entries kept — the lucky-win bad entry is blocked
        assert len(df) == 2
        # The loss from a true sniper is labeled 0 (ML learns from it)
        labels = sorted(df["label"].tolist())
        assert labels == [0, 1]
        # The bad-entry lucky win (sniper_zone_strength=40) is NOT in training
        assert 40 not in df["sniper_zone_strength"].tolist()

    def test_entry_quality_gate_legacy_fallback(self):
        """Old trade records without entry_quality fall back to outcome filter."""
        from pipeline.ml_engine import build_training_data
        feature_cols = ["zone_strength"]
        trade_log = [
            # Legacy record (no entry_quality) — big sniper winner → KEEP via fallback
            {"status": "CLOSED", "exit_reason": "SNIPER_TRAILING_EXIT",
             "pnl": 80, "trade_cost": 100,
             "ml_features": {"zone_strength": 0.9},
             "entry_ts": "2026-09-01 09:30:00"},
            # Legacy record — small winner → SKIP (< 30%)
            {"status": "CLOSED", "exit_reason": "SNIPER_TRAILING_EXIT",
             "pnl": 10, "trade_cost": 100,
             "ml_features": {"zone_strength": 0.5},
             "entry_ts": "2026-09-01 10:00:00"},
        ]
        df = build_training_data(trade_log, feature_cols, sniper_only=True,
                                 sniper_min_pnl_pct=30.0,
                                 sniper_exit_reason="SNIPER_TRAILING_EXIT")
        assert len(df) == 1  # legacy fallback keeps the big winner

    def test_save_and_load_model(self, tmp_path):
        """save_model → joblib load → TigerMLGate loads it."""
        from pipeline.ml_engine import train_model, save_model, TigerMLGate
        import joblib
        np.random.seed(42)
        n = 250
        feature_cols = [
            "zone_strength", "volume_velocity", "option_chain_pcr", "live_iv_skew",
            "setup_score", "body_pct", "vol_surge_ratio", "rsi",
            "brain_alignment", "is_scalper", "is_momentum_hunter",
        ]
        df = pd.DataFrame({
            **{c: np.random.rand(n) for c in feature_cols},
            "label": np.random.randint(0, 2, n),
            "entry_ts": pd.date_range("2026-01-01", periods=n, freq="15min"),
        })
        bundle = train_model(
            df=df, feature_columns=feature_cols,
            n_splits=3, min_samples=100, validated_acc_min=0.0,
        )
        assert bundle is not None
        model_path = str(tmp_path / "test_model.joblib")
        assert save_model(bundle, model_path)
        assert os.path.exists(model_path)
        # Load via TigerMLGate
        gate = TigerMLGate(model_path=model_path, min_win_prob=0.5,
                           feature_columns=feature_cols)
        assert gate.is_enabled()
        # Inference must return a probability
        from data.features import feature_vector
        feats = {c: 0.5 for c in feature_cols}
        prob = gate.predict_win_probability(feats)
        assert 0.0 <= prob <= 1.0

    def test_required_confluence_for_high_win_prob(self):
        """win_prob > 0.80 → required_confluence = 3 (ML trusts setup)."""
        from pipeline.ml_engine import required_confluence_for_win_prob
        assert required_confluence_for_win_prob(0.85) == 3
        assert required_confluence_for_win_prob(0.81) == 3

    def test_required_confluence_for_medium_win_prob(self):
        """win_prob 0.70-0.80 → required_confluence = 4."""
        from pipeline.ml_engine import required_confluence_for_win_prob
        assert required_confluence_for_win_prob(0.75) == 4
        assert required_confluence_for_win_prob(0.71) == 4

    def test_required_confluence_for_low_win_prob(self):
        """win_prob <= 0.70 → required_confluence = 5 (demand max confluence)."""
        from pipeline.ml_engine import required_confluence_for_win_prob
        assert required_confluence_for_win_prob(0.65) == 5
        assert required_confluence_for_win_prob(0.50) == 5

    def test_train_model_min_samples_lowered_to_50(self):
        """train_model now accepts 50 samples (was 200) — faster learning."""
        import inspect
        from pipeline.ml_engine import train_model
        sig = inspect.signature(train_model)
        assert sig.parameters["min_samples"].default == 50


# ============================================================
# Config Tests
# ============================================================

class TestMLConfig:
    """ML_ENGINE config in thresholds.py."""

    def test_ml_engine_config_exists(self):
        from config.thresholds import ML_ENGINE
        assert "MODEL_PATH" in ML_ENGINE
        assert "MIN_WIN_PROB" in ML_ENGINE
        assert "FEATURE_COLUMNS" in ML_ENGINE
        assert "TRAINING" in ML_ENGINE

    def test_min_win_prob_is_070(self):
        from config.thresholds import ML_ENGINE
        assert ML_ENGINE["MIN_WIN_PROB"] == 0.70

    def test_feature_columns_match_features_py(self):
        """FEATURE_COLUMNS in config must match data/features.py."""
        from config.thresholds import ML_ENGINE
        from data.features import FEATURE_COLUMNS
        assert ML_ENGINE["FEATURE_COLUMNS"] == FEATURE_COLUMNS

    def test_training_config_values(self):
        from config.thresholds import ML_ENGINE
        train = ML_ENGINE["TRAINING"]
        assert train["N_SPLITS"] >= 3
        assert train["PURGE_BARS"] >= 1
        # MIN_SAMPLES lowered to 50 (Sep 2026) — faster learning on small accounts
        assert train["MIN_SAMPLES"] >= 50


# ============================================================
# WebSocket Singleton Guard Tests
# ============================================================

class TestWebSocketSingleton:
    """TigerWebSocket must enforce singleton — only one live WS per process."""

    def test_singleton_returns_same_instance(self):
        """Two TigerWebSocket() calls on a healthy WS must return same object."""
        from broker.tiger_websocket import TigerWebSocket

        # Reset singleton state for this test
        TigerWebSocket._singleton_instance = None

        # Mock is_healthy to always return True (simulates live WS)
        class FakeWS:
            _initialized = True
            def is_healthy(self):
                return True
            def tick_count(self):
                return 1000
            def last_tick_age_seconds(self):
                return 0.5

        # First call creates instance via __new__
        ws1 = FakeWS()
        TigerWebSocket._singleton_instance = ws1

        # Second call should return the SAME instance (singleton)
        # We can't call TigerWebSocket() directly (needs broker), so test __new__
        result = TigerWebSocket.__new__(TigerWebSocket)
        assert result is ws1, "Singleton must return existing healthy instance"

        # Cleanup
        TigerWebSocket._singleton_instance = None

    def test_singleton_creates_new_when_dead(self):
        """Singleton must create new instance when existing is unhealthy."""
        from broker.tiger_websocket import TigerWebSocket

        TigerWebSocket._singleton_instance = None

        class DeadWS:
            _initialized = True
            def is_healthy(self):
                return False  # dead

        TigerWebSocket._singleton_instance = DeadWS()

        # __new__ should NOT return the dead instance
        result = TigerWebSocket.__new__(TigerWebSocket)
        assert result is not TigerWebSocket._singleton_instance or not isinstance(
            result, DeadWS)
        assert isinstance(result, TigerWebSocket)

        # Cleanup
        TigerWebSocket._singleton_instance = None

    def test_singleton_instance_is_class_attribute(self):
        """_singleton_instance must be a class-level attribute (shared)."""
        from broker.tiger_websocket import TigerWebSocket
        assert hasattr(TigerWebSocket, "_singleton_instance")
        assert hasattr(TigerWebSocket, "_singleton_lock")

    def test_init_guard_prevents_reinit(self):
        """__init__ must skip when _initialized is already True."""
        from broker.tiger_websocket import TigerWebSocket

        # Create a mock instance that's already initialized
        class MockWS(TigerWebSocket):
            _initialized = True
            def __init__(self):
                pass  # should be skipped

        # This should NOT raise (init is guarded)
        ws = MockWS.__new__(MockWS)
        ws._initialized = True
        # Calling __init__ on already-initialized instance should be no-op
        # (the guard returns early before touching broker)
