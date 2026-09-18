"""
Tiger Brain V6.1 — 5-Brain architecture tests.
Covers: Brain 1 momentum/noise filter, Brain 2 SMC setups,
Brain 3 option selector, Brain 4 trade counter + position sizer,
Brain 5 exit rules, and the full brain-flow wiring.
"""

import numpy as np
import pandas as pd
import pytest
from datetime import datetime, date

from config.thresholds import BRAIN1, BRAIN2, BRAIN3, BRAIN4, BRAIN5
from pipeline.brain1_scanner import scan, volume_velocity, rs_divergence, is_choppy
from pipeline.smart_money_scanner import (
    generate_setup, find_order_blocks, find_liquidity_sweep, compute_rs_score,
)
from broker.option_selector import select_option, estimate_delta
from risk.risk_management import TradeCounterGuard, resolve_market_category
from broker.position_sizer import get_available_capital, size_position
from risk.exit_brain import evaluate_exit, check_gamma_risk
from pipeline.brain_flow import run_brain_flow


# ============================================================
# Fixtures — synthetic OHLCV builders
# ============================================================

N = 80


def make_df(closes, volumes, wick=0.3, dates=None):
    dates = dates or pd.date_range("2025-01-01", periods=len(closes), freq="D")
    d = pd.DataFrame(index=dates)
    d["close"] = closes
    d["open"] = pd.Series(closes).shift(1).fillna(closes[0]).values
    d["high"] = d[["open", "close"]].max(axis=1) + wick
    d["low"] = d[["open", "close"]].min(axis=1) - wick
    d["volume"] = volumes
    return d


def trending_df(slope=0.2, vol_spike=True, seed=42):
    np.random.seed(seed)
    closes = 100 + np.linspace(0, slope * N, N) + np.random.normal(0, 0.15, N).cumsum() * 0.1
    vols = np.full(N, 200000.0)
    if vol_spike:
        vols[-1] = 700000.0
    df = make_df(closes, vols)
    # Last candle ko strong directional body dena (doji nahi) — Brain 1 ka
    # body/range gate warna synthetic trend data pe bhi fail ho jata hai.
    last = len(df) - 1
    df.iloc[last, df.columns.get_loc("open")] = df.iloc[last]["close"] - 1.5
    df.iloc[last, df.columns.get_loc("high")] = df.iloc[last]["close"] + 0.2
    df.iloc[last, df.columns.get_loc("low")] = df.iloc[last]["open"] - 0.2
    return df


def flat_df(seed=42):
    np.random.seed(seed)
    closes = 100 + np.random.normal(0, 0.05, N).cumsum() * 0.02
    df = make_df(closes, np.full(N, 200000.0), wick=0.02)
    last = len(df) - 1
    df.iloc[last, df.columns.get_loc("open")] = df.iloc[last]["close"] - 0.5
    return df


def inject_bullish_sweep(df, depth_atr=1.5):
    """Last bar mein deep bullish liquidity sweep inject karta hai —
    Brain 2 ke same swing window (last 20 bars, final bar exclusive) se.
    Close ko deep-reclaim dete hain (strong body) taaki candle doji na ho."""
    from pipeline.smart_money_scanner import _atr
    atr = _atr(df)
    prior_low = float(df["low"].iloc[-21:-1].min())
    last = len(df) - 1
    df.iloc[last, df.columns.get_loc("low")] = prior_low - depth_atr * atr
    df.iloc[last, df.columns.get_loc("open")] = prior_low - 0.3
    df.iloc[last, df.columns.get_loc("close")] = prior_low + 2.5
    df.iloc[last, df.columns.get_loc("high")] = prior_low + 2.7
    return df


def inject_bearish_sweep(df, depth_atr=1.5):
    from pipeline.smart_money_scanner import _atr
    atr = _atr(df)
    prior_high = float(df["high"].iloc[-21:-1].max())
    last = len(df) - 1
    df.iloc[last, df.columns.get_loc("high")] = prior_high + depth_atr * atr
    df.iloc[last, df.columns.get_loc("close")] = prior_high - 0.5
    df.iloc[last, df.columns.get_loc("open")] = prior_high + 0.3
    df.iloc[last, df.columns.get_loc("low")] = prior_high - 0.8
    return df


