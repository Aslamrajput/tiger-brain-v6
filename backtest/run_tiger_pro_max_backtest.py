"""
TIGER PRO MAX — SUPER POWERFUL RACE CAR ENGINE (Variant D)
==========================================================
V6.6 ke SAARE 5 brains JAGTE HAIN (wake forever, koi soya nahi).
Full Black-Scholes mathematics + Greeks + PCR sentiment filter.

This engine is a SUPERSET of V6.6:
  - Brain 1 (Gate):         ✅ ACTIVE — brain1_intraday_pass
  - Brain 2 (Zone Scanner): ✅ ALL 6 functions awake:
       detect_zones_explosive, zone_touched_on_1m, delta_spike_confirms,
       liquidity_sweep, zone_explosive_quality, find_opposing_zone
  - Brain 3 (Sniper Entry): ✅ ACTIVE — find_sniper_entry (FULL V6.6 sniper)
  - Brain 4 (Trade Quota):  ✅ ACTIVE — TradeCounterGuard
  - Brain 5 (Exit Rider):   ✅ ACTIVE — check_intraday_exit

ADDITIONAL UPGRADES over V6.6 + Tiger PRO:
  + BLACK-SCHOLES pricing (proper option valuation, not rough estimate)
  + GREEKS: Delta, Gamma, Theta, Vega for every trade
  + PCR (Put-Call Ratio) sentiment filter — bullish/bearish confirmation
  + TREND LOCK: 15m trend alignment (Calls in uptrend, Puts in downtrend)
  + GOLDEN WINDOW: trade only high-probability time windows
  + DYNAMIC LOT-SIZING: 1.0% risk per trade, ₹2,000 hard cap
  + STRUCTURAL STOP: zone-breach based (wider, lets trade breathe)

Race Car + Tiger PRO = Super Powerful Race Car.
Agressive like Raw S/D (more trades) + Quality like Tiger PRO (high win rate).

V6.6 core files (intraday_backtest.py, intraday_strategies.py) are
100% UNTOUCHED — only imports, no modifications.

Run from repo ROOT:
    python3 -m backtest.run_tiger_pro_max_backtest
"""

from __future__ import annotations

import logging
import math
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import pandas as pd

sys.path.insert(0, ".")

from broker.angel_connect import AngelBroker
from data.loader import (
    fetch_angel_historical_candles,
    find_symbol_token,
    load_angel_instrument_master,
    get_option_chain_instruments,
)
from backtest.intraday_backtest import (
    _normalize_cols,
    _in_entry_window,
    _is_square_off_bar,
    brain1_intraday_pass,
    realized_vol_simple,
    spread_ok,
    check_intraday_exit,
    find_sniper_entry,
    print_report,
)
from pipeline.intraday_strategies import (
    detect_zones,
    detect_zones_explosive,
    zone_touched_on_1m,
    zone_explosive_quality,
    delta_spike_confirms,
    liquidity_sweep,
    find_opposing_zone,
    _simple_range,
    volume_delta,
)
from universe.fno_universe import (
    UNIVERSE, all_symbols, segment_of, lot_size, is_expiry_day,
)
from risk.risk_management import TradeCounterGuard

logger = logging.getLogger("tiger_brain.tiger_pro_max")
logging.basicConfig(level=logging.INFO)

# ============================================================
# CONSTANTS
# ============================================================
RISK_PCT_PER_TRADE = 1.0
RISK_FREE_RATE = 0.07          # India 10-year bond yield (~7%)
PREMIUM_MAX_PCT_OF_UNDERLYING = 2.0
PREMIUM_MIN = 3.0
TREND_LOOKBACK = 10
# Golden windows — widened for Race Car aggressiveness
GOLDEN_WINDOWS_NSE = [
    ("09:15", "11:30"),   # Morning institutional flow (widened)
    ("13:00", "15:15"),   # Afternoon breakout + closing momentum
]
# PCR thresholds (Put-Call Ratio sentiment)
PCR_BULLISH_MAX = 1.3     # PCR < 1.3 = bullish/neutral → Calls OK
PCR_BEARISH_MIN = 0.7     # PCR > 0.7 = bearish/neutral → Puts OK
# PCR is fetched live; fallback when unavailable
PCR_FALLBACK_NEUTRAL = 1.0

ANGEL_EXCHANGE = {
    "NIFTY": ("NSE", "Nifty 50"),
    "BANKNIFTY": ("NSE", "Nifty Bank"),
    "CRUDEOIL": ("MCX", "CRUDEOIL"),
    "GOLD": ("MCX", "GOLD"),
    "NATURALGAS": ("MCX", "NATURALGAS"),
}


