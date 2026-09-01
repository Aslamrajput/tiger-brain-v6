"""
Real option-chain layer ke tests — sab offline (koi Angel/network nahi).

Sabse zaroori baat jo yahan pin ki gayi hai: jab asli bhaav na mile to
system chupchaap model-price pe fisal kar "real" ka dawa na kare. Trade
par `pricing` likha hota hai aur coverage report miss ginti hai.
"""

import json
from datetime import date

import pandas as pd
import pytest

from backtest import options_sim
from data import option_chain


def master_row(symbol, token, expiry, strike, lotsize=75):
    return {
        "token": token, "symbol": symbol, "name": "NIFTY", "expiry": expiry,
        "strike": f"{strike * 100:.6f}", "lotsize": str(lotsize),
        "instrumenttype": "OPTIDX", "exch_seg": "NFO",
    }


SAMPLE_MASTER = [
    master_row("NIFTY08SEP2620000CE", "111", "08SEP2026", 20000),
    master_row("NIFTY08SEP2620000PE", "112", "08SEP2026", 20000),
    master_row("NIFTY15SEP2620000CE", "113", "15SEP2026", 20000),
    # shor: doosra underlying aur equity option
    {**master_row("BANKNIFTY08SEP2650000CE", "999", "08SEP2026", 50000),
     "name": "BANKNIFTY"},
    {**master_row("RELIANCE08SEP263000CE", "888", "08SEP2026", 3000),
     "name": "RELIANCE", "instrumenttype": "OPTSTK"},
]


# ----------------------- master parsing -----------------------

def test_only_this_underlyings_index_options_are_extracted():
    contracts = option_chain.extract_option_contracts(SAMPLE_MASTER)

    assert set(contracts) == {
        "NIFTY08SEP2620000CE", "NIFTY08SEP2620000PE", "NIFTY15SEP2620000CE"
    }
    ce = contracts["NIFTY08SEP2620000CE"]
    assert ce["token"] == "111"
    assert ce["strike"] == 20000.0  # master paise mein deta hai
    assert ce["expiry"] == "2026-09-08"
    assert ce["option_type"] == "CE"


# ----------------------- registry -----------------------

def test_registry_keeps_contracts_that_have_left_the_master(tmp_path):
    """
    Angel ke master mein sirf ZINDA contracts hote hain. Agar registry
    expire hue contracts bhool jaaye to unka real bhaav dobara kabhi
    nahi mil sakta — yahi is layer ka poora point hai.
    """
    cache = str(tmp_path)
    option_chain.refresh_registry(cache_dir=cache, master=SAMPLE_MASTER)

    # agle din 08SEP wala expire ho gaya, master se gayab
    later = [r for r in SAMPLE_MASTER if not r["symbol"].startswith("NIFTY08SEP")]
    registry = option_chain.refresh_registry(cache_dir=cache, master=later)

    assert "NIFTY08SEP2620000CE" in registry
    assert registry["NIFTY08SEP2620000CE"]["token"] == "111"

    on_disk = json.loads(
        (tmp_path / "option_registry_NIFTY_NFO.json").read_text()
    )
    assert "NIFTY08SEP2620000CE" in on_disk


def test_a_far_away_expiry_is_not_used_as_that_days_weekly(tmp_path):
    """
    Purani weekly registry se pehle hi gayab ho chuki ho to agli zinda
    expiry uthana ek ALAG contract (zyada DTE) pe P&L banata hai.
    """
    registry = option_chain.refresh_registry(
        cache_dir=str(tmp_path), master=SAMPLE_MASTER
    )
    provider = option_chain.AngelOptionChain(
        cache_dir=str(tmp_path), registry=registry, offline=True
    )

    # us din ki asli weekly gayab hai; 08SEP 20 din door hai
    assert provider.contract_for(
        pd.Timestamp("2026-08-19 09:20"), 20000.0, "CE"
    ) is None
    assert provider.contract_for(
        pd.Timestamp("2026-09-07 09:20"), 20000.0, "CE"
    )["token"] == "111"