# ============================================================
# BRAIN 1 — momentum & noise filter
# ============================================================

class TestBrain1:
    def test_momentum_day_passes(self):
        df = trending_df()
        bench = trending_df(slope=0.02, vol_spike=False, seed=1)
        r = scan(df, benchmark_df=bench)
        assert r["passed_brain1"] is True
        assert r["momentum"]["high_velocity"] is True

    def test_chop_day_fails_volume_gate(self):
        df = flat_df()
        bench = flat_df(seed=7)  # benchmark bhi flat — koi RS divergence nahi
        r = scan(df, benchmark_df=bench)
        # flat closes: no volume spike, no RS divergence -> fail
        assert r["passed_brain1"] is False
        assert any("MOMENTUM GATE FAIL" in n for n in r["brain1_notes"])

    def test_choppy_candle_rejected(self):
        # volume spike but pure doji last candle
        df = trending_df()
        df.iloc[-1, df.columns.get_loc("open")] = df.iloc[-1]["close"]
        df.iloc[-1, df.columns.get_loc("high")] = df.iloc[-1]["close"] + 2.0
        df.iloc[-1, df.columns.get_loc("low")] = df.iloc[-1]["close"] - 2.0
        bench = trending_df(slope=0.02, vol_spike=False, seed=1)
        r = scan(df, benchmark_df=bench)
        assert r["passed_brain1"] is False
        assert any("CHOPPY CANDLE" in n for n in r["brain1_notes"])

    def test_rs_divergence_alone_can_pass(self):
        # no volume spike, but strong outperformance
        df = trending_df(slope=0.4, vol_spike=False)
        bench = trending_df(slope=-0.2, vol_spike=False, seed=1)
        r = scan(df, benchmark_df=bench)
        assert r["rs_divergence"]["significant"] is True
        assert r["passed_brain1"] is True

    def test_zero_volume_index_handled(self):
        df = trending_df()
        df["volume"] = 0.0
        bench = trending_df(slope=0.02, vol_spike=False, seed=1)
        r = scan(df, benchmark_df=bench)
        assert r["momentum"]["high_velocity"] is False
        assert r["momentum"]["avg_volume"] is None

    def test_volume_velocity_ratio(self):
        df = trending_df()
        vv = volume_velocity(df)
        assert vv["ratio"] == pytest.approx(700000 / 200000, rel=0.01)
        assert vv["high_velocity"] is True

    def test_rs_divergence_math(self):
        df = trending_df()
        bench = trending_df(slope=0.05, vol_spike=False, seed=1)
        rs = rs_divergence(df, bench)
        assert rs["divergence_pct"] > 0
        assert rs["divergence_pct"] == pytest.approx(
            rs["symbol_return_pct"] - rs["benchmark_return_pct"], abs=0.01
        )

    def test_insufficient_data(self):
        r = scan(trending_df().head(10))
        assert r["passed_brain1"] is False

    def test_is_choppy_detects_dead_market(self):
        df = flat_df()
        c = is_choppy(df)
        assert c["choppy"] is True


# ============================================================
# BRAIN 2 — SMC setups
# ============================================================

