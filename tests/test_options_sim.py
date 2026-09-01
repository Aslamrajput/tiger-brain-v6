"""
Options P&L simulation ke tests — sab synthetic, koi broker/internet nahi.

Sabse zaroori baat jo yahan pin ki gayi hai: DIRECTION SAHI HONE PAR BHI
LOSS ho sakta hai (theta + costs). Agar kabhi ye test toota, matlab
simulator theta ya cost khaana bhool gaya — aur tab backtest phir se
jhoothi umeed dene lagega.
"""

import numpy as np
import pandas as pd
import pytest

from backtest import cli, options_sim


def make_ohlcv(closes, start="2024-01-01") -> pd.DataFrame:
    dates = pd.date_range(start, periods=len(closes), freq="B")
    df = pd.DataFrame(index=dates)
    df["close"] = closes
    df["open"] = df["close"]
    df["high"] = df["close"] + 20
    df["low"] = df["close"] - 20
    df["volume"] = 300_000
    return df


def trade(date, decision="BUY", correct=True, index=0) -> dict:
    return {
        "date_index": index, "date": date, "decision": decision,
        "score": 70, "actual_direction": decision, "correct": correct,
    }


# ----------------------- Black-Scholes -----------------------

def test_call_put_parity():
    spot, strike, t, iv = 20000.0, 20000.0, 7 / 365, 0.14
    call = options_sim.black_scholes_price(spot, strike, t, iv, "CE")
    put = options_sim.black_scholes_price(spot, strike, t, iv, "PE")

    discounted_strike = strike * np.exp(-options_sim.DEFAULT_RISK_FREE_RATE * t)
    assert call - put == pytest.approx(spot - discounted_strike, abs=0.5)


def test_premium_decays_with_time():
    args = (20000.0, 20000.0)
    week = options_sim.black_scholes_price(*args, 7 / 365, 0.14, "CE")
    day = options_sim.black_scholes_price(*args, 1 / 365, 0.14, "CE")

    assert day < week  # theta


def test_premium_rises_with_iv():
    low = options_sim.black_scholes_price(20000.0, 20000.0, 7 / 365, 0.10, "CE")
    high = options_sim.black_scholes_price(20000.0, 20000.0, 7 / 365, 0.25, "CE")

    assert high > low


def test_deep_itm_call_is_at_least_intrinsic():
    price = options_sim.black_scholes_price(21000.0, 20000.0, 7 / 365, 0.14, "CE")
    assert price >= 1000.0


def test_invalid_option_type_rejected():
    with pytest.raises(ValueError):
        options_sim.black_scholes_price(20000.0, 20000.0, 0.02, 0.14, "CALL")


# ----------------------- strikes & expiry -----------------------

def test_atm_strike_rounds_to_step():
    assert options_sim.atm_strike(20037.0, 50) == 20050
    assert options_sim.atm_strike(20024.0, 50) == 20000
    assert options_sim.atm_strike(20037.0, 100) == 20000


def test_days_to_expiry_rolls_to_next_week_on_expiry_day():
    thursday = pd.Timestamp("2024-01-04")  # weekday 3
    assert thursday.weekday() == 3
    assert options_sim.days_to_expiry(thursday, expiry_weekday=3) == 7
    assert options_sim.days_to_expiry(pd.Timestamp("2024-01-02"), 3) == 2


# ----------------------- trade simulation -----------------------

def test_correct_direction_can_still_lose_money():
    """Chhoti si sahi move + theta + cost = phir bhi loss. Yahi wo cheez
    hai jo directional accuracy chhupa leti thi."""
    df = make_ohlcv([20000.0, 20005.0])
    result = options_sim.simulate_trade_log(df, [trade(df.index[0])])

    assert len(result["trades"]) == 1
    assert result["trades"][0]["direction_correct"] is True
    assert result["total_pnl"] < 0


def test_big_favourable_move_makes_money():
    df = make_ohlcv([20000.0, 20500.0])
    result = options_sim.simulate_trade_log(df, [trade(df.index[0])])

    assert result["total_pnl"] > 0
    assert result["trades"][0]["option_type"] == "CE"


def test_sell_decision_buys_put():
    df = make_ohlcv([20000.0, 19500.0])
    result = options_sim.simulate_trade_log(df, [trade(df.index[0], decision="SELL")])

    assert result["trades"][0]["option_type"] == "PE"
    assert result["total_pnl"] > 0