def test_nearest_expiry_and_contract_lookup(tmp_path):
    registry = option_chain.refresh_registry(
        cache_dir=str(tmp_path), master=SAMPLE_MASTER
    )

    expiry = option_chain.nearest_expiry_on_or_after(
        registry, pd.Timestamp("2026-09-09").date()
    )
    assert expiry.isoformat() == "2026-09-15"

    contract = option_chain.find_contract(registry, expiry, 20000.0, "CE")
    assert contract["token"] == "113"
    assert option_chain.find_contract(registry, expiry, 20000.0, "PE") is None


# ----------------------- provider -----------------------

def build_provider(monkeypatch, candles: dict, tmp_path):
    registry = option_chain.refresh_registry(
        cache_dir=str(tmp_path), master=SAMPLE_MASTER
    )
    provider = option_chain.AngelOptionChain(
        cache_dir=str(tmp_path), registry=registry, offline=True
    )
    monkeypatch.setattr(
        provider, "_load_contract_candles",
        lambda contract: candles.get(contract["token"], pd.DataFrame()),
    )
    return provider


def option_candles(timestamps, closes):
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes,
         "close": closes, "volume": [100] * len(closes)},
        index=pd.to_datetime(timestamps),
    )


def test_provider_returns_the_traded_close(monkeypatch, tmp_path):
    candles = {"111": option_candles(
        ["2026-09-07 09:20", "2026-09-07 09:25"], [120.0, 135.5]
    )}
    provider = build_provider(monkeypatch, candles, tmp_path)

    assert provider.premium(pd.Timestamp("2026-09-07 09:25"), 20000.0, "CE") == 135.5
    assert provider.coverage()["hits"] == 1


def test_missing_bar_is_a_miss_not_a_guess(monkeypatch, tmp_path):
    candles = {"111": option_candles(["2026-09-07 09:20"], [120.0])}
    provider = build_provider(monkeypatch, candles, tmp_path)

    assert provider.premium(pd.Timestamp("2026-09-07 09:25"), 20000.0, "CE") is None
    # strike listed hi nahi
    assert provider.premium(pd.Timestamp("2026-09-07 09:20"), 21000.0, "CE") is None

    coverage = provider.coverage()
    assert coverage["misses"]["no_bar"] == 1
    assert coverage["misses"]["no_contract"] == 1
    assert coverage["hit_rate_pct"] == 0.0


# ----------------------- simulator integration -----------------------

def make_ohlcv(closes, start="2026-09-07 09:20"):
    index = pd.date_range(start, periods=len(closes), freq="5min")
    df = pd.DataFrame(index=index)
    df["close"] = closes
    df["open"] = df["close"]
    df["high"] = df["close"] + 5
    df["low"] = df["close"] - 5
    df["volume"] = 1000
    return df


def trade_entry(date, decision="BUY", index=0):
    return {
        "date_index": index, "date": date, "decision": decision,
        "score": 70, "actual_direction": decision, "correct": True,
    }


class StubProvider:
    def __init__(self, prices: dict):
        self.prices = prices

    def premium(self, timestamp, strike, option_type):
        return self.prices.get(pd.Timestamp(timestamp))


def test_real_prices_replace_black_scholes_entirely():
    df = make_ohlcv([20000.0, 20050.0])
    provider = StubProvider({df.index[0]: 100.0, df.index[1]: 140.0})

    result = options_sim.simulate_trade_log(
        df, [trade_entry(df.index[0])], price_provider=provider,
        iv_crush_pct=50.0,  # real pricing pe iska koi asar nahi hona chahiye
    )
    trade = result["trades"][0]

    assert trade["pricing"] == "real"
    assert (trade["premium_in"], trade["premium_out"]) == (100.0, 140.0)
    assert trade["gross_pnl"] == pytest.approx(40.0 * options_sim.DEFAULT_LOT_SIZE)
    assert result["pricing"] == {"real": 1, "model": 0}
    assert not any("crush maana gaya" in w for w in result["warnings"])