class TestBrain2:
    def _b1(self, rs_pct=2.5, passed=True):
        return {"passed_brain1": passed,
                "rs_divergence": {"divergence_pct": rs_pct}}

    def test_bullish_sweep_setup(self):
        df = inject_bullish_sweep(trending_df())
        r = generate_setup(self._b1(), df)
        assert r["setup_found"] is True
        assert r["direction"] == "BUY"
        assert r["liquidity_sweep"]["sweep"]["direction"] == "bullish"
        assert r["stop_loss"] < r["entry_price"]

    def test_bearish_sweep_setup(self):
        df = inject_bearish_sweep(trending_df(slope=-0.2))
        r = generate_setup(self._b1(rs_pct=-2.5), df)
        assert r["setup_found"] is True
        assert r["direction"] == "SELL"
        assert r["stop_loss"] > r["entry_price"]

    def test_no_structure_no_setup(self):
        df = flat_df()
        r = generate_setup(self._b1(), df)
        # flat data may have conflicting/no signals
        assert r["setup_found"] is False or r["setup_score"] < BRAIN2["MIN_SETUP_SCORE"]

    def test_brain1_gate_blocks(self):
        df = trending_df()
        r = generate_setup({"passed_brain1": False}, df)
        assert r["setup_found"] is False

    def test_order_block_detection(self):
        df = trending_df()
        last = len(df) - 1
        # SMC definition: displacement se theek PEHLE ek opposite (red) candle
        # chahiye — wahi order block hai. Pehle red candle banao...
        df.iloc[last - 1, df.columns.get_loc("close")] = df.iloc[last - 1]["open"] - 1.0
        df.iloc[last - 1, df.columns.get_loc("low")] = df.iloc[last - 1]["open"] - 1.2
        df.iloc[last - 1, df.columns.get_loc("high")] = df.iloc[last - 1]["open"] + 0.2
        # ... phir last bar displacement-up jo swing high todta hai.
        df.iloc[last, df.columns.get_loc("close")] = df["high"].iloc[-25:-1].max() + 2.0
        df.iloc[last, df.columns.get_loc("open")] = df.iloc[last - 1]["close"] + 0.2
        df.iloc[last, df.columns.get_loc("high")] = df.iloc[last]["close"] + 0.2
        obs = find_order_blocks(df)
        assert obs["bullish"] is not None
        assert obs["bullish"]["top"] >= obs["bullish"]["bottom"]

    def test_rs_score_mapping(self):
        assert compute_rs_score(None) == 50.0
        assert compute_rs_score(5.0) == 100.0
        assert compute_rs_score(-5.0) == 0.0
        assert compute_rs_score(1.0) == pytest.approx(60.0)

    def test_setup_score_bounds(self):
        df = trending_df()
        swing_low = float(df["low"].iloc[-15:-1].min())
        df.iloc[-1, df.columns.get_loc("low")] = swing_low - 1.5
        df.iloc[-1, df.columns.get_loc("close")] = swing_low + 0.5
        r = generate_setup(self._b1(), df)
        assert 0 <= r["setup_score"] <= 100


# ============================================================
# BRAIN 3 — option selector
# ============================================================

def make_chain(underlying=25000.0, expiry_days=5):
    # Tight spreads (~1.1% of premium) — liquid index-option jaisa.
    return {
        "underlying_price": underlying,
        "expiry_days": expiry_days,
        "contracts": [
            {"strike": 25000, "option_type": "CE", "ltp": 180, "bid": 179, "ask": 181,
             "open_interest": 5000, "oi_change_pct": 8, "iv": 14, "delta": 0.52, "lot_size": 75},
            {"strike": 25100, "option_type": "CE", "ltp": 120, "bid": 119.3, "ask": 120.7,
             "open_interest": 8000, "oi_change_pct": 25, "iv": 14, "delta": 0.45, "lot_size": 75},
            {"strike": 24900, "option_type": "CE", "ltp": 240, "bid": 238.7, "ask": 241.3,
             "open_interest": 4000, "oi_change_pct": 5, "iv": 14, "delta": 0.60, "lot_size": 75},
            {"strike": 25000, "option_type": "PE", "ltp": 175, "bid": 174, "ask": 176,
             "open_interest": 5200, "oi_change_pct": 6, "iv": 14, "delta": -0.50, "lot_size": 75},
            {"strike": 24900, "option_type": "PE", "ltp": 230, "bid": 228.7, "ask": 231.3,
             "open_interest": 6000, "oi_change_pct": 18, "iv": 14, "delta": -0.58, "lot_size": 75},
        ],
    }