def test_costs_reduce_gross_pnl():
    df = make_ohlcv([20000.0, 20500.0])
    result = options_sim.simulate_trade_log(df, [trade(df.index[0])])
    t = result["trades"][0]

    assert t["costs"] > 0
    # har field alag round hoti hai, isliye 1 paisa tolerance
    assert t["net_pnl"] == pytest.approx(t["gross_pnl"] - t["costs"], abs=0.02)


def test_vix_series_drives_iv():
    df = make_ohlcv([20000.0, 20100.0])
    vix = pd.Series([28.0, 28.0], index=df.index)

    with_vix = options_sim.simulate_trade_log(df, [trade(df.index[0])], vix_series=vix)
    without = options_sim.simulate_trade_log(df, [trade(df.index[0])])

    assert with_vix["trades"][0]["iv_pct"] == 28.0
    assert with_vix["trades"][0]["premium_in"] > without["trades"][0]["premium_in"]


def test_trade_resolved_by_date_not_index():
    """Split-mode ka trade_log sliced df pe banta hai — uska date_index
    poore df pe galat hota hai, isliye date se resolve hona chahiye."""
    df = make_ohlcv([20000.0, 20100.0, 20200.0, 20300.0])
    entry = trade(df.index[2], index=0)  # jaanbujhkar galat date_index

    result = options_sim.simulate_trade_log(df, [entry])

    assert result["trades"][0]["entry_date"] == df.index[2]
    assert result["trades"][0]["exit_date"] == df.index[3]


def test_last_day_trade_skipped():
    df = make_ohlcv([20000.0, 20100.0])
    result = options_sim.simulate_trade_log(df, [trade(df.index[1])])

    assert result["trades"] == []


# ----------------------- summary & warnings -----------------------

def test_small_sample_and_iv_warnings():
    df = make_ohlcv([20000.0, 20100.0, 20200.0])
    result = options_sim.simulate_trade_log(df, [trade(df.index[0])])
    joined = " ".join(result["warnings"])

    assert "IV" in joined  # constant-IV limitation
    assert "20" in joined  # small sample threshold


def test_accuracy_vs_win_rate_gap_warning():
    """Har trade direction-correct par chhoti move — accuracy 100%,
    win rate 0%. Report ko ye farak khud batana chahiye."""
    closes = [20000.0 + i * 5 for i in range(12)]
    df = make_ohlcv(closes)
    log = [trade(df.index[i]) for i in range(len(closes) - 1)]

    result = options_sim.simulate_trade_log(df, log)

    assert result["directional_accuracy_pct"] == 100.0
    assert result["win_rate_pct"] == 0.0
    assert any("theta" in w for w in result["warnings"])


def test_summary_stats_and_drawdown():
    df = make_ohlcv([20000.0, 20500.0, 20000.0, 20500.0])
    log = [trade(df.index[i]) for i in range(3)]

    result = options_sim.simulate_trade_log(df, log)

    assert len(result["trades"]) == 3
    assert result["expectancy"] == pytest.approx(
        result["total_pnl"] / 3, abs=0.01
    )
    assert result["max_drawdown"] > 0  # beech wala losing trade
    assert result["total_costs"] > 0


def test_expired_option_has_no_time_value():
    itm = options_sim.black_scholes_price(20100.0, 20000.0, 0.0, 0.14, "CE")
    otm = options_sim.black_scholes_price(19900.0, 20000.0, 0.0, 0.14, "CE")

    assert itm == pytest.approx(100.0)
    assert otm == 0.0


def test_timezone_aware_prices_still_use_fetched_vix():
    """Price index tz-aware, VIX index naive — phir bhi asli VIX lagna
    chahiye, chupchaap 14% fallback nahi."""
    df = make_ohlcv([20000.0, 20100.0])
    df.index = df.index.tz_localize("Asia/Kolkata")
    vix = pd.Series([30.0, 30.0], index=df.index.tz_localize(None))

    result = options_sim.simulate_trade_log(df, [trade(df.index[0])], vix_series=vix)

    assert result["trades"][0]["iv_pct"] == 30.0


def test_all_winning_trades_report_infinite_profit_factor():
    df = make_ohlcv([20000.0, 20600.0, 21200.0])
    log = [trade(df.index[0]), trade(df.index[1])]

    result = options_sim.simulate_trade_log(df, log)

    assert result["win_rate_pct"] == 100.0
    assert result["profit_factor"] == float("inf")


