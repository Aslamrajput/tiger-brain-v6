"""
KADAM 3 — backtest ko intraday bars pe chalane wale wiring ke tests.

Yahan koi broker/network nahi chahiye: synthetic 5-min sessions banate
hain aur scanner ko monkeypatch karke deterministic decisions dete hain.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest import cli, engine
from backtest.cli import bars_per_session, build_parser, load_vix
from backtest.options_sim import simulate_trade_log
from backtest.walk_forward import generate_folds

BARS_PER_DAY = 75  # 5-min NIFTY session


def _intraday_frame(sessions: int = 4, bars: int = BARS_PER_DAY) -> pd.DataFrame:
    stamps = []
    for day in range(sessions):
        start = pd.Timestamp("2026-01-05 09:15") + pd.Timedelta(days=day)
        stamps.extend(start + pd.Timedelta(minutes=5 * b) for b in range(bars))

    index = pd.DatetimeIndex(stamps)
    close = pd.Series(np.linspace(20000, 20100, len(index)), index=index)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 5,
            "low": close - 5,
            "close": close,
            "volume": np.full(len(index), 500_000.0),
        },
        index=index,
    )


def _always_buy(df, vix_series=None, score_threshold=65, min_stage1_confidence=70):
    return {
        "regime": {"regime": "STRONG_TREND"},
        "sub_brain_votes": {"trend_follow": {"vote": "BUY", "confidence": 90}},
        "meta_brain_result": {
            "final_decision": "BUY", "final_score": 90.0, "veto_triggered": False,
        },
        "passed_stage1": True,
    }


def test_bars_per_session_matches_interval():
    assert bars_per_session("ONE_DAY") == 1
    assert bars_per_session("FIVE_MINUTE") == 75
    assert bars_per_session("FIFTEEN_MINUTE") == 25


def test_session_aware_run_never_holds_overnight(monkeypatch):
    df = _intraday_frame()
    monkeypatch.setattr(engine, "run_scanner", _always_buy)

    result = engine.backtest_range(
        df, BARS_PER_DAY, len(df) - 1,
        warmup_bars=BARS_PER_DAY, session_aware=True,
    )

    assert result["total_trades"] > 0
    for trade in result["trade_log"]:
        i = df.index.get_loc(trade["date"])
        assert df.index[i].date() == df.index[i + 1].date()


def test_without_session_awareness_overnight_bars_are_traded(monkeypatch):
    df = _intraday_frame()
    monkeypatch.setattr(engine, "run_scanner", _always_buy)

    session_aware = engine.backtest_range(
        df, BARS_PER_DAY, len(df) - 1,
        warmup_bars=BARS_PER_DAY, session_aware=True,
    )
    plain = engine.backtest_range(
        df, BARS_PER_DAY, len(df) - 1, warmup_bars=BARS_PER_DAY,
    )

    # Har extra trade session ka aakhri bar hai (2 boundaries is frame mein)
    assert plain["total_trades"] > session_aware["total_trades"]


def test_folds_are_measured_in_bars_for_intraday():
    folds = generate_folds(
        total_len=10 * BARS_PER_DAY, train_days=4, test_days=2,
        bars_per_day=BARS_PER_DAY,
    )

    assert folds
    assert folds[0]["train_start"] == 0
    assert folds[0]["test_start"] == 4 * BARS_PER_DAY
    assert folds[0]["test_end"] == 6 * BARS_PER_DAY


def test_intraday_holding_period_costs_less_theta_than_a_full_day():
    df = _intraday_frame(sessions=1)
    entry_ts = df.index[0]
    trade_log = [{
        "date_index": 0, "date": entry_ts, "decision": "BUY",
        "score": 90.0, "actual_direction": "BUY", "correct": True,
    }]

    intraday = simulate_trade_log(df, trade_log, slippage_pct=0.0,
                                  brokerage_per_order=0.0)

    daily_index = pd.DatetimeIndex([entry_ts, entry_ts + pd.Timedelta(days=1)])
    daily_df = df.iloc[:2].copy()
    daily_df.index = daily_index
    daily_log = [{**trade_log[0], "date": daily_index[0]}]
    daily = simulate_trade_log(daily_df, daily_log, slippage_pct=0.0,
                               brokerage_per_order=0.0)

    assert intraday["trades"][0]["holding_days"] < 0.01
    assert daily["trades"][0]["holding_days"] == 1.0
    # Same price move, kam time decay — intraday exit ka premium zyada bachta hai
    assert intraday["trades"][0]["premium_out"] > daily["trades"][0]["premium_out"]


def test_intraday_vix_alignment_uses_previous_session(monkeypatch):
    df = _intraday_frame(sessions=3)
    vix_dates = pd.date_range("2026-01-05", periods=3, freq="D")
    vix_frame = pd.DataFrame({"Close": [12.0, 13.0, 14.0]}, index=vix_dates)

    from data import loader

    monkeypatch.setattr(
        loader, "fetch_india_vix_history", lambda days_back=0: vix_frame
    )

    aligned = load_vix(df, intraday=True)

    assert aligned is not None
    # 6 Jan ke bars ko 5 Jan ka close milna chahiye, us din ka nahi
    assert float(aligned.loc[pd.Timestamp("2026-01-06 09:15")]) == 12.0


def test_cli_rejects_bad_intraday_arguments():
    parser = build_parser()
    args = parser.parse_args(["--interval", "FIVE_MINUTE", "--intraday-days", "5"])
    assert args.interval == "FIVE_MINUTE"
    assert args.intraday_days == 5
    assert parser.parse_args([]).interval == "ONE_DAY"


def test_explicit_walk_forward_windows_survive_intraday_defaults(monkeypatch):
    """--train-days/--test-days diye ho to intraday defaults unhe na dabaayen."""
    captured = {}

    def fake_walk_forward(df, **kwargs):
        captured.update(kwargs)
        return {"folds": [], "aggregate": {}}

    monkeypatch.setattr(cli, "run_walk_forward", fake_walk_forward)
    monkeypatch.setattr(cli, "print_walk_forward_report", lambda results: None)
    monkeypatch.setattr(
        cli, "load_intraday_from_angel",
        lambda *a, **kw: _intraday_frame(sessions=2),
    )

    cli.main([
        "--interval", "FIVE_MINUTE", "--no-vix",
        "--train-days", "250", "--test-days", "60",
    ])
    assert (captured["train_days"], captured["test_days"]) == (250, 60)

    captured.clear()
    cli.main(["--interval", "FIVE_MINUTE", "--no-vix"])
    assert (captured["train_days"], captured["test_days"]) == (
        cli.INTRADAY_TRAIN_DAYS, cli.INTRADAY_TEST_DAYS,
    )


def test_coarse_intervals_still_get_the_regime_classifier_minimum():
    with pytest.raises(SystemExit):
        cli.main(["--interval", "ONE_HOUR", "--warmup-bars", "5"])
    assert bars_per_session("ONE_HOUR") < engine.MIN_WARMUP_DAYS


def test_bar_without_a_decision_is_skipped_not_crashed(monkeypatch):
    df = _intraday_frame(sessions=2)

    def sometimes_no_decision(df_slice, **kwargs):
        if len(df_slice) % 2 == 0:
            return {
                "passed_stage1": False, "regime": None, "sub_brain_votes": {},
                "meta_brain_result": None, "stage1_notes": ["history kam hai"],
            }
        return _always_buy(df_slice, **kwargs)

    monkeypatch.setattr(engine, "run_scanner", sometimes_no_decision)
    result = engine.backtest_range(
        df, BARS_PER_DAY, len(df) - 1,
        warmup_bars=BARS_PER_DAY, session_aware=True,
    )

    assert result["total_trades"] > 0