class TestBrain3:
    def test_buy_selects_ce(self):
        r = select_option(make_chain(), "BUY")
        assert r["selected"] is True
        assert r["contract"]["option_type"] == "CE"

    def test_sell_selects_pe(self):
        r = select_option(make_chain(), "SELL")
        assert r["selected"] is True
        assert r["contract"]["option_type"] == "PE"

    def test_oi_velocity_hot_strike_preferred(self):
        r = select_option(make_chain(), "BUY")
        # 25100 has OI velocity 25% (hot) and better delta-band center
        assert r["contract"]["strike"] == 25100
        assert "hot" in r["oi_velocity_note"]

    def test_expiry_too_close_rejected(self):
        r = select_option(make_chain(expiry_days=0), "BUY")
        assert r["selected"] is False
        assert any("gamma/theta" in x for x in r["reasons"])

    def test_expiry_too_far_rejected(self):
        r = select_option(make_chain(expiry_days=60), "BUY")
        assert r["selected"] is False
        assert any("far-month" in x for x in r["reasons"])

    def test_illiquid_oi_rejected(self):
        chain = make_chain()
        for c in chain["contracts"]:
            c["open_interest"] = 10
        r = select_option(chain, "BUY")
        assert r["selected"] is False
        assert any("OI" in x for x in r["reasons"])

    def test_wide_spread_rejected(self):
        chain = make_chain()
        for c in chain["contracts"]:
            c["bid"] = c["ltp"] - 20
            c["ask"] = c["ltp"] + 20  # huge spread
        r = select_option(chain, "BUY")
        assert r["selected"] is False
        assert any("spread" in x for x in r["reasons"])

    def test_delta_out_of_band_rejected(self):
        chain = make_chain()
        for c in chain["contracts"]:
            if c["option_type"] == "CE":
                c["delta"] = 0.95  # deep ITM — out of band
        r = select_option(chain, "BUY")
        assert r["selected"] is False
        assert any("delta" in x for x in r["reasons"])

    def test_invalid_direction(self):
        r = select_option(make_chain(), "HOLD")
        assert r["selected"] is False

    def test_estimate_delta_fallback(self):
        # no delta provided — estimate from moneyness
        ce_atm = {"strike": 25000}
        d = estimate_delta(ce_atm, 25000.0, "CE")
        assert 0.45 <= d <= 0.55
        ce_itm = {"strike": 24500}
        d2 = estimate_delta(ce_itm, 25000.0, "CE")
        assert d2 > d
        pe_itm = {"strike": 25500}
        d3 = estimate_delta(pe_itm, 25000.0, "PE")
        assert d3 > 0.55

    def test_empty_chain(self):
        r = select_option({"underlying_price": 100, "contracts": []}, "BUY")
        assert r["selected"] is False


# ============================================================
# BRAIN 4 — trade counter guard
# ============================================================

