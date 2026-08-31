"""
Missing-data handling ke tests — index spot candles (NIFTY token) mein
volume hamesha 0 aata hai. Purana code use "volume confirmation fail"
maan raha tha, jisse har sub-brain ki confidence structurally kat rahi
thi aur 2 saal ke real data pe 0 trades aaye.

Yahan ye pakka karte hain ki:
  - volume 0 = "data available nahi", "signal fail" nahi
  - VWAP volume ke bina bhi valid rehta hai (NaN nahi)
  - Meta-Brain score sirf participating brains ke weight se normalise ho
"""

import numpy as np
import pandas as pd

from backtest.engine import backtest_range, new_gate_stats, _record_gate_stats
from meta_brain.weighting import decide
from pipeline.stage1_scanner import run_scanner
from subbrains import breakout, mean_reversion, trend_follow


def make_ohlcv(n: int = 80, volume: int = 300_000, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    close = 20000 + rng.normal(0, 50, n).cumsum()

    df = pd.DataFrame(index=dates)
    df["close"] = close
    df["open"] = df["close"].shift(1).fillna(close[0])
    df["high"] = df[["open", "close"]].max(axis=1) + rng.uniform(10, 50, n)
    df["low"] = df[["open", "close"]].min(axis=1) - rng.uniform(10, 50, n)
    df["volume"] = volume
    return df


def test_zero_volume_detected_as_unavailable():
    assert trend_follow.has_volume_data(make_ohlcv()) is True
    assert trend_follow.has_volume_data(make_ohlcv(volume=0)) is False


def test_missing_volume_column_is_unavailable():
    df = make_ohlcv().drop(columns=["volume"])
    assert trend_follow.has_volume_data(df) is False


def test_vwap_is_finite_without_volume():
    vwap = trend_follow.calculate_vwap(make_ohlcv(volume=0))
    assert vwap.notna().all()
    assert (vwap > 0).all()


def test_vwap_is_rolling_not_cumulative():
    """Purana cumulative VWAP 2 saal purane bhaav ko pakde rehta tha."""
    df = make_ohlcv(n=300, volume=0)
    df["close"] = np.linspace(20000, 26000, len(df))
    df["high"] = df["close"] + 20
    df["low"] = df["close"] - 20
    vwap = trend_follow.calculate_vwap(df)
    # Rolling VWAP recent price ke paas rehna chahiye, series ke beech mein nahi
    assert abs(vwap.iloc[-1] - df["close"].iloc[-1]) < 1000


def test_redistribute_weight_keeps_total():
    weights = {"a": 0.5, "b": 0.3, "c": 0.2}
    trend_follow.redistribute_weight(weights, "b")
    assert "b" not in weights
    assert abs(sum(weights.values()) - 1.0) < 1e-9


def test_redistribute_missing_key_is_noop():
    weights = {"a": 0.6, "b": 0.4}
    trend_follow.redistribute_weight(weights, "zzz")
    assert weights == {"a": 0.6, "b": 0.4}


def test_subbrains_flag_unavailable_volume_instead_of_failing():
    df = make_ohlcv(volume=0)
    for module, kwargs in (
        (trend_follow, {"is_index": True}),
        (mean_reversion, {}),
        (breakout, {}),
    ):
        result = module.evaluate(df, current_regime="STRONG_TREND", **kwargs)
        joined = " ".join(result["conflicting_evidence"])
        assert "Volume data available nahi tha" in joined
        # missing data ko "volume kam tha" wale failure jaisa report nahi karna
        assert "x avg" not in joined


def test_zero_volume_does_not_cap_trend_follow_confidence():
    """Same price action, sirf volume gayab — confidence 0 nahi girni chahiye."""
    df_zero = make_ohlcv(volume=0)
    result = trend_follow.evaluate(df_zero, current_regime="STRONG_TREND")
    assert result["confidence"] > 0


def _vote(vote: str, confidence: float, regime_fit: int = 100, available: bool = True):
    return {
        "vote": vote,
        "confidence": confidence,
        "regime_fit": regime_fit,
        "data_available": available,
        "hard_veto": False,
    }


def test_score_normalised_by_participating_weight():
    """
    Vol-Arb bina IV feed ke chup hai — uska weight score ko neeche nahi
    kheenchna chahiye, warna threshold structurally kabhi cross nahi hota.
    """
    votes = {
        "trend_follow": _vote("BUY", 90),
        "mean_reversion": _vote("NO_TRADE", 0),
        "breakout": _vote("NO_TRADE", 0),
        "range_scalp": _vote("NO_TRADE", 0, regime_fit=5),
        "vol_arb": _vote("NO_TRADE", 0, regime_fit=0, available=False),
    }
    result = decide(votes, "STRONG_TREND")
    assert result["contributing_factors"]["vol_arb"]["participates"] is False
    assert result["participating_weight"] < 1.0
    # 90 confidence wala trend_follow ab 65 threshold cross kar sakta hai
    assert result["final_score"] > 65
    assert result["final_decision"] == "BUY"


def test_hard_veto_still_blocks_everything():
    votes = {
        "trend_follow": _vote("BUY", 95),
        "vol_arb": {"vote": "NO_TRADE", "confidence": 0, "hard_veto": True,
                    "reasoning_tags": ["Event ke pehle IV high"], "regime_fit": 100},
    }
    result = decide(votes, "STRONG_TREND")
    assert result["final_decision"] == "NO_TRADE"
    assert result["veto_triggered"] is True


def test_custom_score_threshold_is_respected():
    votes = {"trend_follow": _vote("BUY", 50)}
    strict = decide(votes, "STRONG_TREND", score_threshold=90)
    loose = decide(votes, "STRONG_TREND", score_threshold=40)
    assert strict["final_decision"] == "NO_TRADE"
    assert loose["final_decision"] == "BUY"


def test_zero_threshold_does_not_invent_a_direction():
    """Sab NO_TRADE ka raw sum 0 hai — threshold 0 pe bhi SELL nahi banna chahiye."""
    votes = {"trend_follow": _vote("NO_TRADE", 0)}
    flat = decide(votes, "STRONG_TREND", score_threshold=0)
    assert flat["final_decision"] == "NO_TRADE"

    # cancel hote votes bhi direction nahi bante (float residue ke saath bhi)
    # RANGE weights: trend_follow 0.10, mean_reversion 0.35 → 3.5*0.10 == 1.0*0.35
    tied = {
        "trend_follow": _vote("BUY", 3.5),
        "mean_reversion": _vote("SELL", 1.0),
    }
    assert decide(tied, "RANGE", score_threshold=0)["final_decision"] == "NO_TRADE"


def test_run_scanner_accepts_vix_positionally():
    """vix_series ka positional contract naye params se toota nahi chahiye."""
    df = make_ohlcv(volume=0)
    vix = pd.Series(14.0, index=df.index)
    result = run_scanner(df, vix)
    assert result["meta_brain_result"] is not None


def test_stage1_failure_is_not_logged_as_trade(monkeypatch):
    """stage1_min > score_threshold ho to Stage-1 pe ruka candidate trade nahi hai."""
    df = make_ohlcv(n=40)

    def fake_scanner(df_till_today, **kwargs):
        return {
            "passed_stage1": False,
            "regime": {"regime": "STRONG_TREND"},
            "sub_brain_votes": {},
            "meta_brain_result": {
                "final_decision": "BUY", "final_score": 50.0, "veto_triggered": False,
            },
        }

    monkeypatch.setattr("backtest.engine.run_scanner", fake_scanner)
    result = backtest_range(df, 30, len(df) - 1, score_threshold=40, stage1_min=90)
    assert result["total_trades"] == 0
    assert result["gate_stats"]["blocked_by"]["stage1_min_confidence"] > 0


def test_gate_stats_attributes_block_reason():
    stats = new_gate_stats()
    _record_gate_stats(stats, {
        "regime": {"regime": "STRONG_TREND"},
        "sub_brain_votes": {"trend_follow": {"vote": "BUY", "confidence": 40}},
        "meta_brain_result": {
            "final_decision": "NO_TRADE", "final_score": 30.0, "veto_triggered": False,
        },
    }, score_threshold=65)
    _record_gate_stats(stats, {
        "regime": {"regime": "HIGH_VOL"},
        "sub_brain_votes": {"trend_follow": {"vote": "NO_TRADE", "confidence": 0}},
        "meta_brain_result": {
            "final_decision": "NO_TRADE", "final_score": 100.0, "veto_triggered": True,
        },
    }, score_threshold=65)

    assert stats["days"] == 2
    assert stats["blocked_by"]["score_below_threshold"] == 1
    assert stats["blocked_by"]["vol_arb_hard_veto"] == 1
    assert stats["brain_max_confidence"]["trend_follow"] == 40.0