def test_one_missing_leg_makes_the_whole_trade_model_priced():
    """Aadha market ka bhaav + aadha model = sabse bhramak number."""
    df = make_ohlcv([20000.0, 20050.0])
    provider = StubProvider({df.index[0]: 100.0})  # exit ka bhaav nahi

    result = options_sim.simulate_trade_log(
        df, [trade_entry(df.index[0])], price_provider=provider
    )
    trade = result["trades"][0]

    assert trade["pricing"] == "model"
    assert trade["premium_in"] != 100.0
    assert result["pricing"] == {"real": 0, "model": 1}


def test_mixed_run_is_flagged_loudly():
    df = make_ohlcv([20000.0, 20050.0, 20100.0])
    provider = StubProvider({df.index[0]: 100.0, df.index[1]: 140.0})
    log = [trade_entry(df.index[0], index=0), trade_entry(df.index[1], index=1)]

    result = options_sim.simulate_trade_log(df, log, price_provider=provider)

    assert result["pricing"] == {"real": 1, "model": 1}
    assert any("MIXED" in w for w in result["warnings"])


def test_provider_failure_falls_back_instead_of_crashing_the_run():
    class Broken:
        def premium(self, timestamp, strike, option_type):
            raise RuntimeError("angel down")

    df = make_ohlcv([20000.0, 20050.0])
    result = options_sim.simulate_trade_log(
        df, [trade_entry(df.index[0])], price_provider=Broken()
    )

    assert result["trades"][0]["pricing"] == "model"


def test_registry_start_se_pehle_ka_din_real_nahi_maana_jaata(tmp_path):
    """
    Registry banne se pehle ki weekly kabhi dekhi hi nahi gayi. Agli
    registered expiry 7-8 din door ho to gap-check use pass kar deta hai —
    par wo ek ALAG contract hai, uska bhaav "real" batana jhooth hoga.
    """
    registry = option_chain.refresh_registry(
        cache_dir=str(tmp_path), master=SAMPLE_MASTER
    )
    coverage_start = date(2026, 9, 3)  # 01SEP wali weekly kabhi dekhi hi nahi
    provider = option_chain.AngelOptionChain(
        cache_dir=str(tmp_path), registry=registry, offline=True,
        coverage_start=coverage_start,
    )

    for day in ("2026-09-01 09:20", "2026-09-02 09:20"):
        assert provider.contract_for(pd.Timestamp(day), 20000.0, "CE") is None
    assert provider.contract_for(
        pd.Timestamp("2026-09-03 09:20"), 20000.0, "CE"
    )["token"] == "111"


def test_coverage_start_registry_ke_pehle_refresh_se_aata_hai():
    registry = {
        "A": {"expiry": "2026-09-08", "first_seen": "2026-09-04"},
        "B": {"expiry": "2026-09-15", "first_seen": "2026-08-28"},
    }
    assert option_chain.registry_coverage_start(registry) == date(2026, 8, 28)
    assert option_chain.registry_coverage_start({}) is None


def test_expired_contract_ka_fetch_window_expiry_pe_rukta_hai(monkeypatch, tmp_path):
    """
    Post-expiry tail har roz badhta hai; agar window aaj tak khinche to
    har run wahi khaali hissa dobara download karta hai.
    """
    requested = {}

    def fake_load_intraday(**kwargs):
        requested.update(kwargs)
        return pd.DataFrame()

    registry = option_chain.refresh_registry(
        cache_dir=str(tmp_path), master=SAMPLE_MASTER
    )
    provider = option_chain.AngelOptionChain(
        cache_dir=str(tmp_path), registry=registry, offline=True
    )
    monkeypatch.setattr(option_chain, "load_intraday", fake_load_intraday)
    provider._load_contract_candles(registry["NIFTY08SEP2620000CE"])

    assert requested["end"].date() == date(2026, 9, 8)
    assert requested["days"] == option_chain.MAX_CONTRACT_HISTORY_DAYS