class TestTradeCounter:
    def test_global_limit_blocks_all(self):
        g = TradeCounterGuard(global_limit=20, commodity_limit=10)
        for _ in range(20):
            assert g.register_trade("RELIANCE", exchange="NSE")["registered"] is True
        r = g.can_trade("RELIANCE", exchange="NSE")
        assert r["allowed"] is False
        assert "GLOBAL limit hit" in r["blocked_by"][0]

    def test_commodity_limit_blocks_only_commodity(self):
        g = TradeCounterGuard(global_limit=20, commodity_limit=10)
        for _ in range(10):
            g.register_trade("GOLD", exchange="MCX")
        blocked = g.can_trade("CRUDEOIL", exchange="MCX")
        assert blocked["allowed"] is False
        assert "COMMODITY limit hit" in blocked["blocked_by"][0]
        # equity still allowed
        equity = g.can_trade("NIFTY", exchange="NSE")
        assert equity["allowed"] is True

    def test_register_blocked_trade_refuses(self):
        g = TradeCounterGuard(global_limit=20, commodity_limit=10)
        for _ in range(10):
            g.register_trade("GOLD", exchange="MCX")
        r = g.register_trade("GOLD", exchange="MCX")
        assert r["registered"] is False
        assert g.commodity_count == 10  # no double-count

    def test_limits_clamped_to_10_20(self):
        g_low = TradeCounterGuard(global_limit=2, commodity_limit=1)
        assert g_low.global_limit == 10
        assert g_low.commodity_limit == 10
        g_high = TradeCounterGuard(global_limit=99, commodity_limit=99)
        assert g_high.global_limit == 20
        assert g_high.commodity_limit == 20

    def test_date_roll_resets(self, monkeypatch):
        import risk.risk_management as rm

        g = TradeCounterGuard(global_limit=5, commodity_limit=5,
                              today=date(2020, 1, 1))
        g.register_trade("GOLD", exchange="MCX")
        assert g.global_count == 1

        # date immutable hai — module-level reference monkeypatch karo
        class FakeDate:
            @staticmethod
            def today():
                return date(2020, 1, 2)

        monkeypatch.setattr(rm, "date", FakeDate)
        r = g.can_trade("GOLD", exchange="MCX")
        assert r["allowed"] is True
        assert r["counts"]["global_count"] == 0
        assert r["counts"]["commodity_count"] == 0

    def test_category_resolution(self):
        assert resolve_market_category("GOLD", "MCX") == "commodity"
        assert resolve_market_category("RELIANCE", "NSE") == "equity"
        assert resolve_market_category("CRUDEOIL25SEPFUT") == "commodity"
        assert resolve_market_category("NIFTY") == "equity"
        assert resolve_market_category("SILVERMIC") == "commodity"
        assert resolve_market_category("MENTHAOIL") == "commodity"


# ============================================================
# BRAIN 4 — dynamic position sizer
# ============================================================

class FakeSmartApi:
    def __init__(self, cash):
        self._cash = cash

    def getRMS(self):
        return {"data": {"availablecash": str(self._cash)}}


class FakeBroker:
    def __init__(self, cash):
        self.smart_api = FakeSmartApi(cash)
        self._cash = cash

    def get_balance(self):
        return self._cash


class TestPositionSizer:
    def test_live_broker_capital(self):
        info = get_available_capital(FakeBroker(123456.78))
        assert info["available_capital"] == pytest.approx(123456.78)
        assert info["source"] == "broker"

    def test_broker_fail_no_fallback_blocks(self):
        class BadBroker:
            smart_api = None
            def get_balance(self):
                return 0.0

        info = get_available_capital(BadBroker())
        assert info["available_capital"] is None
        assert "block" in info["note"]

    def test_none_broker_no_fallback_blocks(self):
        info = get_available_capital(None)
        assert info["available_capital"] is None

    def test_sizing_within_confidence_cap(self):
        # 100k capital, premium 200, lot 75, ROCKET score 92
        # cap 100k * 100% = 100k -> 100k/(200*75) = 6 lots = 450 qty
        r = size_position(100000, 200, lot_size=75, score=92)
        assert r["quantity"] == 450
        assert r["lots"] == 6
        assert r["confidence_tier"] == "ROCKET"
        # premium 40, strong score 82: 100k*80%=80k -> 80k/(40*75) = 26 lots
        r2 = size_position(100000, 40, lot_size=75, score=82)
        assert r2["lots"] == 26
        assert r2["quantity"] == 1950
        assert r2["allocated_capital"] == pytest.approx(26 * 40 * 75)
        assert r2["confidence_tier"] == "strong"

    def test_low_score_uses_decent_tier(self):
        # low score still sizes (entry is brain2's job), uses decent tier
        r = size_position(100000, 200, lot_size=75, score=60)
        assert r["quantity"] > 0
        assert r["confidence_tier"] == "decent"

    def test_exposure_cap_blocks(self):
        # exposure now 100% — 50k current + 100k cap = headroom 50k
        # score 92 (100%): allocatable 50k, premium 40 lot 75 = 3000/lot
        # 50k/3000 = 16 lots
        r = size_position(100000, 40, lot_size=75, current_exposure=50000, score=92)
        assert r["quantity"] > 0
        # full exposure used up
        r_full = size_position(100000, 40, lot_size=75, current_exposure=100000, score=92)
        assert r_full["quantity"] == 0
        assert any("exposure cap" in n for n in r_full["notes"])

    def test_invalid_inputs(self):
        assert size_position(None, 100, score=92)["quantity"] == 0
        assert size_position(100000, 0, score=92)["quantity"] == 0
        assert size_position(100000, -5, score=92)["quantity"] == 0
        assert size_position(-100, 100, score=92)["quantity"] == 0

    def test_no_lot_size_defaults_to_1(self):
        # 100k, score 92 (100%), premium 100, lot None -> 1 lot = 100 per unit
        # 100k/100 = 1000 lots
        r = size_position(100000, 100, lot_size=None, score=92)
        assert r["quantity"] == 1000
        assert r["lots"] == 1000


