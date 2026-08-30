"""
Walk-forward validation ke tests — sab synthetic data pe, koi broker
login ya internet ki zaroorat nahi.

Sabse zaroori test yahan `test_no_lookahead_in_folds` hai: wo pakka
karta hai ki kisi bhi din ka decision lete waqt future ka ek bhi row
scanner ko nahi diya gaya. Backtest mein lookahead bug ka matlab hai
"fake accuracy" — result accha dikhega par live mein kaam nahi karega.
"""

import numpy as np
import pandas as pd
import pytest

from backtest import walk_forward
from backtest.engine import MIN_WARMUP_DAYS, backtest_range


def make_ohlcv(n: int, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2023-01-02", periods=n, freq="B")
    close = 20000 + rng.normal(0, 60, n).cumsum()

    df = pd.DataFrame(index=dates)
    df["close"] = close
    df["open"] = df["close"].shift(1).fillna(close[0])
    df["high"] = df[["open", "close"]].max(axis=1) + rng.uniform(10, 60, n)
    df["low"] = df[["open", "close"]].min(axis=1) - rng.uniform(10, 60, n)
    df["volume"] = rng.integers(200_000, 400_000, n)
    return df


# ----------------------- generate_folds -----------------------

def test_folds_cover_data_without_overlap():
    folds = walk_forward.generate_folds(total_len=500, train_days=250, test_days=60)

    assert len(folds) == 5  # 250..499 ko 60-60 karke, aakhri chhota
    assert folds[0]["test_start"] == 250
    for prev, nxt in zip(folds, folds[1:]):
        assert prev["test_end"] == nxt["test_start"]  # na gap, na overlap
    assert folds[-1]["test_end"] == 499  # last din evaluate nahi hota


def test_rolling_vs_anchored_train_window():
    rolling = walk_forward.generate_folds(400, train_days=200, test_days=50)
    anchored = walk_forward.generate_folds(
        400, train_days=200, test_days=50, anchored=True
    )

    assert [f["train_start"] for f in rolling] == [0, 50, 100, 150]
    assert all(f["train_start"] == 0 for f in anchored)
    assert all(
        f["train_end"] - f["train_start"] == 200 for f in rolling
    )  # rolling window ka size fixed rehta hai


def test_short_data_gives_no_folds():
    assert walk_forward.generate_folds(100, train_days=250, test_days=60) == []


def test_train_days_below_warmup_rejected():
    with pytest.raises(ValueError):
        walk_forward.generate_folds(500, train_days=MIN_WARMUP_DAYS - 1, test_days=60)


# ----------------------- no lookahead -----------------------

def test_no_lookahead_in_folds(monkeypatch):
    """
    Har call pe scanner ko jo df mila, uska aakhri index decision-din se
    aage nahi hona chahiye. Agar kabhi bhi aage nikla, matlab future data
    leak ho raha hai.
    """
    df = make_ohlcv(200)
    seen_last_dates = []

    def fake_scanner(df_till_today, vix_series=None, **kwargs):
        seen_last_dates.append(df_till_today.index[-1])
        return {"meta_brain_result": {"final_decision": "BUY", "final_score": 70}}

    monkeypatch.setattr("backtest.engine.run_scanner", fake_scanner)

    result = walk_forward.run_walk_forward(df, train_days=60, test_days=30)

    decision_dates = [t["date"] for f in result["folds"] for t in f["trade_log"]]
    assert decision_dates  # kuch to chala hona chahiye
    assert seen_last_dates == decision_dates


def test_test_window_gets_warmup_from_train_window(monkeypatch):
    """
    Fold ka pehla test-din bhi poori history ke saath evaluate hona
    chahiye (train window se warmup), warna har fold ke shuru ke 30 din
    bekaar chale jaate.
    """
    df = make_ohlcv(200)
    history_lengths = []

    def fake_scanner(df_till_today, vix_series=None, **kwargs):
        history_lengths.append(len(df_till_today))
        return {"meta_brain_result": {"final_decision": "NO_TRADE", "final_score": 10}}

    monkeypatch.setattr("backtest.engine.run_scanner", fake_scanner)

    walk_forward.run_walk_forward(df, train_days=60, test_days=30)

    assert history_lengths[0] == 61  # fold 1 ka pehla test din = index 60
    assert min(history_lengths) > MIN_WARMUP_DAYS


# ----------------------- aggregation & warnings -----------------------

def _stub_scanner(decisions_by_index, full_df=None):
    def fake_scanner(df_till_today, vix_series=None, **kwargs):
        idx = (
            full_df.index.get_loc(df_till_today.index[-1])
            if full_df is not None
            else len(df_till_today) - 1
        )
        return {
            "meta_brain_result": {
                "final_decision": decisions_by_index.get(idx, "NO_TRADE"),
                "final_score": 70,
            }
        }
    return fake_scanner


def test_aggregate_counts_only_test_windows(monkeypatch):
    df = make_ohlcv(200)
    monkeypatch.setattr(
        "backtest.engine.run_scanner",
        _stub_scanner({i: "BUY" for i in range(200)}, full_df=df),
    )

    result = walk_forward.run_walk_forward(df, train_days=60, test_days=30)

    per_fold_trades = sum(f["total_trades"] for f in result["folds"])
    per_fold_correct = sum(f["correct_direction"] for f in result["folds"])
    assert result["aggregate"]["total_trades"] == per_fold_trades
    assert result["aggregate"]["correct_direction"] == per_fold_correct
    # train ke 60 din kabhi evaluate nahi hone chahiye
    assert all(t["date_index"] >= 60 for f in result["folds"] for t in f["trade_log"])


def test_warns_when_too_few_folds_and_trades(monkeypatch):
    df = make_ohlcv(120)
    monkeypatch.setattr(
        "backtest.engine.run_scanner", _stub_scanner({60: "BUY", 61: "SELL"})
    )

    result = walk_forward.run_walk_forward(df, train_days=60, test_days=60)

    joined = " ".join(result["warnings"])
    assert "fold" in joined
    assert "trades" in joined


def test_no_folds_returns_honest_warning():
    result = walk_forward.run_walk_forward(make_ohlcv(50), train_days=250, test_days=60)

    assert result["folds"] == []
    assert result["aggregate"]["total_trades"] == 0
    assert result["consistency"] is None
    assert len(result["warnings"]) == 1


def test_consistency_spread_flags_unstable_result(monkeypatch):
    """Ek fold perfect, doosra bilkul galat — average theek dikhega, par
    spread warning aani chahiye."""
    df = make_ohlcv(200)

    def fake_scanner(df_till_today, vix_series=None, **kwargs):
        i = df.index.get_loc(df_till_today.index[-1])
        actual_up = df["close"].iloc[i + 1] > df["close"].iloc[i]
        in_first_fold = i < 90
        correct_call = actual_up if in_first_fold else not actual_up
        return {
            "meta_brain_result": {
                "final_decision": "BUY" if correct_call else "SELL",
                "final_score": 70,
            }
        }

    monkeypatch.setattr("backtest.engine.run_scanner", fake_scanner)

    result = walk_forward.run_walk_forward(df, train_days=60, test_days=30)

    assert result["consistency"]["best_fold_accuracy_pct"] == 100.0
    assert result["consistency"]["worst_fold_accuracy_pct"] == 0.0
    assert any("spread" in w or "farak" in w for w in result["warnings"])


# ----------------------- engine.backtest_range -----------------------

def test_backtest_range_clamps_to_last_evaluable_day(monkeypatch):
    df = make_ohlcv(100)
    monkeypatch.setattr(
        "backtest.engine.run_scanner", _stub_scanner({i: "BUY" for i in range(100)})
    )

    result = backtest_range(df, 50, 500)  # eval_end jaanbujhkar bahar

    assert result["total_days_tested"] == 49  # 50..98, last din chhoda
    assert max(t["date_index"] for t in result["trade_log"]) == 98


# ----------------------- rolling window & fold dates -----------------------

def test_rolling_folds_drop_old_history(monkeypatch):
    """Rolling mode mein scanner ko sirf train_days ki history milni
    chahiye — warna rolling aur anchored ek jaise ho jaate hain."""
    df = make_ohlcv(200)

    def record(lengths):
        def fake_scanner(df_till_today, vix_series=None, **kwargs):
            lengths.append(len(df_till_today))
            return {
                "meta_brain_result": {"final_decision": "NO_TRADE", "final_score": 10}
            }
        return fake_scanner

    rolling_lengths, anchored_lengths = [], []

    monkeypatch.setattr("backtest.engine.run_scanner", record(rolling_lengths))
    walk_forward.run_walk_forward(df, train_days=60, test_days=30)

    monkeypatch.setattr("backtest.engine.run_scanner", record(anchored_lengths))
    walk_forward.run_walk_forward(df, train_days=60, test_days=30, anchored=True)

    # rolling: har fold ke shuru pe history phir se train_days jitni
    assert max(rolling_lengths) == 60 + 30
    assert anchored_lengths[-1] > rolling_lengths[-1]


def test_fold_end_date_is_last_evaluated_day():
    df = make_ohlcv(200)
    folds = walk_forward.generate_folds(len(df), train_days=60, test_days=30)
    results = walk_forward.run_walk_forward(df, train_days=60, test_days=30)["folds"]

    for spec, fold in zip(folds, results):
        # test_end exclusive hai — report mein aakhri EVALUATED din aana chahiye
        assert fold["test_end_date"] == df.index[spec["test_end"] - 1]
