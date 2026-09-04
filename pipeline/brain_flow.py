"""
Tiger Brain V6.1 — BRAIN FLOW ORCHESTRATOR
===========================================
Saare 5 Brains ko ek sequence mein wire karta hai:

    Brain 1 (brain1_scanner)        — momentum + regime gate
        ↓ passed
    Brain 2 (smart_money_scanner)   — SMC setup (OB / sweep / RS)
        ↓ setup found
    Brain 3 (broker.option_selector)— option contract select
        ↓ selected
    Brain 4 (risk.risk_management + broker.position_sizer)
                                    — trade counter guard + live capital sizing
        ↓ allowed & sized
    [EXECUTION — pipeline.stage5_execution / broker]
        ↓ position open
    Brain 5 (risk.exit_brain)       — stop/target/trail/time/gamma exits

Ye orchestrator pure-function style mein hai — har brain ka output dict
aage jaata hai, koi hidden state nahi (trade counter ke siwa, jo
process-wide singleton hai).
"""

from __future__ import annotations

import logging

try:
    from config.thresholds import BRAIN4
    from pipeline.brain1_scanner import scan as brain1_scan
    from pipeline.smart_money_scanner import generate_setup as brain2_setup
    from broker.option_selector import select_option as brain3_select
    from broker.position_sizer import get_available_capital, size_position
    from risk.risk_management import get_trade_counter
except ImportError:
    raise ImportError("Repo ROOT se chalao, 'pipeline/' ke andar se nahi.")

logger = logging.getLogger("tiger_brain.brain_flow")