# ============================================================
# BRAIN 5 — exit brain
# ============================================================

def make_position(**over):
    pos = {
        "symbol": "NIFTY", "option_symbol": "NIFTY25SEP25000CE",
        "direction": "BUY", "entry_premium": 100.0, "quantity": 75,
        "entry_time": datetime(2025, 9, 1, 10, 0), "peak_premium": 100.0,
        "underlying_stop": 24800.0, "delta": 0.55, "gamma_pct": 1.0,
        "days_to_expiry": 5,
    }
    pos.update(over)
    return pos


MIDDAY = datetime(2025, 9, 1, 13, 0)
NEAR_CLOSE = datetime(2025, 9, 1, 15, 20)


class TestBrain5:
    def test_hold_when_nothing_triggered(self):
        r = evaluate_exit(make_position(), 105.0, current_time=MIDDAY,
                          underlying_price=25000)
        assert r["exit"] is False
        assert r["reason"] == "hold"

    def test_stop_loss(self):
        r = evaluate_exit(make_position(), 70.0, current_time=MIDDAY,
                          underlying_price=25000)
        assert r["exit"] is True
        assert r["reason"] == "stop_loss"

    def test_target(self):
        r = evaluate_exit(make_position(), 160.0, current_time=MIDDAY,
                          underlying_price=25000)
        assert r["exit"] is True
        assert r["reason"] == "target"

    def test_time_exit_near_close(self):
        r = evaluate_exit(make_position(), 105.0, current_time=NEAR_CLOSE,
                          underlying_price=25000)
        assert r["exit"] is True
        assert r["reason"] == "time_exit"

    def test_gamma_exit_near_expiry_high_gamma(self):
        pos = make_position(days_to_expiry=1, gamma_pct=3.0)
        r = evaluate_exit(pos, 105.0, current_time=MIDDAY, underlying_price=25000)
        assert r["exit"] is True
        assert r["reason"] == "gamma_exit"

    def test_gamma_watch_not_exit(self):
        pos = make_position(days_to_expiry=1, gamma_pct=1.0)
        r = evaluate_exit(pos, 105.0, current_time=MIDDAY, underlying_price=25000)
        assert r["exit"] is False

    def test_structural_stop_buy(self):
        r = evaluate_exit(make_position(), 105.0, current_time=MIDDAY,
                          underlying_price=24750)
        assert r["exit"] is True
        assert r["reason"] == "structural_stop"

    def test_structural_stop_sell(self):
        pos = make_position(direction="SELL", underlying_stop=25200.0)
        r = evaluate_exit(pos, 105.0, current_time=MIDDAY, underlying_price=25250)
        assert r["exit"] is True
        assert r["reason"] == "structural_stop"

    def test_trailing_stop(self):
        pos = make_position(peak_premium=160.0)
        # 132 from peak 160 = -17.5% give-back after +60% activation
        r = evaluate_exit(pos, 132.0, current_time=MIDDAY, underlying_price=25000)
        assert r["exit"] is True
        assert r["reason"] == "trailing_stop"

    def test_trailing_not_active_below_activation(self):
        pos = make_position(peak_premium=125.0)
        # 110 from peak 125 = -12% but gain only +10% (< activation 30%)
        r = evaluate_exit(pos, 110.0, current_time=MIDDAY, underlying_price=25000)
        assert r["exit"] is False

    def test_peak_updates(self):
        pos = make_position()
        evaluate_exit(pos, 130.0, current_time=MIDDAY, underlying_price=25000)
        assert pos["peak_premium"] == 130.0

    def test_time_exit_priority_over_others(self):
        # stop-loss level premium + near close — time exit wins
        r = evaluate_exit(make_position(), 70.0, current_time=NEAR_CLOSE,
                          underlying_price=25000)
        assert r["reason"] == "time_exit"


