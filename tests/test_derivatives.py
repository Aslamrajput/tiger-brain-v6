"""
Derivative feeds (futures volume + OI + real IV series) ke tests — sab
offline, koi Angel/network nahi.

Yahan jo cheezein PIN ki gayi hain wo teen hain:
  1. Missing feed missing hi rahe — na "confirm", na "confirm nahi hua".
  2. Koi lookahead na ho — bar `i` ke context mein `i` ke baad ka OI/IV
     bilkul na aaye.
  3. Contract roll pe do alag contracts ka OI compare na ho.
"""

from datetime import date, datetime

import pandas as pd
import pytest

from backtest import engine
from backtest.options_sim import black_scholes_price
from data import derivatives, iv_series


# ----------------------- helpers -----------------------

def futures_row(symbol, token, expiry, instrument="FUTIDX", name="NIFTY"):
    return {
        "token": token, "symbol": symbol, "name": name, "expiry": expiry,
        "strike": "-1.000000", "lotsize": "75",
        "instrumenttype": instrument, "exch_seg": "NFO",
    }


SAMPLE_MASTER = [
    futures_row("NIFTY29SEP26FUT", "1001", "29SEP2026"),
    futures_row("NIFTY27OCT26FUT", "1002", "27OCT2026"),
    # shor: doosra underlying, stock future, aur ek kharab token
    futures_row("BANKNIFTY29SEP26FUT", "2001", "29SEP2026", name="BANKNIFTY"),
    futures_row("RELIANCE29SEP26FUT", "3001", "29SEP2026", instrument="FUTSTK"),
    futures_row("NIFTY24NOV26FUT", "1003 4004", "24NOV2026"),
]


def bars(start, count, freq="5min", volume=None, price=100.0):
    index = pd.date_range(start, periods=count, freq=freq)
    return pd.DataFrame({
        "open": price, "high": price + 1, "low": price - 1, "close": price,
        "volume": 0 if volume is None else volume,
    }, index=index)


# ----------------------- master + registry -----------------------

def test_only_this_underlyings_index_futures_are_extracted():
    contracts = derivatives.extract_futures_contracts(SAMPLE_MASTER)

    assert set(contracts) == {"NIFTY29SEP26FUT", "NIFTY27OCT26FUT"}
    assert contracts["NIFTY29SEP26FUT"]["token"] == "1001"
    assert contracts["NIFTY29SEP26FUT"]["expiry"] == "2026-09-29"


def test_malformed_token_is_rejected_not_registered():
    """
    Angel ke feeds mein jude hue token ('1003 4004') aate dekhe hain —
    aisa token har request fail karta hai, isliye registry mein nahi
    jaana chahiye.
    """
    contracts = derivatives.extract_futures_contracts(SAMPLE_MASTER)

    assert "NIFTY24NOV26FUT" not in contracts


def test_registry_keeps_contracts_that_left_the_master(tmp_path):
    cache = str(tmp_path)
    derivatives.refresh_futures_registry(cache_dir=cache, master=SAMPLE_MASTER)
    # agla refresh: SEP wala expire ho kar master se gayab
    later = [row for row in SAMPLE_MASTER if row["token"] != "1001"]
    registry = derivatives.refresh_futures_registry(cache_dir=cache, master=later)

    assert "NIFTY29SEP26FUT" in registry  # purana token bacha rehna chahiye
    assert registry["NIFTY29SEP26FUT"]["token"] == "1001"


# ----------------------- contract selection -----------------------

def sample_registry():
    return derivatives.extract_futures_contracts(SAMPLE_MASTER)


def test_front_contract_is_the_nearest_unexpired_one():
    contract = derivatives.select_futures_contract(
        sample_registry(), date(2026, 9, 10)
    )
    assert contract["symbol"] == "NIFTY29SEP26FUT"


def test_roll_happens_before_expiry_day():
    """Expiry ke din volume/OI agle contract mein shift ho chuka hota hai."""
    contract = derivatives.select_futures_contract(
        sample_registry(), date(2026, 9, 29), roll_days=1
    )
    assert contract["symbol"] == "NIFTY27OCT26FUT"


def test_no_contract_after_registry_runs_out():
    assert derivatives.select_futures_contract(
        sample_registry(), date(2027, 1, 1)
    ) is None


# ----------------------- futures volume -----------------------

def test_futures_volume_replaces_spot_zero_volume_but_not_price():
    spot = bars("2026-09-01 09:15", 5, price=24000.0)
    futures = bars("2026-09-01 09:15", 5, volume=1500, price=24050.0)

    merged, coverage = derivatives.attach_futures_volume(spot, futures)

    assert (merged["close"] == 24000.0).all()   # price spot ka hi
    assert (merged["volume"] == 1500).all()     # volume futures ka
    assert coverage["matched"] == 5
    assert coverage["matched_pct"] == 100.0
    assert coverage["zero_volume_pct"] == 0.0