# ============================================================
# BLACK-SCHOLES OPTION PRICING ENGINE + GREEKS
# ============================================================
def _norm_cdf(x: float) -> float:
    """Cumulative standard normal distribution using error function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    """Standard normal probability density function."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def black_scholes_greeks(S: float, K: float, T: float, r: float,
                         sigma: float, is_call: bool) -> dict:
    """
    Full Black-Scholes option pricing with all Greeks.

    S: underlying spot price
    K: strike price
    T: time to expiry in years (dte_days / 365)
    r: risk-free rate (0.07 = 7%)
    sigma: implied volatility (0.14 = 14%)

    Returns: {price, delta, gamma, theta, vega, d1, d2}
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        intrinsic = max(S - K, 0.0) if is_call else max(K - S, 0.0)
        return {"price": max(intrinsic, 0.5), "delta": 0.0, "gamma": 0.0,
                "theta": 0.0, "vega": 0.0, "d1": 0.0, "d2": 0.0}

    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    nd1 = _norm_cdf(d1)
    nd2 = _norm_cdf(d2)
    npd1 = _norm_pdf(d1)

    if is_call:
        price = S * nd1 - K * math.exp(-r * T) * nd2
        delta = nd1
        theta = (-(S * npd1 * sigma) / (2 * sqrt_T)
                 - r * K * math.exp(-r * T) * nd2) / 365.0
    else:
        price = K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)
        delta = -_norm_cdf(-d1)
        theta = (-(S * npd1 * sigma) / (2 * sqrt_T)
                 + r * K * math.exp(-r * T) * _norm_cdf(-d2)) / 365.0

    gamma = npd1 / (S * sigma * sqrt_T)
    vega = S * npd1 * sqrt_T / 100.0  # per 1% IV change

    return {
        "price": max(price, 0.5),
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta, 4),
        "vega": round(vega, 4),
        "d1": round(d1, 4),
        "d2": round(d2, 4),
    }


def bs_premium(underlying, strike, dte_days, is_call, iv):
    """Black-Scholes premium (replaces simplified atm_premium)."""
    T = max(dte_days, 0.05) / 365.0
    sigma = max(min(iv, 0.80), 0.08)
    g = black_scholes_greeks(underlying, strike, T, RISK_FREE_RATE, sigma, is_call)
    return g["price"]


def bs_premium_at(underlying_level, strike, dte_days, is_call, iv):
    """Black-Scholes premium at a different underlying level (for stop pricing)."""
    return bs_premium(underlying_level, strike, dte_days, is_call, iv)


def bs_greeks_full(underlying, strike, dte_days, is_call, iv) -> dict:
    """Full Greeks dict for trade logging."""
    T = max(dte_days, 0.05) / 365.0
    sigma = max(min(iv, 0.80), 0.08)
    return black_scholes_greeks(underlying, strike, T, RISK_FREE_RATE, sigma, is_call)


# ============================================================
# PCR (PUT-CALL RATIO) SENTIMENT FILTER
# ============================================================
def fetch_pcr(broker, underlying: str, sim_date=None) -> float:
    """
    Fetch Put-Call Ratio from Angel One option chain.

    PCR = Total PE Open Interest / Total CE Open Interest
    PCR > 1 = bearish (more puts = fear)
    PCR < 1 = bullish (more calls = greed)
    PCR 0.7-1.3 = neutral

    For backtest, we fetch the CURRENT option chain (live data).
    Returns PCR value or PCR_FALLBACK_NEUTRAL if unavailable.
    """
    try:
        from data.loader import fetch_option_chain_oi
        chain = fetch_option_chain_oi(broker, underlying=underlying,
                                      strikes_around_atm=15)
        if chain is None or chain.empty:
            return PCR_FALLBACK_NEUTRAL
        ce_oi = float(chain["CE_oi"].sum()) if "CE_oi" in chain.columns else 0
        pe_oi = float(chain["PE_oi"].sum()) if "PE_oi" in chain.columns else 0
        if ce_oi <= 0:
            return PCR_FALLBACK_NEUTRAL
        pcr = pe_oi / ce_oi
        logger.info(f"PCR {underlying}: {pcr:.2f} (CE_OI={ce_oi:.0f} PE_OI={pe_oi:.0f})")
        return pcr
    except Exception as exc:
        logger.debug(f"PCR fetch failed for {underlying}: {exc}")
        return PCR_FALLBACK_NEUTRAL


def pcr_allows_trade(pcr: float, direction: str) -> tuple[bool, str]:
    """
    PCR sentiment filter:
      BUY (Call): PCR should be < 1.3 (not too bearish)
      SELL (Put): PCR should be > 0.7 (not too bullish)
    """
    if direction == "BUY":
        if pcr < PCR_BULLISH_MAX:
            return (True, f"PCR {pcr:.2f} < {PCR_BULLISH_MAX} → bullish OK for Call")
        return (False, f"PCR {pcr:.2f} ≥ {PCR_BULLISH_MAX} → too bearish for Call")
    else:
        if pcr > PCR_BEARISH_MIN:
            return (True, f"PCR {pcr:.2f} > {PCR_BEARISH_MIN} → bearish OK for Put")
        return (False, f"PCR {pcr:.2f} ≤ {PCR_BEARISH_MIN} → too bullish for Put")


# ============================================================
# TREND LOCK (15m price action)
# ============================================================
def detect_15m_trend(df_15m, i) -> str:
    """Uptrend = HH+HL, Downtrend = LH+LL, else range."""
    if i < TREND_LOOKBACK + 2:
        return "range"
    recent = df_15m.iloc[i - TREND_LOOKBACK: i]
    highs = recent["high"].values
    lows = recent["low"].values
    n = len(highs)
    if n < 4:
        return "range"
    mid = n // 2
    fh, sh = max(highs[:mid]), max(highs[mid:])
    fl, sl = min(lows[:mid]), min(lows[mid:])
    if sh > fh and sl > fl:
        return "up"
    if sh < fh and sl < fl:
        return "down"
    return "range"


def trend_allows_trade(trend: str, direction: str) -> bool:
    if direction == "BUY" and trend == "up":
        return True
    if direction == "SELL" and trend == "down":
        return True
    return False


# ============================================================
# GOLDEN WINDOW (time filter)
# ============================================================
def in_golden_window(ts, segment) -> bool:
    if segment == "commodity":
        return _in_entry_window(ts, segment)
    ts_str = ts.strftime("%H:%M") if ts.tz is None else ts.tz_convert("Asia/Kolkata").strftime("%H:%M")
    for start, end in GOLDEN_WINDOWS_NSE:
        if start <= ts_str < end:
            return True
    return False


# ============================================================
# DYNAMIC LOT-SIZING
# ============================================================
def size_dynamic(entry_premium, stop_premium, lot_sz, current_capital,
                 max_loss_cap=2000.0, risk_pct=RISK_PCT_PER_TRADE):
    stop_per_unit = abs(entry_premium - stop_premium)
    if stop_per_unit <= 0:
        return {"lots": 1, "quantity": lot_sz, "max_loss": entry_premium * lot_sz,
                "stop_per_unit": 0.0, "risk_amount": 0.0}
    risk_amount = min(current_capital * risk_pct / 100.0, max_loss_cap)
    loss_per_lot = stop_per_unit * lot_sz
    lots = max(1, int(risk_amount // loss_per_lot))
    quantity = lots * lot_sz
    actual_risk = stop_per_unit * quantity
    return {"lots": lots, "quantity": quantity, "max_loss": actual_risk,
            "stop_per_unit": stop_per_unit, "risk_amount": round(actual_risk, 2)}


# ============================================================
# STRUCTURAL STOP (zone-breach based, wider)
# ============================================================
def compute_structural_stop(entry_premium, zone, zone_type, cur_underlying,
                            strike, dte, is_call, iv, max_loss_cap=2000.0):
    if zone_type == "demand":
        stop_underlying = zone["bottom"]
    else:
        stop_underlying = zone["top"]
    stop_prem = bs_premium_at(stop_underlying, strike, dte, is_call, iv)
    stop_prem = max(stop_prem, 0.5)
    # Wider stop (let trade breathe), but never above 85% of entry
    stop_prem = min(stop_prem, entry_premium * 0.85)
    stop_prem = max(stop_prem, 0.5)
    return stop_prem


# ============================================================
# PREMIUM SANITY CHECK
# ============================================================
def premium_sane(entry_premium, cur_underlying) -> tuple[bool, str]:
    if entry_premium < PREMIUM_MIN:
        return (False, f"premium too low (₹{entry_premium:.1f} < ₹{PREMIUM_MIN})")
    pct = entry_premium / cur_underlying * 100
    if pct > PREMIUM_MAX_PCT_OF_UNDERLYING:
        return (False, f"premium overpriced ({pct:.1f}% of underlying)")
    return (True, "sane")


# ============================================================
# TOKEN RESOLUTION (with MCX fix)
# ============================================================
def _resolve_symbol_token(symbol: str) -> tuple[str, str] | None:
    if symbol in ANGEL_EXCHANGE:
        exchange, search = ANGEL_EXCHANGE[symbol]
    else:
        exchange, search = "NSE", symbol
    try:
        matches = find_symbol_token(exchange, search)
    except Exception as exc:
        logger.error(f"{symbol}: instrument master lookup failed: {exc}")
        return None
    if matches is None or matches.empty:
        logger.warning(f"{symbol}: no instrument match for '{search}' on {exchange}")
        return None
    sym_upper = search.upper()
    if exchange == "MCX":
        mcom = matches[matches["symbol"].str.upper().str.contains("MCOM", na=False)]
        if not mcom.empty:
            row = mcom.iloc[0]
        else:
            fut = matches[matches["symbol"].str.upper().str.contains(
                "FUT", na=False) | matches["symbol"].str.upper().str.startswith(sym_upper)]
            row = fut.iloc[0] if not fut.empty else matches.iloc[0]
        token = str(row["token"])
        logger.info(f"{symbol}: {exchange} token={token} ({row['symbol']})")
        return exchange, token
    exact_eq = matches[matches["symbol"].str.upper() == f"{sym_upper}-EQ"]
    if exact_eq.empty:
        exact_eq = matches[matches["symbol"].str.upper() == sym_upper]
    if exact_eq.empty:
        exact_eq = matches[matches["symbol"].str.upper().str.endswith("-EQ")]
    row = exact_eq.iloc[0] if not exact_eq.empty else matches.iloc[0]
    token = str(row["token"])
    logger.info(f"{symbol}: {exchange} token={token} ({row['symbol']})")
    return exchange, token


def fetch_angel_data(broker, days_15m: int = 60, days_1m: int = 60):
    to_date = datetime.now().replace(hour=15, minute=30, second=0, microsecond=0)
    from_15m = to_date - timedelta(days=days_15m)
    from_1m = to_date - timedelta(days=days_1m)
    data_map: dict[str, pd.DataFrame] = {}
    data_map_1m: dict[str, pd.DataFrame] = {}
    failed: list[str] = []
    syms = all_symbols()
    total = len(syms)
    for idx, (sym, _tk) in enumerate(syms.items(), 1):
        tag = f"[{idx}/{total}] {sym}"
        mapping = _resolve_symbol_token(sym)
        if mapping is None:
            failed.append(sym)
            continue
        exchange, token = mapping
        try:
            d15 = fetch_angel_historical_candles(
                broker, exchange, token, "FIFTEEN_MINUTE", from_15m, to_date)
            if d15 is not None and not d15.empty:
                data_map[sym] = _normalize_cols(d15)
            else:
                failed.append(sym)
                continue
        except Exception as exc:
            logger.error(f"{tag}: 15m fetch error: {exc}")
            failed.append(sym)
            continue
        try:
            d1 = fetch_angel_historical_candles(
                broker, exchange, token, "ONE_MINUTE", from_1m, to_date)
            if d1 is not None and not d1.empty:
                data_map_1m[sym] = _normalize_cols(d1)
        except Exception as exc:
            logger.warning(f"{tag}: 1m fetch error: {exc}")
        print(f"  {tag:30s}: 15m={len(data_map[sym]):5d}  "
              f"1m={len(data_map_1m.get(sym, [])):5d}")
        time.sleep(0.4)
    print(f"\nFailed symbols: {failed}")
    print(f"Universe loaded from Angel One: 15m={len(data_map)}  1m={len(data_map_1m)}")
    return data_map, data_map_1m, failed


# ============================================================
# A+ ENTRY ENGINE — ALL 5 BRAINS AWAKE + Tiger PRO filters
# ============================================================
def find_tiger_pro_max_entry(df_15m, i_15m, df_1m, seg, is_expiry, symbol,
                             broker, pcr_cache: dict):
    """
    FULL V6.6 Sniper Engine (Brain 3 = find_sniper_entry) +
    Tiger PRO filters (Trend Lock, Golden Window, PCR, Premium Sanity).

    ALL 5 brains awake:
      - Brain 2: detect_zones_explosive + delta_spike_confirms + liquidity_sweep
      - Brain 3: find_sniper_entry (calls all Brain 2 functions internally)

    Returns dict or None.
    """
    # --- BRAIN 3: V6.6 FULL SNIPER ENTRY (wakes Brain 2 internally) ---
    sniper = find_sniper_entry(df_15m, i_15m, df_1m, seg, is_expiry)
    if sniper is None:
        return None

    # --- TIGER PRO FILTER 1: TREND LOCK ---
    trend = detect_15m_trend(df_15m, i_15m)
    if not trend_allows_trade(trend, sniper["direction"]):
        return None
    sniper["trend"] = trend

    # --- TIGER PRO FILTER 2: PCR SENTIMENT ---
    if symbol not in pcr_cache:
        pcr_cache[symbol] = fetch_pcr(broker, symbol)
    pcr = pcr_cache[symbol]
    pcr_ok, pcr_reason = pcr_allows_trade(pcr, sniper["direction"])
    if not pcr_ok:
        return None
    sniper["pcr"] = round(pcr, 2)
    sniper["pcr_reason"] = pcr_reason

    return sniper


# ============================================================
# MAIN BACKTEST ENGINE
# ============================================================
def run_tiger_pro_max_backtest(data_map, start_capital=150000.0,
                               max_loss_per_trade=2000.0,
                               max_capital_per_trade_pct=10.0,
                               dte_default=1.0, verbose=False,
                               data_map_1m=None, broker=None):
    use_sniper = data_map_1m is not None
    all_ts = sorted(set().union(*[set(d.index) for d in data_map.values()]))
    days = defaultdict(list)
    for ts in all_ts:
        days[ts.normalize()].append(ts)
    sorted_days = sorted(days.keys())

    capital = start_capital
    peak_equity = start_capital
    max_dd = 0.0
    equity_curve = []
    trades = []
    open_positions = []
    counter = TradeCounterGuard()
    daily_seg_counts = defaultdict(lambda: defaultdict(int))
    pcr_cache: dict[str, float] = {}
    filter_stats = defaultdict(int)
    brain_log = defaultdict(int)

    for day in sorted_days:
        day_ts = days[day]
        sim_date = day.date() if day.tz is None else day.tz_convert("Asia/Kolkata").date()
        if counter._today != sim_date:
            counter._today = sim_date
            counter.global_count = 0
            counter.commodity_count = 0
            pcr_cache.clear()  # refresh PCR daily

        for ts in day_ts:
            # --- 1. EXIT open positions (Brain 5) ---
            still_open = []
            for pos in open_positions:
                sym = pos["symbol"]
                seg = pos["segment"]
                df_sym = data_map[sym]
                if ts not in df_sym.index:
                    still_open.append(pos)
                    continue
                cur_underlying = float(df_sym.loc[ts, "close"])
                df_so_far = df_sym.loc[:ts]
                iv = max(min(realized_vol_simple(df_so_far), 0.80), 0.12)
                is_call = pos["option_type"] == "CE"
                # BLACK-SCHOLES pricing for current premium
                cur_prem = bs_premium(cur_underlying, pos["strike"],
                                      pos["dte"], is_call, iv)
                if cur_prem > pos.get("peak_premium", 0):
                    pos["peak_premium"] = cur_prem
                sq_off = _is_square_off_bar(ts, seg)
                i_1m_pos = None
                df_1m_pos = None
                if use_sniper and data_map_1m and sym in data_map_1m:
                    df_1m_pos = data_map_1m[sym]
                    if ts in df_1m_pos.index:
                        i_1m_pos = df_1m_pos.index.get_loc(ts)
                ex = check_intraday_exit(pos, cur_underlying, cur_prem, sq_off,
                                         df_1m=df_1m_pos, i_1m=i_1m_pos)
                if ex["exit"]:
                    exit_prem = ex["exit_premium"]
                    slippage = exit_prem * 0.008 + pos["entry_premium"] * 0.008
                    brokerage = 20.0 * 2
                    pnl = (exit_prem - pos["entry_premium"]) * pos["quantity"] - slippage * 2 - brokerage
                    pnl = max(pnl, -max_loss_per_trade - brokerage - slippage * 2 + 1)
                    capital += pnl
                    # Log exit Greeks
                    exit_greeks = bs_greeks_full(cur_underlying, pos["strike"],
                                                 pos["dte"], is_call, iv)
                    trades.append({
                        **pos, "exit_ts": ts, "exit_premium": exit_prem, "pnl": pnl,
                        "exit_reason": ex["reason"],
                        "hold_bars": len(df_so_far) - pos["entry_idx"],
                        "entry_spread_pct": pos.get("entry_spread_pct", 0.0),
                        "confirmation": pos.get("delta_reason", "A+"),
                        "exit_delta": exit_greeks["delta"],
                        "exit_gamma": exit_greeks["gamma"],
                        "exit_theta": exit_greeks["theta"],
                        "exit_vega": exit_greeks["vega"],
                    })
                    if verbose:
                        logger.warning(f"EXIT {sym} {ex['reason']} pnl={pnl:.0f}")
                else:
                    pos["dte"] = max(pos["dte"] - 1 / 25, 0.05)
                    still_open.append(pos)
            open_positions = still_open

            # --- 2. A+ ENTRY SCAN (ALL 5 BRAINS + Tiger PRO filters) ---
            day_candidates = []
            for sym, df_sym in data_map.items():
                if ts not in df_sym.index:
                    continue
                idx = df_sym.index.get_loc(ts)
                if idx < 40:
                    continue
                seg = segment_of(sym)

                # --- BRAIN 1: Gate ---
                b1 = brain1_intraday_pass(df_sym, idx, segment=seg)
                if not b1["passed_brain1"]:
                    continue
                brain_log["brain1_pass"] += 1

                if _is_square_off_bar(ts, seg):
                    continue

                # --- TIGER PRO FILTER: Golden Window ---
                if not in_golden_window(ts, seg):
                    filter_stats["rejected_golden_window"] += 1
                    continue

                expiry = use_sniper and is_expiry_day(sym, ts)

                if use_sniper and data_map_1m and sym in data_map_1m:
                    # --- BRAINS 2+3: Full V6.6 Sniper + Tiger PRO filters ---
                    setup = find_tiger_pro_max_entry(
                        df_sym, idx, data_map_1m[sym], seg, expiry, sym,
                        broker, pcr_cache)
                    if setup is None:
                        filter_stats["rejected_sniper_or_filters"] += 1
                        continue
                    brain_log["brain23_sniper_pass"] += 1
                else:
                    continue

                day_candidates.append({
                    "symbol": sym, "setup": setup, "df_sym": df_sym,
                    "idx": idx, "ts": ts, "seg": seg, "expiry": expiry,
                })

            day_candidates.sort(key=lambda c: c["setup"]["setup_score"], reverse=True)

            for cand in day_candidates:
                sym = cand["symbol"]
                seg = cand["seg"]
                if not counter.can_trade(sym)["allowed"]:
                    continue
                if daily_seg_counts[sim_date][seg] >= 10:
                    continue

                setup = cand["setup"]
                df_sym = cand["df_sym"]
                idx = cand["idx"]
                expiry = cand.get("expiry", False)
                cur_underlying = float(df_sym.iloc[idx]["close"])
                df_so_far = df_sym.loc[:ts]
                iv = max(min(realized_vol_simple(df_so_far), 0.80), 0.12)
                is_call = setup["direction"] == "BUY"

                strike_kind = setup.get("strike_kind", "ATM")
                delta_in_reason = "delta" in setup.get("delta_reason", "")
                if (expiry and delta_in_reason) or strike_kind == "ITM":
                    strike = round(cur_underlying * 0.99) if is_call else round(cur_underlying * 1.01)
                else:
                    strike = round(cur_underlying)

                # BLACK-SCHOLES entry premium
                entry_prem = bs_premium(cur_underlying, strike, dte_default, is_call, iv)
                entry_prem = max(entry_prem, 1.0)

                # --- TIGER PRO FILTER: Premium Sanity ---
                sane, sane_reason = premium_sane(entry_prem, cur_underlying)
                if not sane:
                    filter_stats["rejected_premium_sanity"] += 1
                    if verbose:
                        logger.warning(f"REJECT {sym} {sane_reason}")
                    continue

                ok_spread, spread_pct = spread_ok(sym, entry_prem)
                if not ok_spread:
                    filter_stats["rejected_spread"] += 1
                    continue

                # --- STRUCTURAL STOP (Black-Scholes priced) ---
                stop_prem = compute_structural_stop(
                    entry_premium=entry_prem,
                    zone={"top": setup["zone_top"], "bottom": setup["zone_bottom"]},
                    zone_type=setup["zone_type"],
                    cur_underlying=cur_underlying,
                    strike=strike, dte=dte_default, is_call=is_call, iv=iv,
                    max_loss_cap=max_loss_per_trade,
                )
                stop_prem = max(stop_prem, 0.5)

                lot_sz = lot_size(sym)
                sizing = size_dynamic(entry_prem, stop_prem, lot_sz, capital,
                                      max_loss_cap=max_loss_per_trade)
                alloc = sizing["quantity"] * entry_prem
                if alloc > capital * max_capital_per_trade_pct / 100:
                    max_alloc = capital * max_capital_per_trade_pct / 100
                    lots_fit = max(1, int(max_alloc // (entry_prem * lot_sz)))
                    if lots_fit < sizing["lots"]:
                        sizing["lots"] = lots_fit
                        sizing["quantity"] = lots_fit * lot_sz
                        sizing["max_loss"] = sizing["stop_per_unit"] * sizing["quantity"]
                    alloc = sizing["quantity"] * entry_prem
                if sizing["quantity"] <= 0 or alloc > capital:
                    continue

                counter.register_trade(sym)
                daily_seg_counts[sim_date][seg] += 1

                zone_edge = setup["zone_bottom"] if is_call else setup["zone_top"]
                opp_zone = None
                if idx - 1 >= 40:
                    opp_zone = find_opposing_zone(df_sym, idx - 1, setup["zone_type"])
                if is_call and opp_zone:
                    opposing_zone_edge = opp_zone["bottom"]
                elif (not is_call) and opp_zone:
                    opposing_zone_edge = opp_zone["top"]
                else:
                    atr = _simple_range(df_sym, idx)
                    opposing_zone_edge = (cur_underlying + atr * 6) if is_call else (cur_underlying - atr * 6)

                # Entry Greeks (Black-Scholes)
                entry_greeks = bs_greeks_full(cur_underlying, strike, dte_default,
                                              is_call, iv)

                pos = {
                    "symbol": sym, "segment": seg,
                    "strategy": setup["strategy"],
                    "direction": setup["direction"],
                    "option_type": "CE" if is_call else "PE",
                    "strike": strike,
                    "entry_premium": entry_prem,
                    "stop_premium": stop_prem,
                    "quantity": sizing["quantity"], "lots": sizing["lots"],
                    "max_loss": sizing["max_loss"],
                    "risk_amount": sizing.get("risk_amount", 0.0),
                    "entry_ts": ts, "entry_idx": idx,
                    "dte": dte_default,
                    "iv": iv,
                    "zone_type": setup["zone_type"],
                    "zone_edge": zone_edge,
                    "opposing_zone_edge": opposing_zone_edge,
                    "peak_premium": entry_prem,
                    "strike_kind": strike_kind,
                    "is_expiry": expiry,
                    "delta_spike_mult": setup.get("delta_spike_mult", 0.0),
                    "explosive": setup.get("explosive", False),
                    "expansion_pct": setup.get("expansion_pct", 0.0),
                    "sweep": setup.get("sweep", False),
                    "sweep_reason": setup.get("sweep_reason", ""),
                    "entry_spread_pct": spread_pct,
                    "trend": setup.get("trend", "range"),
                    "pcr": setup.get("pcr", 1.0),
                    "structural_stop": True,
                    "pricing_model": "Black-Scholes",
                    # Entry Greeks
                    "entry_delta": entry_greeks["delta"],
                    "entry_gamma": entry_greeks["gamma"],
                    "entry_theta": entry_greeks["theta"],
                    "entry_vega": entry_greeks["vega"],
                }
                open_positions.append(pos)
                if verbose:
                    logger.warning(
                        f"ENTRY {sym} {setup['strategy']} @{entry_prem:.1f} "
                        f"Δ={entry_greeks['delta']:.2f} Γ={entry_greeks['gamma']:.4f} "
                        f"θ={entry_greeks['theta']:.1f} ν={entry_greeks['vega']:.1f} "
                        f"PCR={setup.get('pcr', '?')} trend={setup.get('trend','?')}"
                    )

            equity_curve.append(capital)
            if capital > peak_equity:
                peak_equity = capital
            dd = (peak_equity - capital) / peak_equity * 100
            if dd > max_dd:
                max_dd = dd

    # Close remaining positions at last bar
    for pos in open_positions:
        sym = pos["symbol"]
        df_sym = data_map[sym]
        last_ts = df_sym.index[-1]
        cur_underlying = float(df_sym.iloc[-1]["close"])
        iv = max(min(realized_vol_simple(df_sym), 0.80), 0.12)
        is_call = pos["option_type"] == "CE"
        cur_prem = bs_premium(cur_underlying, pos["strike"], pos["dte"], is_call, iv)
        slippage = cur_prem * 0.008 + pos["entry_premium"] * 0.008
        brokerage = 20.0 * 2
        pnl = (cur_prem - pos["entry_premium"]) * pos["quantity"] - slippage * 2 - brokerage
        pnl = max(pnl, -max_loss_per_trade - brokerage - slippage * 2 + 1)
        capital += pnl
        exit_greeks = bs_greeks_full(cur_underlying, pos["strike"], pos["dte"], is_call, iv)
        trades.append({
            **pos, "exit_ts": last_ts, "exit_premium": cur_prem, "pnl": pnl,
            "exit_reason": "end_of_data",
            "hold_bars": len(df_sym) - pos["entry_idx"],
            "entry_spread_pct": pos.get("entry_spread_pct", 0.0),
            "confirmation": pos.get("delta_reason", "A+"),
            "exit_delta": exit_greeks["delta"],
            "exit_gamma": exit_greeks["gamma"],
            "exit_theta": exit_greeks["theta"],
            "exit_vega": exit_greeks["vega"],
        })

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))

    daily_counts = Counter()
    daily_comm_counts = Counter()
    for t in trades:
        d = t["entry_ts"].normalize()
        daily_counts[d] += 1
        if t["segment"] == "commodity":
            daily_comm_counts[d] += 1
    max_trades_day = max(daily_counts.values()) if daily_counts else 0
    max_comm_day = max(daily_comm_counts.values()) if daily_comm_counts else 0

    seg_stats = {}
    for seg_key in ("index", "stock", "commodity"):
        seg_trades = [t for t in trades if t["segment"] == seg_key]
        if not seg_trades:
            seg_stats[seg_key] = None
            continue
        sw = [t for t in seg_trades if t["pnl"] > 0]
        sl = [t for t in seg_trades if t["pnl"] <= 0]
        gp = sum(t["pnl"] for t in sw)
        gl = abs(sum(t["pnl"] for t in sl))
        seg_stats[seg_key] = {
            "label": UNIVERSE[seg_key]["label"],
            "trades": len(seg_trades),
            "wins": len(sw),
            "win_rate_pct": round(len(sw) / len(seg_trades) * 100, 1) if seg_trades else 0.0,
            "net_pnl": round(sum(t["pnl"] for t in seg_trades), 2),
            "profit_factor": round(gp / gl, 2) if gl > 0 else float("inf"),
        }

    strat_stats = {}
    for t in trades:
        sname = t.get("strategy", "unknown")
        if sname not in strat_stats:
            strat_stats[sname] = {"trades": 0, "wins": 0, "pnl": 0.0}
        strat_stats[sname]["trades"] += 1
        if t["pnl"] > 0:
            strat_stats[sname]["wins"] += 1
        strat_stats[sname]["pnl"] += t["pnl"]

    totals = {
        "start_capital": start_capital,
        "final_equity": round(capital, 2),
        "total_return_pct": round((capital - start_capital) / start_capital * 100, 2),
        "net_pnl": round(capital - start_capital, 2),
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "avg_pnl_per_trade": round(sum(t["pnl"] for t in trades) / len(trades), 2) if trades else 0.0,
        "avg_winner": round(gross_profit / len(wins), 2) if wins else 0.0,
        "avg_loser": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "best_trade": round(max((t["pnl"] for t in trades), default=0), 2),
        "worst_trade": round(min((t["pnl"] for t in trades), default=0), 2),
        "max_drawdown_pct": round(max_dd, 2),
        "max_daily_global_trades": max_trades_day,
        "max_daily_commodity_trades": max_comm_day,
    }

    return {
        "totals": totals,
        "trades": trades,
        "segment_stats": seg_stats,
        "strategy_stats": strat_stats,
        "equity_curve": equity_curve,
        "filter_stats": dict(filter_stats),
        "brain_log": dict(brain_log),
        "pricing_model": "Black-Scholes",
    }


def main():
    print("\n" + "#" * 72)
    print("#  TIGER PRO MAX — SUPER POWERFUL RACE CAR ENGINE (Variant D)")
    print("#  ALL 5 BRAINS AWAKE: Brain1+Brain2(6 func)+Brain3+Brain4+Brain5")
    print("#  Black-Scholes Pricing | Greeks (Δ,Γ,θ,ν) | PCR Sentiment Filter")
    print("#  Trend Lock + Golden Window + Structural Stop + Dynamic Sizing")
    print("#  Race Car aggressiveness + Tiger PRO quality = SUPER POWERFUL")
    print("#  V6.6 core: 100% UNTOUCHED (imports only, no modifications)")
    print("#" * 72)

    print("\nLoading Angel One instrument master...")
    try:
        load_angel_instrument_master()
        print("  ✓ Instrument master loaded.")
    except Exception as exc:
        print(f"  ✗ Instrument master load failed: {exc}")
        return

    print("\nLogging in to Angel One SmartAPI...")
    broker = AngelBroker()
    try:
        broker.login()
        print("  ✓ Angel One login successful.")
    except Exception as exc:
        print(f"  ✗ Angel One login failed: {exc}")
        return

    print("\nFetching 15-min + 1-min intraday data from Angel One (60 days)...")
    data_map, data_map_1m, failed = fetch_angel_data(broker)

    if not data_map:
        print("\n✗ No data fetched — cannot run backtest.")
        return

    print(f"\nRunning TIGER PRO MAX portfolio (₹1.5L, {len(data_map)} symbols)...")
    combined = run_tiger_pro_max_backtest(
        data_map, start_capital=150000.0, max_loss_per_trade=2000.0,
        data_map_1m=data_map_1m if data_map_1m else None,
        broker=broker,
    )

    print("\n\n")
    print("#" * 72)
    print("#  TIGER PRO MAX — COMBINED PORTFOLIO (Variant D, Angel One data)")
    print("#  Black-Scholes Pricing + Greeks + PCR + ALL 5 BRAINS AWAKE")
    print("#" * 72)
    print_report(combined)

    # Filter statistics
    fs = combined.get("filter_stats", {})
    if fs:
        print("\n" + "-" * 72)
        print("  FILTER STATISTICS (rejections by each filter)")
        print("-" * 72)
        for fname, count in sorted(fs.items(), key=lambda x: -x[1]):
            print(f"  {fname:40s}: {count:6d} rejections")
        print("-" * 72)

    # Brain activity log
    bl = combined.get("brain_log", {})
    if bl:
        print("\n" + "-" * 72)
        print("  BRAIN ACTIVITY LOG (how many times each brain fired)")
        print("-" * 72)
        for bname, count in sorted(bl.items(), key=lambda x: -x[1]):
            print(f"  {bname:40s}: {count:6d} activations")
        print("-" * 72)

    # Greeks summary for all trades
    if combined["trades"]:
        print("\n" + "-" * 72)
        print("  GREEKS SUMMARY (Black-Scholes entry/exit)")
        print("-" * 72)
        avg_ed = sum(t.get("entry_delta", 0) for t in combined["trades"]) / len(combined["trades"])
        avg_et = sum(t.get("entry_theta", 0) for t in combined["trades"]) / len(combined["trades"])
        avg_ev = sum(t.get("entry_vega", 0) for t in combined["trades"]) / len(combined["trades"])
        avg_eg = sum(t.get("entry_gamma", 0) for t in combined["trades"]) / len(combined["trades"])
        print(f"  Avg Entry Delta:  {avg_ed:+.4f}  (0.5=ATM, >0.5=ITM)")
        print(f"  Avg Entry Gamma:  {avg_eg:.6f}  (higher=more gamma squeeze potential)")
        print(f"  Avg Entry Theta:  {avg_et:+.2f}/day  (time decay cost)")
        print(f"  Avg Entry Vega:   {avg_ev:+.2f}  (per 1% IV change)")
        print("-" * 72)

    # Top 10 trades by P&L
    top_trades = sorted(combined["trades"], key=lambda t: t["pnl"], reverse=True)[:10]
    if top_trades:
        print("\n" + "-" * 72)
        print("  TOP 10 TRADES BY P&L (with Greeks + PCR + Trend)")
        print("-" * 72)
        print(f"  {'Symbol':12s} {'Dir':4s} {'Entry':>7s} {'Exit':>7s} {'P&L':>8s} "
              f"{'Δ':>6s} {'θ':>6s} {'PCR':>5s} {'Trend':6s} {'ExitReason'}")
        print("  " + "-" * 68)
        for t in top_trades:
            print(f"  {t['symbol']:12s} {t['direction']:4s} "
                  f"{t['entry_premium']:7.1f} {t.get('exit_premium',0):7.1f} "
                  f"{t['pnl']:+8.0f} "
                  f"{t.get('entry_delta',0):+6.2f} {t.get('entry_theta',0):+6.1f} "
                  f"{t.get('pcr',0):5.2f} {t.get('trend','?'):6s} "
                  f"{t.get('exit_reason','?')}")
        print("-" * 72)

    for seg_key in ("index", "stock", "commodity"):
        seg_syms = list(UNIVERSE[seg_key]["symbols"].keys())
        seg_map = {s: data_map[s] for s in seg_syms if s in data_map}
        seg_map_1m = {s: data_map_1m[s] for s in seg_syms if s in data_map_1m}
        print("\n\n")
        print("#" * 72)
        print(f"#  TIGER PRO MAX — {UNIVERSE[seg_key]['label'].upper()} (standalone, ₹1.5L)")
        print("#" * 72)
        if not seg_map:
            print("  (no data for this segment)")
            continue
        print(f"  Running {seg_key} standalone ({len(seg_map)} symbols)...")
        seg_res = run_tiger_pro_max_backtest(
            seg_map, start_capital=150000.0, max_loss_per_trade=2000.0,
            data_map_1m=seg_map_1m if seg_map_1m else None,
            broker=broker,
        )
        print_report(seg_res)

    try:
        broker.logout()
        print("\n✓ Angel One session logged out.")
    except Exception:
        pass

    print("\n" + "=" * 72)
    print("  TIGER PRO MAX backtest complete.")
    print("=" * 72)


if __name__ == "__main__":
    main()