def run_brain_flow(
    symbol: str,
    df,
    chain_snapshot: dict,
    benchmark_df=None,
    vix_series=None,
    exchange: str | None = None,
    broker=None,
    trade_counter=None,
) -> dict:
    """
    Poora 5-Brain flow ek symbol ke liye chalata hai.

    Args:
        symbol: trading symbol (jaise 'NIFTY', 'CRUDEOIL')
        df: symbol ka OHLCV DataFrame
        chain_snapshot: Brain 3 ka input (option chain data)
        benchmark_df: benchmark OHLCV (RS divergence ke liye)
        vix_series: VIX history (regime ke liye)
        exchange: symbol ka exchange ('MCX', 'NSE', ...) — category detect
        broker: AngelBroker instance — live capital ke liye (None = fallback)
        trade_counter: TradeCounterGuard instance — default process singleton

    Returns:
        dict: har brain ka result + final 'trade' dict ya None
    """
    flow_notes = []

    # ============ BRAIN 1: Scanner & Regime ============
    b1 = brain1_scan(df, benchmark_df=benchmark_df, vix_series=vix_series)
    if not b1["passed_brain1"]:
        flow_notes.append("Brain 1 FAIL — flow yahin ruk gaya")
        return {"brain1": b1, "brain2": None, "brain3": None, "brain4": None,
                "trade": None, "flow_notes": flow_notes}

    # ============ BRAIN 2: SMC Setup ============
    b2 = brain2_setup(b1, df)
    if not b2["setup_found"]:
        flow_notes.append("Brain 2 FAIL — koi SMC setup nahi mila")
        return {"brain1": b1, "brain2": b2, "brain3": None, "brain4": None,
                "trade": None, "flow_notes": flow_notes}

    # ============ BRAIN 3: Option Selection ============
    b3 = brain3_select(chain_snapshot, b2["direction"])
    if not b3["selected"]:
        flow_notes.append("Brain 3 FAIL — koi valid option contract nahi mila")
        return {"brain1": b1, "brain2": b2, "brain3": b3, "brain4": None,
                "trade": None, "flow_notes": flow_notes}

    # ============ BRAIN 4: Trade Counter + Capital Allocation ============
    counter = trade_counter or get_trade_counter()
    counter_check = counter.can_trade(symbol, exchange=exchange)
    if not counter_check["allowed"]:
        flow_notes.append(f"Brain 4 BLOCK: {counter_check['message']}")
        return {"brain1": b1, "brain2": b2, "brain3": b3,
                "brain4": {"counter": counter_check, "sizing": None},
                "trade": None, "flow_notes": flow_notes}

    capital_info = get_available_capital(broker)
    if capital_info["available_capital"] is None:
        flow_notes.append(
            f"Brain 4 BLOCK: live capital nahi mila ({capital_info['note']}) — "
            f"fail-safe: trade nahi"
        )
        return {"brain1": b1, "brain2": b2, "brain3": b3,
                "brain4": {"counter": counter_check, "sizing": None,
                           "capital": capital_info},
                "trade": None, "flow_notes": flow_notes}

    contract = b3["contract"]
    sizing = size_position(
        available_capital=capital_info["available_capital"],
        premium=contract["ltp"],
        lot_size=contract.get("lot_size"),
        current_exposure=0.0,
    )
    if sizing["quantity"] <= 0:
        flow_notes.append("Brain 4 BLOCK: position size 0 — capital/premium issue")
        return {"brain1": b1, "brain2": b2, "brain3": b3,
                "brain4": {"counter": counter_check, "sizing": sizing,
                           "capital": capital_info},
                "trade": None, "flow_notes": flow_notes}

    counter.register_trade(symbol, exchange=exchange)

    # ============ TRADE PACKET (execution-ready) ============
    trade = {
        "symbol": symbol,
        "exchange": exchange,
        "direction": b2["direction"],
        "option_type": "CE" if b2["direction"] == "BUY" else "PE",
        "option_symbol": contract.get("trading_symbol")
        or f"{symbol}{contract['strike']}{contract['option_type']}",
        "strike": contract["strike"],
        "premium": contract["ltp"],
        "quantity": sizing["quantity"],
        "lots": sizing["lots"],
        "allocated_capital": sizing["allocated_capital"],
        "underlying_stop": b2["stop_loss"],
        "entry_price_underlying": b2["entry_price"],
        "delta": contract.get("estimated_delta"),
        "days_to_expiry": chain_snapshot.get("expiry_days"),
        "brain_scores": {
            "brain2_setup_score": b2["setup_score"],
            "rs_score": b2["rs_score"],
        },
    }

    flow_notes.append(
        f"TRADE: {trade['direction']} {trade['option_symbol']} x{trade['quantity']} "
        f"@ {trade['premium']} — capital {trade['allocated_capital']} "
        f"(Brain 5 ab exit sambhalega)"
    )

    return {
        "brain1": b1, "brain2": b2, "brain3": b3,
        "brain4": {"counter": counter_check, "sizing": sizing,
                   "capital": capital_info},
        "trade": trade,
        "flow_notes": flow_notes,
    }