def test_bars_without_a_futures_candle_stay_missing_not_zero():
    """
    Volume 0 likhna 'koi trade nahi hua' ka jhoota dawa hai — sub-brain
    tab factor ko FAIL maanta hai. Missing ko missing rehna chahiye.
    """
    spot = bars("2026-09-01 09:15", 4)
    futures = bars("2026-09-01 09:15", 2, volume=900)

    merged, coverage = derivatives.attach_futures_volume(spot, futures)

    assert merged["volume"].isna().sum() == 2
    assert coverage["matched"] == 2
    assert coverage["matched_pct"] == 50.0


def test_no_futures_data_leaves_spot_untouched():
    spot = bars("2026-09-01 09:15", 3)
    merged, coverage = derivatives.attach_futures_volume(spot, pd.DataFrame())

    assert merged.equals(spot)
    assert coverage["matched"] == 0


# ----------------------- OI parsing + cache -----------------------

def test_oi_rows_are_parsed_into_tz_naive_ist_series():
    series = derivatives.parse_oi_rows([
        {"time": "2026-08-31T09:15:00+05:30", "oi": 1350180.0},
        {"time": "2026-08-31T09:20:00+05:30", "oi": 1354665.0},
    ])

    assert list(series.index) == [
        pd.Timestamp("2026-08-31 09:15"), pd.Timestamp("2026-08-31 09:20")
    ]
    assert series.index.tz is None
    assert series.iloc[-1] == 1354665.0


def test_oi_rows_with_unexpected_shape_raise_instead_of_silently_empty():
    with pytest.raises(ValueError):
        derivatives.parse_oi_rows([{"timestamp": "x", "openInterest": 1}])


def test_oi_cache_round_trip(tmp_path):
    series = derivatives.parse_oi_rows([
        {"time": "2026-08-31T09:15:00+05:30", "oi": 10.0},
    ])
    derivatives.save_cached_oi(series, "1001", "FIVE_MINUTE", cache_dir=str(tmp_path))
    loaded = derivatives.load_cached_oi(
        "1001", "FIVE_MINUTE", cache_dir=str(tmp_path)
    )

    assert loaded.equals(series)


def test_offline_oi_load_uses_cache_and_never_calls_broker(tmp_path):
    series = derivatives.parse_oi_rows([
        {"time": "2026-08-31T09:15:00+05:30", "oi": 10.0},
    ])
    derivatives.save_cached_oi(series, "1001", "FIVE_MINUTE", cache_dir=str(tmp_path))

    loaded = derivatives.load_oi_series(
        "1001", start=datetime(2026, 8, 31), end=datetime(2026, 9, 1),
        broker=None, cache_dir=str(tmp_path), offline=True,
    )
    assert len(loaded) == 1


# ----------------------- OI buildup flags -----------------------

def oi_series(values, start="2026-09-01 09:15"):
    index = pd.date_range(start, periods=len(values), freq="5min")
    return pd.Series(values, index=index, dtype="float64")


def test_rising_oi_is_buildup_and_falling_oi_is_not():
    rising = oi_series([100, 101, 102, 103, 104])
    flags = derivatives.oi_buildup_flags(
        rising, lookback_bars=2, min_change_pct=1.0
    )
    assert bool(flags.iloc[-1]) is True

    falling = oi_series([104, 103, 102, 101, 100])
    flags = derivatives.oi_buildup_flags(
        falling, lookback_bars=2, min_change_pct=1.0
    )
    assert bool(flags.iloc[-1]) is False


def test_bars_without_enough_lookback_have_no_flag_not_false():
    flags = derivatives.oi_buildup_flags(
        oi_series([100, 101, 102]), lookback_bars=2, min_change_pct=0.5
    )
    assert pd.isna(flags.iloc[0]) and pd.isna(flags.iloc[1])


def test_oi_across_a_contract_roll_is_not_compared():
    """Naye contract ka OI base alag hota hai — us jump ko buildup maan
    lena poora factor jhootha kar deta hai."""
    series = oi_series([100, 101, 102, 900, 950])
    contracts = pd.Series(
        ["1001", "1001", "1001", "1002", "1002"], index=series.index
    )
    flags = derivatives.oi_buildup_flags(
        series, lookback_bars=2, min_change_pct=1.0, contract_ids=contracts
    )

    assert pd.isna(flags.iloc[3])  # roll bar: comparison hi nahi
    assert pd.isna(flags.iloc[4])


# ----------------------- context (no lookahead) -----------------------