# ============================================================
# FULL FLOW — 5 brains wired together
# ============================================================

class TestBrainFlow:
    def _data(self):
        df = inject_bullish_sweep(trending_df(seed=3))
        # Weak/negative benchmark — symbol ka clear outperformance dikhana
        bench = trending_df(slope=-0.2, vol_spike=False, seed=1)
        return df, bench

    def test_full_flow_produces_trade(self):
        df, bench = self._data()
        result = run_brain_flow(
            symbol="NIFTY", df=df, chain_snapshot=make_chain(float(df["close"].iloc[-1])),
            benchmark_df=bench, exchange="NSE",
            broker=FakeBroker(150000), trade_counter=TradeCounterGuard(),
        )
        assert result["brain1"]["passed_brain1"] is True
        assert result["brain2"]["setup_found"] is True
        assert result["brain3"]["selected"] is True
        trade = result["trade"]
        assert trade is not None
        assert trade["direction"] == "BUY"
        assert trade["option_type"] == "CE"
        assert trade["quantity"] > 0
        assert trade["underlying_stop"] is not None
        assert result["brain4"]["counter"]["allowed"] is True

    def test_flow_blocked_by_trade_counter(self):
        df, bench = self._data()
        exhausted = TradeCounterGuard(global_limit=20, commodity_limit=10)
        for _ in range(20):
            exhausted.register_trade("NIFTY", exchange="NSE")
        result = run_brain_flow(
            symbol="NIFTY", df=df, chain_snapshot=make_chain(float(df["close"].iloc[-1])),
            benchmark_df=bench, exchange="NSE",
            broker=FakeBroker(150000), trade_counter=exhausted,
        )
        assert result["trade"] is None
        assert any("Brain 4 BLOCK" in n for n in result["flow_notes"])

    def test_flow_blocked_by_no_capital(self):
        df, bench = self._data()
        result = run_brain_flow(
            symbol="NIFTY", df=df, chain_snapshot=make_chain(float(df["close"].iloc[-1])),
            benchmark_df=bench, exchange="NSE",
            broker=None, trade_counter=TradeCounterGuard(),  # no fallback -> block
        )
        assert result["trade"] is None
        assert any("live capital" in n for n in result["flow_notes"])

    def test_flow_blocked_by_brain1(self):
        df = flat_df()
        result = run_brain_flow(
            symbol="XYZ", df=df, chain_snapshot=make_chain(),
            benchmark_df=trending_df(seed=7), exchange="NSE",
            broker=FakeBroker(150000), trade_counter=TradeCounterGuard(),
        )
        assert result["brain2"] is None
        assert result["trade"] is None
        assert any("Brain 1 FAIL" in n for n in result["flow_notes"])

    def test_flow_registers_trade_in_counter(self):
        df, bench = self._data()
        guard = TradeCounterGuard()
        run_brain_flow(
            symbol="NIFTY", df=df, chain_snapshot=make_chain(float(df["close"].iloc[-1])),
            benchmark_df=bench, exchange="NSE",
            broker=FakeBroker(150000), trade_counter=guard,
        )
        assert guard.global_count == 1

    def test_commodity_flow_counts_commodity(self):
        df, bench = self._data()
        guard = TradeCounterGuard(global_limit=10, commodity_limit=5)
        run_brain_flow(
            symbol="GOLD", df=df, chain_snapshot=make_chain(float(df["close"].iloc[-1])),
            benchmark_df=bench, exchange="MCX",
            broker=FakeBroker(150000), trade_counter=guard,
        )
        assert guard.commodity_count == 1
        assert guard.global_count == 1
