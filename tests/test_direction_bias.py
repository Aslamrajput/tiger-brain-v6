"""
KADAM 1 — bias fix ke regression tests.

Pehle trend_follow/breakout neutral direction pe bhi `+1.0` (bullish)
inject karte the, aur vol_arb favorable-IV par directional "BUY" vote
deta tha. Dono se score structurally long-side jhukta tha.
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meta_brain.weighting import decide
from subbrains import breakout, trend_follow, vol_arb


def test_vol_arb_favorable_vote_is_not_directional():
    rng = np.random.default_rng(7)
    iv = pd.Series(rng.uniform(20, 30, 40))
    iv.iloc[-1] = 10  # aaj IV bahut low — favorable buying window

    result = vol_arb.evaluate(iv, hours_to_next_event=36)

    assert result["vote"] == "FAVORABLE"
    assert result["hard_veto"] is False


def test_meta_brain_favorable_vote_adds_no_direction():
    votes = {
        "vol_arb": {"vote": "FAVORABLE", "confidence": 75, "hard_veto": False},
    }
    result = decide(votes, "HIGH_VOL", score_threshold=65)

    assert result["final_decision"] == "NO_TRADE"
    assert result["raw_weighted_sum"] == 0.0
    # Brain data ke saath aaya tha, isliye denominator mein ginna chahiye
    assert result["participating_weight"] > 0


def _flat_prices(n: int = 80) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    close = pd.Series(np.full(n, 20000.0), index=idx)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 5,
            "low": close - 5,
            "close": close,
            "volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )


def test_trend_follow_unclear_supertrend_gives_no_bull_vote(monkeypatch):
    df = _flat_prices()

    def neutral_supertrend(frame, period=10, multiplier=3.0):
        return pd.DataFrame({"trend": np.zeros(len(frame))}, index=frame.index)

    monkeypatch.setattr(trend_follow, "calculate_supertrend", neutral_supertrend)
    result = trend_follow.evaluate(df, "STRONG_TREND", oi_buildup_confirmed=True)

    assert result["vote"] == "NO_TRADE"


def test_breakout_without_range_break_gives_no_bull_vote():
    # Flat series band ke andar hi rehti hai — koi breakout direction nahi
    result = breakout.evaluate(
        _flat_prices(), "COMPRESSION", oi_new_buildup_confirmed=True
    )
    assert result["vote"] == "NO_TRADE"