def iv_frame(values, start="2026-09-01 09:15"):
    index = pd.date_range(start, periods=len(values), freq="5min")
    return pd.DataFrame(
        {"atm_iv": values, "call_iv": values, "put_iv": [v + 1 for v in values]},
        index=index,
    )


def test_context_without_any_feed_reports_everything_missing():
    context = derivatives.DerivativeContext()
    kwargs = context.scanner_kwargs(pd.Timestamp("2026-09-01 09:15"))

    assert kwargs["oi_buildup_confirmed"] is None
    assert kwargs["oi_new_buildup_confirmed"] is None
    assert "iv_series" not in kwargs and "call_iv" not in kwargs


def test_context_passes_oi_flags_for_that_bar_only():
    series = oi_series([100, 101, 102, 103, 104, 105])
    context = derivatives.DerivativeContext(
        oi_series=series, trend_lookback_bars=2, trend_min_change_pct=1.0,
        breakout_lookback_bars=2, breakout_min_change_pct=50.0,
    )

    kwargs = context.scanner_kwargs(series.index[-1])
    assert kwargs["oi_buildup_confirmed"] is True
    assert kwargs["oi_new_buildup_confirmed"] is False
    # bar jo series mein hai hi nahi → missing, False nahi
    assert context.scanner_kwargs(
        pd.Timestamp("2026-09-05 09:15")
    )["oi_buildup_confirmed"] is None


def test_iv_context_never_shows_future_bars():
    frame = iv_frame([float(v) for v in range(30)])
    context = derivatives.DerivativeContext(iv_frame=frame)

    cutoff = frame.index[24]
    kwargs = context.scanner_kwargs(cutoff)

    assert kwargs["iv_series"].index.max() == cutoff
    assert len(kwargs["iv_series"]) == 25
    assert kwargs["call_iv"] == frame["call_iv"].loc[cutoff]
    assert kwargs["put_iv"] == frame["put_iv"].loc[cutoff]


def test_iv_series_is_withheld_until_vol_arb_has_enough_points():
    frame = iv_frame([float(v) for v in range(30)])
    context = derivatives.DerivativeContext(iv_frame=frame)

    kwargs = context.scanner_kwargs(frame.index[5])
    assert "iv_series" not in kwargs        # 6 points — Vol-Arb ke liye kam
    assert kwargs["call_iv"] is not None    # aaj ka IV phir bhi bata sakte hain


def test_summary_counts_what_actually_reached_the_scanner():
    frame = iv_frame([float(v) for v in range(30)])
    context = derivatives.DerivativeContext(
        oi_series=oi_series([100 + i for i in range(30)]),
        iv_frame=frame, trend_lookback_bars=2, trend_min_change_pct=0.1,
    )
    for timestamp in frame.index:
        context.scanner_kwargs(timestamp)

    summary = context.summary()
    assert summary["bars_evaluated"] == 30
    assert summary["bars_with_oi"] == 28   # pehle 2 bars pe lookback nahi
    assert summary["bars_with_iv"] == 11   # 20th point wale bar se aage


# ----------------------- engine wiring -----------------------

def test_engine_passes_bar_context_to_the_scanner(monkeypatch):
    """
    Backtest loop context se USI bar ka data maange, aur wahi scanner ko
    de — ye poore KADAM 3 ka jod hai.
    """
    df = bars("2026-09-01 09:15", 40, price=24000.0)
    seen = []

    def fake_scanner(frame, **kwargs):
        seen.append((frame.index[-1], kwargs.get("oi_buildup_confirmed")))
        return {
            "passed_stage1": False, "regime": {"regime": "SIDEWAYS"},
            "sub_brain_votes": {}, "meta_brain_result": None,
            "stage1_notes": [],
        }

    monkeypatch.setattr(engine, "run_scanner", fake_scanner)

    class Context:
        def scanner_kwargs(self, timestamp):
            return {"oi_buildup_confirmed": True, "oi_new_buildup_confirmed": False}

    engine.backtest_range(df, 30, 35, warmup_bars=30, context=Context())

    assert seen and all(flag is True for _, flag in seen)
    assert [ts for ts, _ in seen] == list(df.index[30:35])


# ----------------------- IV inversion -----------------------

def test_implied_volatility_recovers_the_input_vol():
    price = black_scholes_price(24000, 24000, 7 / 365, 0.14, "CE")
    recovered = iv_series.implied_volatility(price, 24000, 24000, 7 / 365, "CE")

    assert recovered == pytest.approx(0.14, abs=1e-3)


def test_impossible_premium_gives_no_iv_instead_of_a_made_up_one():
    # intrinsic se bhi neeche ka stale close — isse koi IV banta hi nahi
    assert iv_series.implied_volatility(1.0, 24000, 20000, 7 / 365, "CE") is None
    assert iv_series.implied_volatility(0.0, 24000, 24000, 7 / 365, "CE") is None