@pytest.mark.parametrize(
    "kwargs",
    [{"lot_size": 0}, {"strike_step": 0}, {"expiry_weekday": 7}],
)
def test_invalid_instrument_settings_rejected(kwargs):
    df = make_ohlcv([20000.0, 20100.0])
    with pytest.raises(ValueError):
        options_sim.simulate_trade_log(df, [trade(df.index[0])], **kwargs)


@pytest.mark.parametrize(
    "flag", [["--strike-step", "0"], ["--lot-size", "0"], ["--expiry-weekday", "9"]]
)
def test_cli_rejects_bad_options_args_before_running(flag, tmp_path):
    """Validation backtest ke BAAD nahi, pehle honi chahiye."""
    csv_path = tmp_path / "prices.csv"
    make_ohlcv([20000.0, 20100.0]).to_csv(csv_path)

    with pytest.raises(SystemExit) as exc:
        cli.main(
            ["--source", "csv", "--csv-path", str(csv_path), "--no-vix", *flag]
        )

    assert exc.value.code == 2


def test_empty_trade_log_is_honest():
    result = options_sim.simulate_trade_log(make_ohlcv([20000.0, 20100.0]), [])

    assert result["trades"] == []
    assert result["total_pnl"] == 0.0
    assert any("Ek bhi trade" in w for w in result["warnings"])

# ----------------------- KADAM 4: IV dynamics + crush -----------------------

def test_exit_uses_its_own_vix_not_the_entry_iv():
    """VIX ka asli move P&L mein aana chahiye — pehle exit bhi entry IV pe pricing thi."""
    df = make_ohlcv([20000.0, 20000.0])
    crashing_vix = pd.Series([30.0, 12.0], index=df.index)
    steady_vix = pd.Series([30.0, 30.0], index=df.index)

    crashed = options_sim.simulate_trade_log(
        df, [trade(df.index[0])], vix_series=crashing_vix
    )["trades"][0]
    steady = options_sim.simulate_trade_log(
        df, [trade(df.index[0])], vix_series=steady_vix
    )["trades"][0]

    assert (crashed["iv_pct"], crashed["iv_out_pct"]) == (30.0, 12.0)
    # Spot hila hi nahi — sirf IV girne se premium girna chahiye
    assert crashed["premium_out"] < steady["premium_out"]
    assert crashed["net_pnl"] < steady["net_pnl"]


def test_iv_crush_only_haircuts_the_exit_leg():
    df = make_ohlcv([20000.0, 20100.0])
    vix = pd.Series([20.0, 20.0], index=df.index)

    no_crush = options_sim.simulate_trade_log(
        df, [trade(df.index[0])], vix_series=vix
    )["trades"][0]
    crushed = options_sim.simulate_trade_log(
        df, [trade(df.index[0])], vix_series=vix, iv_crush_pct=25.0
    )["trades"][0]

    assert crushed["iv_pct"] == no_crush["iv_pct"] == 20.0
    assert crushed["iv_out_pct"] == 15.0
    assert crushed["premium_in"] == no_crush["premium_in"]
    assert crushed["net_pnl"] < no_crush["net_pnl"]


def test_crush_sensitivity_is_monotonic_for_a_buyer():
    df = make_ohlcv([20000.0, 20100.0, 20250.0, 20400.0])
    vix = pd.Series([18.0] * len(df), index=df.index)
    log = [trade(df.index[i], index=i) for i in range(3)]

    rows = options_sim.crush_sensitivity(df, log, vix_series=vix)

    assert [r["iv_crush_pct"] for r in rows] == list(
        options_sim.IV_CRUSH_SENSITIVITY_LEVELS
    )
    pnls = [r["total_pnl"] for r in rows]
    assert pnls == sorted(pnls, reverse=True)  # zyada crush = buyer ko zyada nuksaan


def test_invalid_iv_crush_rejected():
    df = make_ohlcv([20000.0, 20100.0])
    with pytest.raises(ValueError):
        options_sim.simulate_trade_log(df, [trade(df.index[0])], iv_crush_pct=120.0)


def test_cli_rejects_bad_iv_crush_before_running(tmp_path):
    csv_path = tmp_path / "prices.csv"
    make_ohlcv([20000.0, 20100.0]).to_csv(csv_path)

    with pytest.raises(SystemExit) as exc:
        cli.main([
            "--source", "csv", "--csv-path", str(csv_path), "--no-vix",
            "--iv-crush-pct", "150",
        ])

    assert exc.value.code == 2