# ============================================================
# QUICK MANUAL TEST — repo ROOT se: python3 -m pipeline.brain_flow
# ============================================================
if __name__ == "__main__":
    import numpy as np
    import pandas as pd

    from risk.risk_management import TradeCounterGuard

    np.random.seed(3)
    n = 80
    dates = pd.date_range("2025-01-01", periods=n, freq="D")

    # Gentle momentum uptrend (tests wala pattern) — NIFTY scale tak scale
    # kiya hua, taaki sweep-reclaim ke baad bhi 10-bar return positive rahe.
    base = 100 + np.linspace(0, 0.2 * n, n) + np.random.normal(0, 0.15, n).cumsum() * 0.1
    scale = 200.0  # 100-ish -> 20000-ish (NIFTY)
    d = pd.DataFrame(index=dates)
    d["close"] = base * scale
    d["open"] = d["close"].shift(1).fillna(d["close"].iloc[0])
    d["high"] = d[["open", "close"]].max(axis=1) + 0.3 * scale
    d["low"] = d[["open", "close"]].min(axis=1) - 0.3 * scale
    vols = np.full(n, 300000)
    vols[-1] = 900000
    d["volume"] = vols

    # Strong directional last candle
    d.iloc[-1, d.columns.get_loc("open")] = d.iloc[-1]["close"] - 1.5 * scale
    d.iloc[-1, d.columns.get_loc("high")] = d.iloc[-1]["close"] + 0.2 * scale
    d.iloc[-1, d.columns.get_loc("low")] = d.iloc[-1]["open"] - 0.2 * scale

    # Bullish sweep — detector ke SAME swing window (last 20 bars, final
    # bar exclusive) se inject karo, deep pierce + strong reclaim body.
    prior_low = float(d["low"].iloc[-21:-1].min())
    d.iloc[-1, d.columns.get_loc("low")] = prior_low - 1.5 * scale
    d.iloc[-1, d.columns.get_loc("open")] = prior_low - 0.3 * scale
    d.iloc[-1, d.columns.get_loc("close")] = prior_low + 2.5 * scale
    d.iloc[-1, d.columns.get_loc("high")] = prior_low + 2.7 * scale

    # Weak/declining benchmark — clear positive RS divergence
    bench = d.copy()
    bench["close"] = (100 + np.linspace(0, -0.2 * n, n)) * scale
    bench["open"] = bench["close"].shift(1).fillna(bench["close"].iloc[0])
    bench["high"] = bench[["open", "close"]].max(axis=1) + 0.3 * scale
    bench["low"] = bench[["open", "close"]].min(axis=1) - 0.3 * scale

    chain = {
        "underlying_price": float(d["close"].iloc[-1]),
        "expiry_days": 5,
        # Strikes underlying ke aas-paas — delta band valid rahe
        "contracts": [
            {"strike": 19600, "option_type": "CE", "ltp": 180, "bid": 179, "ask": 181,
             "open_interest": 8000, "oi_change_pct": 25, "iv": 14, "delta": 0.48,
             "lot_size": 75, "trading_symbol": "NIFTY25SEP19600CE"},
            {"strike": 19700, "option_type": "CE", "ltp": 120, "bid": 119.3, "ask": 120.7,
             "open_interest": 6000, "oi_change_pct": 10, "iv": 14, "delta": 0.42,
             "lot_size": 75, "trading_symbol": "NIFTY25SEP19700CE"},
            {"strike": 19500, "option_type": "CE", "ltp": 240, "bid": 238.7, "ask": 241.3,
             "open_interest": 9000, "oi_change_pct": 12, "iv": 14, "delta": 0.58,
             "lot_size": 75, "trading_symbol": "NIFTY25SEP19500CE"},
            {"strike": 19600, "option_type": "PE", "ltp": 110, "bid": 109.3, "ask": 110.7,
             "open_interest": 5000, "oi_change_pct": 8, "iv": 14, "delta": -0.44,
             "lot_size": 75, "trading_symbol": "NIFTY25SEP19600PE"},
        ],
    }

    guard = TradeCounterGuard(global_limit=5, commodity_limit=5)

    class FakeBroker:
        class smart_api:
            @staticmethod
            def getRMS():
                return {"data": {"availablecash": "150000"}}

    result = run_brain_flow(
        symbol="NIFTY", df=d, chain_snapshot=chain,
        benchmark_df=bench, exchange="NSE",
        broker=FakeBroker(), trade_counter=guard,
    )

    print("=== 5-Brain Flow Test ===")
    print(f"Brain 1 passed: {result['brain1']['passed_brain1']}")
    if result.get("brain2"):
        print(f"Brain 2 setup: {result['brain2']['setup_found']} "
              f"({result['brain2']['direction']}, score {result['brain2']['setup_score']})")
    if result.get("brain3"):
        print(f"Brain 3 strike: {result['brain3']['contract'] and result['brain3']['contract']['strike']}")
    print(f"Trade: {result['trade']}")
    print("Notes:")
    for note in result["flow_notes"]:
        print(f"  - {note}")