class FakeProvider:
    """Sirf kuch bars pe asli bhaav deta hai — baaki pe kuch nahi."""

    def __init__(self, priced_timestamps, expiry="2026-09-08", iv=0.15):
        self.priced = set(priced_timestamps)
        self.expiry = expiry
        self.iv = iv

    def contract_for(self, timestamp, strike, option_type):
        if pd.Timestamp(timestamp) not in self.priced:
            return None
        return {"expiry": self.expiry, "strike": strike, "option_type": option_type}

    def premium(self, timestamp, strike, option_type):
        if pd.Timestamp(timestamp) not in self.priced:
            return None
        t_years = iv_series.time_to_expiry_years(
            timestamp, date.fromisoformat(self.expiry)
        )
        return black_scholes_price(24000.0, strike, t_years, self.iv, option_type)


def test_atm_iv_series_is_built_from_real_premiums():
    df = bars("2026-09-01 09:15", 6, price=24000.0)
    frame = iv_series.build_atm_iv_series(df, FakeProvider(df.index))

    assert frame["atm_iv"].notna().all()
    assert frame["atm_iv"].iloc[0] == pytest.approx(15.0, abs=0.2)


def test_bars_without_a_real_option_price_stay_nan():
    df = bars("2026-09-01 09:15", 6, price=24000.0)
    frame = iv_series.build_atm_iv_series(df, FakeProvider(df.index[:2]))

    assert frame["atm_iv"].notna().sum() == 2
    assert frame["atm_iv"].iloc[-1] != frame["atm_iv"].iloc[-1]  # NaN


def test_sampling_carries_the_last_real_reading_forward_only():
    df = bars("2026-09-01 09:15", 6, price=24000.0)
    frame = iv_series.build_atm_iv_series(
        df, FakeProvider(df.index), sample_every_bars=3
    )

    # bar 0 aur 3 pe asli reading, beech ke bars us reading ko carry karte
    # hain — aage se peeche kuch nahi aata
    assert frame["atm_iv"].iloc[1] == frame["atm_iv"].iloc[0]
    assert frame["atm_iv"].iloc[4] == frame["atm_iv"].iloc[3]


# ----------------------- live greeks -----------------------

GREEK_ROWS = [
    {"name": "NIFTY", "expiry": "29SEP2026", "strikePrice": "24000.000000",
     "optionType": "CE", "delta": "0.5", "gamma": "0.0005", "theta": "-5.0",
     "vega": "22.1", "impliedVolatility": "12.5", "tradeVolume": "100"},
    {"name": "NIFTY", "expiry": "29SEP2026", "strikePrice": "24000.000000",
     "optionType": "PE", "delta": "-0.5", "gamma": "0.0005", "theta": "-4.0",
     "vega": "21.0", "impliedVolatility": "13.5", "tradeVolume": "90"},
]


class FakeSmartApi:
    def __init__(self, response):
        self.response = response

    def optionGreek(self, params):
        self.params = params
        return self.response


class FakeBroker:
    def __init__(self, response):
        self.smart_api = FakeSmartApi(response)


def test_live_greeks_are_parsed_and_atm_ivs_extracted():
    broker = FakeBroker({"status": True, "data": GREEK_ROWS})
    greeks = iv_series.fetch_live_greeks(broker, "NIFTY", date(2026, 9, 29))
    atm = iv_series.live_atm_iv(greeks, spot=24010.0)

    assert broker.smart_api.params == {"name": "NIFTY", "expirydate": "29SEP2026"}
    assert atm["strike"] == 24000.0
    assert atm["call_iv"] == 12.5 and atm["put_iv"] == 13.5
    assert atm["atm_iv"] == 13.0


def test_empty_greek_response_gives_no_iv():
    broker = FakeBroker({"status": False, "message": "no data", "data": []})
    greeks = iv_series.fetch_live_greeks(broker, "NIFTY", date(2026, 9, 29))

    assert greeks.empty
    assert iv_series.live_atm_iv(greeks, spot=24010.0)["atm_iv"] is None


# ----------------------- single Angel session -----------------------

def test_cli_logs_in_only_once_per_run(monkeypatch):
    """
    Candles aur derivative feeds alag-alag login karein to Angel seedha
    'exceeding access rate' de deta hai (EC2 pe hua tha).
    """
    from backtest import cli

    logins = []

    class FakeBroker:
        def login(self):
            logins.append(1)

    monkeypatch.setattr(cli, "_BROKER", None)
    monkeypatch.setitem(
        __import__("sys").modules, "broker.angel_connect",
        type("module", (), {"AngelBroker": FakeBroker}),
    )

    first = cli._login_broker()
    second = cli._login_broker()

    assert first is second
    assert len(logins) == 1
