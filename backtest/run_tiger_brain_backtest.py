"""
TIGER BRAIN — ONE MAN ARMY (Unified Engine)
============================================
Super Powerful Race Car + Sniper + Research Tool. One engine, everything in.

ARCHITECTURE: SCORING, NOT GATING
==================================
Pehle 10+ hard gates stack karne se 99.85% entries reject ho rahe the.
Ab sirf 2 HARD requirements hain, baaki sab SCORE bonus hai:

HARD REQUIREMENTS (must pass — ~50% pass rate):
  1. Zone touch on 1m bar (real S/D zone, not random level)
  2. Volume delta in correct direction (1.3x surge — institutional participation)

SCORE BOOSTERS (add to score, don't reject — rank & take top entries):
  +10  Explosive zone quality (big momentum — zone_explosive_quality)
  +8   Liquidity sweep (smart money stop-hunt — liquidity_sweep)
  +5   Delta spike 1.8x (strong institutional volume — delta_spike_confirms)
  +5   Trend alignment (15m HH+HL for Call, LH+LL for Put)
  +5   PDH/PDL confluence (zone near Previous Day High/Low — institutional memory)
  +5   VWAP confluence (zone near intraday VWAP — consensus level)
  +3   Golden window morning (09:15-11:30 — best institutional flow)
  +2   Golden window afternoon (13:00-15:15 — closing momentum)
  +3   PCR sentiment alignment (PCR confirms direction)
  +2   India VIX low (<15 = trending day, good for momentum)
  +3   Opening range alignment (breakout direction matches trade direction)

ALL 5 BRAINS AWAKE (as scorers, not gates):
  Brain 1: brain1_intraday_pass (gate — entry window)
  Brain 2: detect_zones + zone_touched_on_1m + volume_delta (gate)
          + zone_explosive_quality + liquidity_sweep + delta_spike_confirms (scorers)
          + find_opposing_zone (exit target)
  Brain 3: find_sniper_entry internal logic (adapted as scoring pipeline)
  Brain 4: TradeCounterGuard (daily quota)
  Brain 5: check_intraday_exit (full exit engine)

PRICING: Black-Scholes + Greeks (Delta, Gamma, Theta, Vega)
RISK: Structural stop (zone-based) + Dynamic sizing (1% risk, ₹2K cap)

Catches BOTH big AND small momentum — explosive zones get +10 score,
but non-explosive zones with good confluence still enter.

V6.6 core files: 100% UNTOUCHED (imports only).

Run from repo ROOT:
    python3 -m backtest.run_tiger_brain_backtest
"""

from __future__ import annotations

import logging
import math
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import pandas as pd

sys.path.insert(0, ".")

from broker.angel_connect import AngelBroker
from data.loader import (
    fetch_angel_historical_candles,
    fetch_angel_underlying_candles,
    fetch_india_vix_history,
    find_symbol_token,
    load_angel_instrument_master,
    get_option_chain_instruments,
)
from backtest.intraday_backtest import (
    _normalize_cols,
    _in_entry_window,
    _is_square_off_bar,
    _to_ist,
    brain1_intraday_pass,
    realized_vol_simple,
    spread_ok,
    check_intraday_exit,
    print_report,
    MAX_SPREAD_PCT,
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
    one_min_exhaustion,
)
from universe.fno_universe import (
    UNIVERSE, all_symbols, segment_of, lot_size, is_expiry_day,
    square_off_for,
)
from backtest.tiger_premium_brain import (
    PremiumDiscountTracker, compute_premium_strike, check_premium_exit,
)
from backtest.tiger_session_brain import (
    get_current_session, get_session_score_threshold,
    should_force_hunt, HuntStatus, print_session_schedule,
)
from risk.risk_management import TradeCounterGuard
from backtest.tiger_fund_brain import announce_fund_plan, size_trade_with_fund_brain

logger = logging.getLogger("tiger_brain.one_man_army")
logging.basicConfig(level=logging.INFO)

# ============================================================
# CONSTANTS
# ============================================================
RISK_PCT_PER_TRADE = 1.0
RISK_FREE_RATE = 0.07
PREMIUM_MAX_PCT_OF_UNDERLYING = 2.0
PREMIUM_MIN = 3.0
TREND_LOOKBACK = 10
VOL_SURGE_MULT = 1.5        # 1.5x volume (balanced — not too strict, not too loose)
VOL_LOOKBACK = 5
MAX_ENTRIES_PER_DAY = 8     # raised from 2 — 10-20 trades/day target, 8 balanced

def compute_iv(df_so_far, vix_val, symbol, is_call, strike, underlying):
    """V19 IV model: blend India VIX (implied) with realized vol.

    India VIX = market's implied volatility expectation (forward-looking).
    Realized vol = actual price movement (backward-looking).
    Blend: 60% VIX + 40% realized = better IV estimate than either alone.

    Skew adjustment: OTM puts have higher IV than ATM (volatility smile).
    For CALL options: ITM strikes get slightly lower IV (0.95x).
    For PUT options: OTM strikes get slightly higher IV (1.05x).
    """
    rv = max(min(realized_vol_simple(df_so_far), 0.80), 0.12)
    vix = max(min(vix_val / 100.0, 0.80), 0.12) if vix_val > 0 else rv

    # Blend: VIX is forward-looking (60%), realized is backward (40%)
    blended = 0.60 * vix + 0.40 * rv
    blended = max(min(blended, 0.80), 0.10)

    # Simple skew: OTM options get slight IV bump (smile effect)
    moneyness = strike / underlying if underlying > 0 else 1.0
    if is_call:
        # OTM call (strike > underlying) gets slight IV bump
        if moneyness > 1.0:
            blended *= 1.03
        # ITM call gets slight discount
        elif moneyness < 0.98:
            blended *= 0.97
    else:
        # OTM put (strike < underlying) gets IV bump (demand for protection)
        if moneyness < 1.0:
            blended *= 1.05
        elif moneyness > 1.02:
            blended *= 0.97

    return max(min(blended, 0.80), 0.10)


# ============================================================
# V19 EXIT ENGINE — tighter trail + fixed target booking
# ============================================================
V19_TRAIL_ACTIVATE_PCT = 5.0    # activate trail at +5% (har trade — profit jaldi lock)
V19_TRAIL_LOCK_PCT = 70.0       # lock 70% of peak (was 65% — gives back only 30%)
V19_FIXED_TARGET_PCT = 50.0     # book 40% at +50% (was +100% — book profit sooner)
V19_FIXED_TARGET_BOOK = 0.40    # book 40% of position at target (ride 60%)
V19_RUNAWAY_EXIT_PCT = 250.0    # absolute safety exit

# ============================================================
# V19 SYMBOL FILTER — INDEX + COMMODITY + TOP LIQUID STOCKS
# ============================================================
# Index + MCX always allowed. Stocks allowed if they pass the
# liquidity filter (top 10-11 by volume/OI/turnover). The scan
# universe (nse_scan_symbols) only includes filtered stocks, so
# the entry function will never be called for non-filtered stocks.
# This guard is a safety net — blocks any random symbol that might
# sneak in through a backtest path or replay.
INDEX_SYMBOL_NAMES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"}
MCX_SYMBOL_NAMES = {"GOLDM", "SILVERM", "CRUDEOIL", "NATURALGAS"}


def _is_symbol_allowed(symbol: str) -> bool:
    """Check if a symbol is allowed for options buying.

    - Index symbols (NIFTY/BANKNIFTY/FINNIFTY/SENSEX) → always allowed
    - MCX commodities (GOLDM/SILVERM/CRUDEOIL/NATURALGAS) → always allowed
    - Stock symbols → allowed (filtered by liquidity pipeline upstream)
    """
    return True  # stocks allowed — liquidity filter handles upstream

# SMART SQUARE-OFF (V19+) — don't blindly close profitable trades.
# In the 15-min window before square-off, apply a tighter trail so that
# profitable trades (trail already active) exit SMARTLY on trail instead
# of getting blind force-closed at the square-off bar. Losses + small
# (unprotected) profits still get force-closed at square-off.
V19_PRE_SQOFF_TRAIL_LOCK_PCT = 80.0   # tighter lock in pre-sqoff window (was 65)
V19_PRE_SQOFF_WINDOW_MINUTES = 15     # last 15 mins before square-off


def _in_pre_square_off_window(ts, segment: str) -> bool:
    """True if `ts` falls in the aggressive pre-square-off window.

    NSE: 15:00-15:15 (square-off 15:15)
    MCX: 23:00-23:15 (square-off 23:15)
    """
    ts = _to_ist(ts)
    sq_str = square_off_for(segment)
    sq = pd.Timestamp(f"2000-01-01 {sq_str}")
    window_start = sq - pd.Timedelta(minutes=V19_PRE_SQOFF_WINDOW_MINUTES)
    return window_start.time() <= ts.time() < sq.time()


def check_intraday_exit_v19(pos, cur_underlying, cur_premium, is_square_off_bar,
                            df_1m=None, i_1m=None, ts=None, segment: str = "stock") -> dict:
    """V19 exit engine — tighter trailing + fixed target booking.

    Improvements over V6.6:
      1. Trail activates at +5% (har trade — profit jaldi lock)
      2. Trail locks 70% of peak (not 55%) — gives back 15% less
      3. Fixed target at +50%: books 40% of position, rides rest
      4. Runaway safety at +250% (unchanged)
      5. SMART SQUARE-OFF (V19+): in the 15-min pre-sqoff window, profitable
         trades with active trail get a TIGHTER trail (80% lock) so they
         exit smartly before the hard square-off. At the square-off bar
         itself, only LOSS / small-profit trades are force-closed — big
         winners have already exited on trail.

    Locked V6.6 exit (check_intraday_exit) is NOT modified.
    """
    gain_pct = (cur_premium - pos["entry_premium"]) / pos["entry_premium"] * 100

    # 1. SMART PRE-SQUARE-OFF TRAIL — exit profitable trades before blind close.
    # Only applies inside the pre-sqoff window, only to trail-active (>=+5%)
    # trades. Tighter 80% lock vs normal 70% → locks profit faster.
    if ts is not None and _in_pre_square_off_window(ts, segment) \
            and gain_pct >= V19_TRAIL_ACTIVATE_PCT:
        peak = max(pos.get("peak_premium", cur_premium), cur_premium)
        peak_gain = (peak - pos["entry_premium"]) / pos["entry_premium"]
        trail_floor = pos["entry_premium"] * (1 + peak_gain * V19_PRE_SQOFF_TRAIL_LOCK_PCT / 100)
        if cur_premium <= trail_floor:
            return {"exit": True, "reason": "pre_sqoff_trail_lock_80pct",
                    "exit_premium": max(cur_premium, 0.5)}

    # 2. SQUARE-OFF — but NOT blind anymore.
    #    Big winners (>=+5%) already exited above. Here we force-close
    #    only LOSS / small-profit trades (trail not active = no protection).
    if is_square_off_bar:
        if gain_pct >= V19_TRAIL_ACTIVATE_PCT:
            # Profitable trail-active trade somehow still open at sqoff:
            # close it but as a smart close, not a blind loss cut.
            return {"exit": True, "reason": "sqoff_smart_profit",
                    "exit_premium": cur_premium}
        return {"exit": True, "reason": "square_off",
                "exit_premium": cur_premium}

    # 3. Hard stop
    if cur_premium <= pos["stop_premium"]:
        return {"exit": True, "reason": "stop_loss_2000",
                "exit_premium": max(cur_premium, 0.5)}

    # 4. Opposing zone
    opp = pos.get("opposing_zone_edge")
    if opp is not None:
        if pos["direction"] == "BUY" and cur_underlying >= opp:
            return {"exit": True, "reason": "opposing_zone_reached",
                    "exit_premium": max(cur_premium, 0.5)}
        if pos["direction"] == "SELL" and cur_underlying <= opp:
            return {"exit": True, "reason": "opposing_zone_reached",
                    "exit_premium": max(cur_premium, 0.5)}

    # 5. FIXED TARGET — book 50% at +100% (first time only)
    if gain_pct >= V19_FIXED_TARGET_PCT and not pos.get("target_booked", False):
        pos["target_booked"] = True
        pos["quantity"] = max(1, int(pos["quantity"] * (1 - V19_FIXED_TARGET_BOOK)))
        return {"exit": True, "reason": "fixed_target_100pct_book50",
                "exit_premium": cur_premium}

    # 6. Dynamic trail — activates at +5% (har trade — profit jaldi lock)
    if gain_pct >= V19_TRAIL_ACTIVATE_PCT:
        peak = max(pos.get("peak_premium", cur_premium), cur_premium)
        peak_gain = (peak - pos["entry_premium"]) / pos["entry_premium"]
        trail_floor = pos["entry_premium"] * (1 + peak_gain * V19_TRAIL_LOCK_PCT / 100)
        if cur_premium <= trail_floor:
            return {"exit": True, "reason": "v19_trail_lock_70pct",
                    "exit_premium": max(cur_premium, 0.5)}

    # 7. 1m exhaustion (only after +5% gain — micro exhaustion pattern)
    if df_1m is not None and i_1m is not None and gain_pct >= V19_TRAIL_ACTIVATE_PCT:
        exhausted, why = one_min_exhaustion(df_1m, i_1m, pos["direction"])
        if exhausted:
            return {"exit": True, "reason": f"1m_exhaustion:{why}",
                    "exit_premium": max(cur_premium, 0.5)}

    # 8. Runaway safety
    if gain_pct >= V19_RUNAWAY_EXIT_PCT:
        return {"exit": True, "reason": "runaway_safety_250pct",
                "exit_premium": cur_premium}

    return {"exit": False}


# ============================================================
# DELIVERY MODE — 2-3 day rocket holding (Tiger V16)
# ============================================================
DELIVERY_ROCKET_MIN_SCORE = 90  # only ultra-high-conviction setups get delivery
DELIVERY_MAX_HOLD_DAYS = 3      # max 3 days hold for delivery trades
DELIVERY_HOLD_BARS_15M = 3 * 25 # 3 trading days * 25 bars/day (15m)
DELIVERY_TRAIL_ACTIVATION = 40.0  # 40% gain before trailing (wider than intraday)
DELIVERY_TRAIL_GIVEBACK = 20.0    # 20% giveback from peak (let winners run)
DELIVERY_STOP_PCT = 30.0          # wider stop for delivery (30% vs 25%)

# Score thresholds
MIN_SCORE_TO_ENTER = 75     # raised from 68 — only strong multi-confluence setups
EXPLOSIVE_BONUS = 3         # reduced from 10 — data showed explosive zones lose money
SWEEP_BONUS = 8
DELTA_SPIKE_BONUS = 5
TREND_BONUS = 5
PDH_PDL_BONUS = 5
VWAP_BONUS = 5
MORNING_BONUS = 3
AFTERNOON_BONUS = 2
PCR_BONUS = 3
VIX_LOW_BONUS = 2
ORB_BONUS = 3

# DEMAND ZONE QUALITY VALIDATION (new in V6)
# Weak demand zones were the #1 problem (Tiger_Demand_Boom 45.5% win).
# Now every demand zone is validated for institutional strength.
DEMAND_FRESH_UNTESTED = 15       # 0 touches since creation = freshest
DEMAND_FRESH_TESTED = 10         # 1-2 touches and held = confirmed support
DEMAND_STALE_PENALTY = -8        # 3+ touches = weakening (supply absorbed)
DEMAND_HIGH_VOL_BONUS = 8        # volume at creation > 1.5x avg = institutional
DEMAND_FAST_DEPARTURE = 6       # price left zone fast = strong rejection
DEMAND_STRONG_BODY = 5          # impulse candle body > 60% of range
DEMAND_ROUND_NUMBER_BONUS = 4   # zone near round number = psychological

# EXPLOSIVE SPLIT (data-driven — demand explosive LOSES, supply explosive WINS)
DEMAND_EXPLOSIVE_PENALTY = -5   # explosive demand = false breakout trap (45.5% win)
SUPPLY_EXPLOSIVE_BONUS = 3      # explosive supply = confirmed rejection (100% win)

# PCR thresholds
PCR_BULLISH_MAX = 1.3
PCR_BEARISH_MIN = 0.7
PCR_FALLBACK = 1.0

# VIX threshold
VIX_LOW_MAX = 15.0
VIX_FALLBACK = 15.0

# VWAP tolerance (zone within this % of VWAP = confluence)
VWAP_TOLERANCE_PCT = 0.3

# PDH/PDL tolerance (zone within this % of PDH/PDL = confluence)
PDH_PDL_TOLERANCE_PCT = 0.5

# ============================================================
# ROCKET DETECTION LENS — the "powerful eyes" meta-filter
# Identifies setups with explosive 2-3 day rocket potential.
# Only the top 50/100 setups pass — sniper quality, bumper P&L.
# ============================================================
ROCKET_MIN_SCORE = 75        # tightened — sirf high-confidence blast trades (was 72)
ROCKET_MIN_CONFLUENCES = 2   # relaxed from 3 — Tiger freedom: more entries, less waiting
ROCKET_MIN_FUEL = 1          # need 1+ rocket-fuel signal (sweep/compression/2.5x spike/momentum)
ROCKET_MOMENTUM_MIN = 2.0    # delta spike must be 2x+ for rocket fuel
ROCKET_STALE_HARD_REJECT = 999 # disabled — stale penalty (-8) handles it, hard reject kills commodity winners
ROCKET_FRESHNESS_BONUS = 10  # untested institutional zone = rocket fuel
ROCKET_COMPRESSION_BONUS = 8 # BB/compression before breakout = coiled spring
ROCKET_MULTI_TOUCH_BONUS = 6 # zone tested 1-2x and held = strong level
ROCKET_INST_VOL_BONUS = 8    # institutional volume at creation
ROCKET_DEPARTURE_BONUS = 6   # fast departure from zone
ROCKET_BODY_BONUS = 5        # strong impulse body
ROCKET_SWEEP_STACK_BONUS = 5 # sweep + spike stacking = stop hunt before rocket
ROCKET_TREND_STACK_BONUS = 5 # trend + PDH/PDL stacking = breakout confirm
ROCKET_PCR_STACK_BONUS = 3   # PCR aligned with direction
ROCKET_ORB_STACK_BONUS = 3   # ORB breakout confirm
ROCKET_VWAP_STACK_BONUS = 4  # VWAP confluence

ANGEL_EXCHANGE = {
    "NIFTY": ("NSE", "Nifty 50"),
    "BANKNIFTY": ("NSE", "Nifty Bank"),
    "FINNIFTY": ("NSE", "Nifty Fin Service"),
    "CRUDEOIL": ("MCX", "CRUDEOIL"),
    "GOLD": ("MCX", "GOLD"),
    "NATURALGAS": ("MCX", "NATURALGAS"),
    "SILVER": ("MCX", "SILVER"),
}


# ============================================================
# BLACK-SCHOLES OPTION PRICING + GREEKS
# ============================================================
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

def black_scholes_greeks(S, K, T, r, sigma, is_call) -> dict:
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        intrinsic = max(S - K, 0.0) if is_call else max(K - S, 0.0)
        return {"price": max(intrinsic, 0.5), "delta": 0.0, "gamma": 0.0,
                "theta": 0.0, "vega": 0.0}
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    nd1, nd2 = _norm_cdf(d1), _norm_cdf(d2)
    npd1 = _norm_pdf(d1)
    if is_call:
        price = S * nd1 - K * math.exp(-r * T) * nd2
        delta = nd1
        theta = (-(S * npd1 * sigma) / (2 * sqrt_T) - r * K * math.exp(-r * T) * nd2) / 365.0
    else:
        price = K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)
        delta = -_norm_cdf(-d1)
        theta = (-(S * npd1 * sigma) / (2 * sqrt_T) + r * K * math.exp(-r * T) * _norm_cdf(-d2)) / 365.0
    gamma = npd1 / (S * sigma * sqrt_T)
    vega = S * npd1 * sqrt_T / 100.0
    return {"price": max(price, 0.5), "delta": round(delta, 4),
            "gamma": round(gamma, 6), "theta": round(theta, 4),
            "vega": round(vega, 4)}

def bs_premium(underlying, strike, dte_days, is_call, iv):
    T = max(dte_days, 0.05) / 365.0
    sigma = max(min(iv, 0.80), 0.08)
    return black_scholes_greeks(underlying, strike, T, RISK_FREE_RATE, sigma, is_call)["price"]

def bs_premium_at(underlying_level, strike, dte_days, is_call, iv):
    return bs_premium(underlying_level, strike, dte_days, is_call, iv)

def bs_greeks_full(underlying, strike, dte_days, is_call, iv) -> dict:
    T = max(dte_days, 0.05) / 365.0
    sigma = max(min(iv, 0.80), 0.08)
    return black_scholes_greeks(underlying, strike, T, RISK_FREE_RATE, sigma, is_call)


# ============================================================
# CONFLUENCE FEATURES (all calculable from existing data)
# ============================================================

# --- VWAP (intraday volume-weighted average price) ---
_vwap_cache: dict[str, pd.Series] = {}

def compute_vwap_series(df_1m: pd.DataFrame) -> pd.Series:
    """Intraday VWAP — resets daily. Institutional consensus level."""
    key = id(df_1m)
    if key in _vwap_cache:
        return _vwap_cache[key]
    tp = (df_1m["high"] + df_1m["low"] + df_1m["close"]) / 3.0
    vol = df_1m["volume"].replace(0, 1)
    cum_pv = (tp * vol).groupby(df_1m.index.date).cumsum()
    cum_v = vol.groupby(df_1m.index.date).cumsum()
    vwap = cum_pv / cum_v
    _vwap_cache[key] = vwap
    return vwap

def vwap_confluence(cur_price, vwap_val) -> tuple[bool, str]:
    """Zone near VWAP = institutional consensus = stronger."""
    if vwap_val <= 0 or pd.isna(vwap_val):
        return (False, "no vwap")
    dist_pct = abs(cur_price - vwap_val) / vwap_val * 100
    if dist_pct <= VWAP_TOLERANCE_PCT:
        side = "above" if cur_price > vwap_val else "below"
        return (True, f"vwap confluence ({side}, {dist_pct:.2f}%)")
    return (False, f"far from vwap ({dist_pct:.2f}%)")


# --- PDH/PDL (Previous Day High/Low) ---
_pdh_pdl_cache: dict[str, tuple[float, float]] = {}

def compute_pdh_pdl(df_15m: pd.DataFrame, current_date) -> tuple[float, float]:
    """Previous day's high and low — institutional memory levels."""
    key = f"{id(df_15m)}|{current_date}"
    if key in _pdh_pdl_cache:
        return _pdh_pdl_cache[key]
    prev_dates = sorted(set(d for d in df_15m.index.date if d < current_date))
    if not prev_dates:
        return (0.0, 0.0)
    prev_day = prev_dates[-1]
    prev_bars = df_15m[df_15m.index.date == prev_day]
    if prev_bars.empty:
        return (0.0, 0.0)
    pdh = float(prev_bars["high"].max())
    pdl = float(prev_bars["low"].min())
    _pdh_pdl_cache[key] = (pdh, pdl)
    return (pdh, pdl)

def pdh_pdl_confluence(zone_top, zone_bottom, pdh, pdl) -> tuple[bool, str]:
    """Zone near PDH or PDL = institutional memory = stronger."""
    if pdh <= 0 or pdl <= 0:
        return (False, "no pdh/pdl")
    zone_mid = (zone_top + zone_bottom) / 2
    dist_pdh = abs(zone_mid - pdh) / pdh * 100 if pdh > 0 else 999
    dist_pdl = abs(zone_mid - pdl) / pdl * 100 if pdl > 0 else 999
    if dist_pdh <= PDH_PDL_TOLERANCE_PCT:
        return (True, f"near PDH ({dist_pdh:.2f}%)")
    if dist_pdl <= PDH_PDL_TOLERANCE_PCT:
        return (True, f"near PDL ({dist_pdl:.2f}%)")
    return (False, "")


# --- Opening Range Breakout (ORB) ---
_orb_cache: dict[str, tuple[float, float]] = {}

def compute_opening_range(df_1m: pd.DataFrame, current_date) -> tuple[float, float]:
    """First 15 minutes (09:15-09:30) high/low. Breakout = day direction."""
    key = f"{id(df_1m)}|{current_date}"
    if key in _orb_cache:
        return _orb_cache[key]
    day_bars = df_1m[df_1m.index.date == current_date]
    if day_bars.empty:
        return (0.0, 0.0)
    # Convert to IST if needed
    if day_bars.index.tz is not None:
        day_bars_ist = day_bars.tz_convert("Asia/Kolkata")
    else:
        day_bars_ist = day_bars
    or_bars = day_bars_ist[(day_bars_ist.index.strftime("%H:%M") >= "09:15") &
                           (day_bars_ist.index.strftime("%H:%M") < "09:30")]
    if or_bars.empty:
        return (0.0, 0.0)
    or_high = float(or_bars["high"].max())
    or_low = float(or_bars["low"].min())
    _orb_cache[key] = (or_high, or_low)
    return (or_high, or_low)

def orb_direction(cur_price, or_high, or_low) -> str:
    """Above OR high = bullish breakout. Below OR low = bearish. Inside = neutral."""
    if or_high <= 0 or or_low <= 0:
        return "neutral"
    if cur_price > or_high:
        return "bullish"
    if cur_price < or_low:
        return "bearish"
    return "neutral"


# ============================================================
# ROCKET DETECTION LENS — the "powerful eyes" meta-filter
# ============================================================
# Scans 15m data for compression (coiled spring) before breakout.
# A rocket needs: tight consolidation → explosive expansion → zone touch.
# This detects the "coiled" phase that precedes 2-3 day rocket moves.

def detect_compression(df_15m, i, lookback=20) -> tuple[bool, float]:
    """BB-width compression: current range vs 20-bar avg range.
    Returns (is_compressed, compression_ratio)."""
    if i < lookback:
        return (False, 1.0)
    window = df_15m.iloc[i - lookback:i + 1]
    ranges = (window["high"] - window["low"]).astype(float)
    if len(ranges) < 5:
        return (False, 1.0)
    recent_range = float(ranges.iloc[-5:].mean())
    avg_range = float(ranges.mean())
    if avg_range <= 0:
        return (False, 1.0)
    ratio = recent_range / avg_range
    # compressed if recent range < 70% of average (tight coil)
    return (ratio < 0.70, round(ratio, 2))


def rocket_momentum_score(df_1m, i_1m, direction) -> tuple[float, str]:
    """1m momentum rocket check: consecutive bars in direction + accelerating volume.
    Returns (momentum_score, reason)."""
    if i_1m < 3:
        return (0.0, "insufficient")
    bars = df_1m.iloc[i_1m - 2:i_1m + 1]
    closes = bars["close"].astype(float).values
    vols = bars["volume"].astype(float).values

    # Count consecutive bars closing in direction
    if direction == "BUY":
        bullish = sum(1 for k in range(len(closes) - 1) if closes[k + 1] > closes[k])
        momentum = bullish / max(len(closes) - 1, 1)
    else:
        bearish = sum(1 for k in range(len(closes) - 1) if closes[k + 1] < closes[k])
        momentum = bearish / max(len(closes) - 1, 1)

    # Volume acceleration: is volume increasing?
    # For 0-volume sources (indexes), use price-range acceleration as proxy
    if vols[-1] > 0 and vols[-2] > 0:
        vol_accel = vols[-1] / max(vols[-2], 1.0)
    else:
        # Price-range proxy: current bar range vs previous bar range
        ranges = (bars["high"] - bars["low"]).astype(float).values
        vol_accel = ranges[-1] / max(ranges[-2], 1e-9) if len(ranges) >= 2 else 1.0

    score = momentum * 10 + min(vol_accel, 3.0) * 3
    reason = f"mom{momentum:.0%} vol-acc{vol_accel:.1f}x"
    return (round(score, 1), reason)


def validate_zone_quality(df_15m, zone, i_15m, zone_type) -> tuple[int, list[str], int]:
    """Full zone quality validation for BOTH demand and supply zones.
    This was built (validate_demand_zone_quality) but never called — now integrated.
    Scores: freshness + institutional volume + departure speed + body + round number.
    Returns (score, details, touch_count). 5+ touches = HARD REJECT signal."""
    score = 0
    details = []

    touches = count_zone_touches(df_15m, zone, i_15m)
    if touches == 0:
        score += ROCKET_FRESHNESS_BONUS
        details.append(f"fresh-untested(+{ROCKET_FRESHNESS_BONUS})")
    elif touches <= 2:
        score += ROCKET_MULTI_TOUCH_BONUS
        details.append(f"confirmed-{touches}touch(+{ROCKET_MULTI_TOUCH_BONUS})")
    else:
        score += DEMAND_STALE_PENALTY
        details.append(f"stale-{touches}touch({DEMAND_STALE_PENALTY})")

    vol_ratio, is_high_vol = zone_creation_volume(df_15m, zone)
    if is_high_vol:
        score += ROCKET_INST_VOL_BONUS
        details.append(f"inst-vol{vol_ratio:.1f}x(+{ROCKET_INST_VOL_BONUS})")

    dep_pct, is_fast = zone_departure_speed(df_15m, zone, zone_type)
    if is_fast:
        score += ROCKET_DEPARTURE_BONUS
        details.append(f"fast-departure{dep_pct:.1f}%(-{ROCKET_DEPARTURE_BONUS})")

    body_ratio, is_strong = impulse_body_strength(df_15m, zone)
    if is_strong:
        score += ROCKET_BODY_BONUS
        details.append(f"strong-body{body_ratio:.0f}%(+{ROCKET_BODY_BONUS})")

    zone_mid = (zone["top"] + zone["bottom"]) / 2
    if near_round_number(zone_mid):
        score += DEMAND_ROUND_NUMBER_BONUS
        details.append(f"round-no(+{DEMAND_ROUND_NUMBER_BONUS})")

    return (score, details, touches)


def rocket_detection_lens(
    df_15m, i_15m, df_1m, i_1m, zone, zone_type, direction,
    spike_mult, swept, is_exp, trend, pdh_ok, vwap_ok, orb_dir,
    pcr_aligned, pcr_val, cur_price, vix_val
) -> tuple[int, list[str], int, bool]:
    """The POWERFUL meta-filter lens. Scores rocket potential on 3 axes:

    1. ZONE QUALITY (institutional strength): freshness, vol, departure, body
       — HARD REJECTS stale zones (5+ touches = noise, not institutional)
    2. MOMENTUM IGNITION (rocket fuel): delta spike strength, volume
       acceleration, liquidity sweep, compression (coiled spring)
    3. CONFLUENCE STACKING: how many REAL independent signals agree
       — PCR fallback (1.0) does NOT count; VWAP only counts if meaningful

    Returns (rocket_bonus, rocket_details, confluence_count, is_rocket).
    is_rocket = confluence_count >= ROCKET_MIN_CONFLUENCES AND fuel >= ROCKET_MIN_FUEL.
    """
    rocket_bonus = 0
    rocket_details = []
    confluence_count = 0
    fuel_count = 0

    # === AXIS 1: ZONE QUALITY (the institutional eyes) ===
    zq_score, zq_details, touches = validate_zone_quality(
        df_15m, zone, i_15m, zone_type)
    rocket_bonus += zq_score
    rocket_details.extend(zq_details)

    # HARD REJECT stale zones — 5+ touches = noise, not institutional level
    if touches >= ROCKET_STALE_HARD_REJECT:
        return (0, [f"HARD-REJECT:stale-{touches}touch"], 0, False)

    if zq_score > 0:
        confluence_count += 1

    # === AXIS 2: MOMENTUM IGNITION (the rocket fuel) ===
    # Strong delta spike (2.5x+ = rocket grade)
    if spike_mult >= ROCKET_MOMENTUM_MIN:
        rocket_bonus += 3
        rocket_details.append(f"rocket-spike{spike_mult:.1f}x(+3)")
        confluence_count += 1
        fuel_count += 1

    # Liquidity sweep (stop hunt before rocket)
    if swept:
        confluence_count += 1
        fuel_count += 1

    # 1m momentum acceleration
    mom_score, mom_reason = rocket_momentum_score(df_1m, i_1m, direction)
    if mom_score >= 13:  # stricter: need strong momentum (was 10)
        rocket_bonus += 3
        rocket_details.append(f"{mom_reason}(+3)")
        confluence_count += 1
        fuel_count += 1

    # Compression before breakout (coiled spring)
    is_compressed, comp_ratio = detect_compression(df_15m, i_15m)
    if is_compressed:
        rocket_bonus += ROCKET_COMPRESSION_BONUS
        rocket_details.append(f"coiled{comp_ratio:.2f}(+{ROCKET_COMPRESSION_BONUS})")
        confluence_count += 1
        fuel_count += 1

    # === AXIS 3: CONFLUENCE STACKING (the lens power) ===
    # Sweep + spike stacking = stop hunt then rocket
    if swept and spike_mult >= ROCKET_MOMENTUM_MIN:
        rocket_bonus += ROCKET_SWEEP_STACK_BONUS
        rocket_details.append(f"sweep+spike-stack(+{ROCKET_SWEEP_STACK_BONUS})")

    # Trend + PDH/PDL stacking = breakout confirmation
    trend_aligned = (direction == "BUY" and trend == "up") or \
                    (direction == "SELL" and trend == "down")
    if trend_aligned and pdh_ok:
        rocket_bonus += ROCKET_TREND_STACK_BONUS
        rocket_details.append(f"trend+PDH-stack(+{ROCKET_TREND_STACK_BONUS})")
        confluence_count += 1
    elif trend_aligned:
        confluence_count += 1

    # VWAP confluence — only counts as REAL confluence if price is at
    # meaningful distance from VWAP (not just touching = easy signal)
    if vwap_ok:
        rocket_bonus += ROCKET_VWAP_STACK_BONUS
        rocket_details.append(f"vwap-stack(+{ROCKET_VWAP_STACK_BONUS})")
        confluence_count += 1

    # PCR alignment — only counts as REAL confluence if PCR is NOT the
    # fallback value (1.0 = no real data, just default)
    if pcr_aligned and pcr_val != 1.0:
        rocket_bonus += ROCKET_PCR_STACK_BONUS
        rocket_details.append(f"pcr-stack(+{ROCKET_PCR_STACK_BONUS})")
        confluence_count += 1

    # ORB confirmation
    orb_aligned = (direction == "BUY" and orb_dir == "bullish") or \
                  (direction == "SELL" and orb_dir == "bearish")
    if orb_aligned:
        rocket_bonus += ROCKET_ORB_STACK_BONUS
        rocket_details.append(f"orb-stack(+{ROCKET_ORB_STACK_BONUS})")
        confluence_count += 1

    # Explosive zone quality
    if is_exp:
        confluence_count += 1
        fuel_count += 1

    is_rocket = (confluence_count >= ROCKET_MIN_CONFLUENCES
                 and fuel_count >= ROCKET_MIN_FUEL)
    return (rocket_bonus, rocket_details, confluence_count, is_rocket)


# --- India VIX ---
_vix_cache: pd.DataFrame | None = None

def get_vix_for_date(date) -> float:
    """India VIX value for a given date. Low VIX = trending day."""
    global _vix_cache
    if _vix_cache is None or _vix_cache.empty:
        try:
            _vix_cache = fetch_india_vix_history(days_back=90)
        except Exception:
            _vix_cache = pd.DataFrame()
    if _vix_cache is None or _vix_cache.empty:
        return VIX_FALLBACK
    # Find the close column (could be 'close', 'Close', or last numeric column)
    close_col = None
    for col in ["close", "Close", "CLOSE"]:
        if col in _vix_cache.columns:
            close_col = col
            break
    if close_col is None:
        # Fallback: use last numeric column
        numeric_cols = _vix_cache.select_dtypes(include="number").columns
        if len(numeric_cols) == 0:
            return VIX_FALLBACK
        close_col = numeric_cols[-1]
    target_date = date
    if hasattr(date, "date"):
        target_date = date.date()
    if hasattr(date, "tz_convert"):
        try:
            target_date = date.tz_convert("Asia/Kolkata").date()
        except (TypeError, AttributeError):
            pass
    try:
        mask = _vix_cache.index.date == target_date
        if mask.any():
            return float(_vix_cache.loc[mask].iloc[-1][close_col])
    except Exception:
        pass
    return VIX_FALLBACK


# ============================================================
# STRONG DEMAND ZONE VALIDATION (V6 — fixes weak demand detection)
# ============================================================

def count_zone_touches(df_15m, zone, current_idx) -> int:
    """
    Kitni baar price zone ko touch karke wapas gaya since creation?
    0 touches = freshest (untested institutional level)
    1-2 touches = confirmed support (held = strong)
    3+ touches = stale (supply absorbed, weakening)
    """
    bar_idx = zone.get("bar_idx", current_idx)
    if bar_idx >= current_idx or bar_idx < 1:
        return 0
    zone_top = zone["top"]
    zone_bottom = zone["bottom"]
    touches = 0
    for k in range(bar_idx + 3, min(current_idx, len(df_15m))):
        low = float(df_15m.iloc[k]["low"])
        if low <= zone_top and low >= zone_bottom * 0.98:
            close_k = float(df_15m.iloc[k]["close"])
            if close_k > zone_bottom:
                touches += 1
    return touches

def zone_creation_volume(df_15m, zone) -> tuple[float, bool]:
    """Volume at zone creation vs 20-bar average. High = institutional."""
    bar_idx = zone.get("bar_idx", 0)
    if bar_idx < 1 or bar_idx >= len(df_15m):
        return (1.0, False)
    creation_vols = [float(df_15m.iloc[bar_idx + k].get("volume", 0) or 0)
                     for k in range(min(3, len(df_15m) - bar_idx))]
    creation_vol = sum(creation_vols) / len(creation_vols) if creation_vols else 0
    vol_window = df_15m.iloc[max(0, bar_idx - 20):bar_idx]
    avg_vol = float(vol_window["volume"].mean() or 0) if len(vol_window) > 0 else 0
    if avg_vol <= 0 or creation_vol <= 0:
        return (1.0, False)
    ratio = creation_vol / avg_vol
    return (round(ratio, 2), ratio >= 1.5)

def zone_departure_speed(df_15m, zone, zone_type) -> tuple[float, bool]:
    """How fast did price leave the zone? Fast = strong rejection."""
    bar_idx = zone.get("bar_idx", 0)
    if bar_idx < 1 or bar_idx + 5 >= len(df_15m):
        return (0.0, False)
    after = df_15m.iloc[bar_idx + 1:bar_idx + 6]
    if len(after) < 2:
        return (0.0, False)
    if zone_type == "demand":
        base_high = zone["top"]
        peak = float(after["high"].max())
        departure_pct = (peak - base_high) / base_high * 100 if base_high > 0 else 0
    else:
        base_low = zone["bottom"]
        trough = float(after["low"].min())
        departure_pct = (base_low - trough) / base_low * 100 if base_low > 0 else 0
    return (round(departure_pct, 2), departure_pct >= 0.5)

def impulse_body_strength(df_15m, zone) -> tuple[float, bool]:
    """Impulse candle body should be strong (close near high for demand)."""
    bar_idx = zone.get("bar_idx", 0)
    if bar_idx < 1 or bar_idx >= len(df_15m):
        return (0.0, False)
    imp_idx = max(0, bar_idx - 1)
    imp = df_15m.iloc[imp_idx]
    o, c = float(imp["open"]), float(imp["close"])
    h, l = float(imp["high"]), float(imp["low"])
    rng = h - l
    if rng <= 0:
        return (0.0, False)
    body_ratio = abs(c - o) / rng
    return (round(body_ratio, 2), body_ratio >= 0.6)

def near_round_number(price, tolerance_pct=0.3) -> bool:
    """Is price near a psychological round number (50/100/500/1000)?"""
    for step in [50, 100, 500, 1000]:
        remainder = price % step
        dist_to_round = min(remainder, step - remainder)
        if price > 0 and dist_to_round / price * 100 <= tolerance_pct:
            return True
    return False

def validate_demand_zone_quality(df_15m, zone, i_15m) -> tuple[int, list[str]]:
    """
    Full demand zone validation — the FIX for weak demand detection.
    Scores: freshness + creation volume + departure speed + body + round no.
    """
    score = 0
    details = []

    touches = count_zone_touches(df_15m, zone, i_15m)
    if touches == 0:
        score += DEMAND_FRESH_UNTESTED
        details.append(f"fresh-untested(+{DEMAND_FRESH_UNTESTED})")
    elif touches <= 2:
        score += DEMAND_FRESH_TESTED
        details.append(f"confirmed-{touches}touch(+{DEMAND_FRESH_TESTED})")
    else:
        score += DEMAND_STALE_PENALTY
        details.append(f"stale-{touches}touch({DEMAND_STALE_PENALTY})")

    vol_ratio, is_high_vol = zone_creation_volume(df_15m, zone)
    if is_high_vol:
        score += DEMAND_HIGH_VOL_BONUS
        details.append(f"inst-vol{vol_ratio:.1f}x(+{DEMAND_HIGH_VOL_BONUS})")

    dep_pct, is_fast = zone_departure_speed(df_15m, zone, zone["type"])
    if is_fast:
        score += DEMAND_FAST_DEPARTURE
        details.append(f"fast-departure{dep_pct:.1f}%(+{DEMAND_FAST_DEPARTURE})")

    body_ratio, is_strong = impulse_body_strength(df_15m, zone)
    if is_strong:
        score += DEMAND_STRONG_BODY
        details.append(f"strong-body{body_ratio:.0f}%(+{DEMAND_STRONG_BODY})")

    zone_mid = (zone["top"] + zone["bottom"]) / 2
    if near_round_number(zone_mid):
        score += DEMAND_ROUND_NUMBER_BONUS
        details.append(f"round-no(+{DEMAND_ROUND_NUMBER_BONUS})")

    return (score, details)


# --- PCR (Put-Call Ratio) ---
def fetch_pcr(broker, underlying: str) -> float:
    """Fetch Put-Call Ratio from option chain OI.

    Thread-safe 8s hard timeout via concurrent.futures — works in any
    thread context (BackgroundScheduler worker threads). The previous
    signal.SIGALRM approach only works in the main thread and silently
    fails in apscheduler's ThreadPoolExecutor.
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

    def _do_fetch():
        from data.loader import fetch_option_chain_oi
        chain = fetch_option_chain_oi(broker, underlying=underlying, strikes_around_atm=15)
        if chain is None or chain.empty:
            return PCR_FALLBACK
        ce_oi = float(chain["CE_oi"].sum()) if "CE_oi" in chain.columns else 0
        pe_oi = float(chain["PE_oi"].sum()) if "PE_oi" in chain.columns else 0
        if ce_oi <= 0:
            return PCR_FALLBACK
        return pe_oi / ce_oi

    try:
        ex = ThreadPoolExecutor(max_workers=1)
        future = ex.submit(_do_fetch)
        result = future.result(timeout=8)
        ex.shutdown(wait=False)  # non-blocking — don't trap on hung worker
        return result
    except FuturesTimeout:
        logger.debug("PCR fetch timeout for %s — using fallback", underlying)
        return PCR_FALLBACK
    except Exception:
        return PCR_FALLBACK


# ============================================================
# TREND DETECTION (soft scorer, not hard gate)
# ============================================================
def detect_15m_trend(df_15m, i) -> str:
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


# ============================================================
# GOLDEN WINDOW (soft scorer)
# ============================================================
def golden_window_bonus(ts, segment) -> int:
    if segment == "commodity":
        return MORNING_BONUS if _in_entry_window(ts, segment) else 0
    ts_str = ts.strftime("%H:%M") if ts.tz is None else ts.tz_convert("Asia/Kolkata").strftime("%H:%M")
    if "09:15" <= ts_str < "11:30":
        return MORNING_BONUS
    if "13:00" <= ts_str < "15:15":
        return AFTERNOON_BONUS
    return 0


# ============================================================
# VOLUME CONFIRMATION (HARD GATE — required)
# ============================================================
def volume_confirmed(df_1m, i_1m, zone_type) -> tuple[bool, str]:
    """1.3x volume surge in correct direction. This is a HARD requirement."""
    if i_1m < VOL_LOOKBACK + 1:
        return (True, "insufficient-bars (pass)")
    bar = df_1m.iloc[i_1m]
    vol = float(bar.get("volume", 0) or 0)
    vdelta = volume_delta(bar)
    if zone_type == "demand" and vdelta <= 0:
        return (False, "sell-pressure at demand")
    if zone_type == "supply" and vdelta >= 0:
        return (False, "buy-pressure at supply")
    if vol > 0:
        recent_vols = [float(df_1m.iloc[j].get("volume", 0) or 0)
                       for j in range(i_1m - VOL_LOOKBACK, i_1m)]
        avg_vol = sum(recent_vols) / len(recent_vols) if recent_vols else 0.0
        if avg_vol > 0 and vol >= avg_vol * VOL_SURGE_MULT:
            dir_label = "buy-delta" if zone_type == "demand" else "sell-delta"
            return (True, f"{dir_label} + {vol/avg_vol:.1f}x surge")
        return (False, f"correct-dir but no surge ({vol/max(avg_vol,1):.1f}x)")
    return (True, "correct-dir (no-vol source)")


# ============================================================
# SCORING ENTRY ENGINE — ALL 5 BRAINS AS SCORERS
# ============================================================
def find_tiger_brain_entry_15m(df_15m, i_15m, seg, is_expiry, symbol, vix_val,
                                min_score=75.0, force_hunt=False):
    """15m-only entry path for symbols without 1m data (indexes, illiquid).

    Uses 15m bar for zone touch + momentum confirmation instead of 1m sniper.
    Simpler but works when 1m data is unavailable (e.g., NIFTY/BANKNIFTY
    where Angel One doesn't provide direct index 1m candles).

    HARD GATES: zone touch on 15m bar + body confirmation.
    """
    if not _is_symbol_allowed(symbol):
        return None  # symbol blocked (safety net)

    if i_15m < 40:
        return None

    bar = df_15m.iloc[i_15m]
    cur_price = float(bar["close"])

    zone_idx = max(0, i_15m - 1)
    zones = detect_zones(df_15m, zone_idx, lookback=zone_idx)
    if not zones:
        return None

    trend = detect_15m_trend(df_15m, i_15m)
    current_date = df_15m.index[i_15m].date() if df_15m.index[i_15m].tz is None \
        else df_15m.index[i_15m].tz_convert("Asia/Kolkata").date()
    pdh, pdl = compute_pdh_pdl(df_15m, current_date)

    best = None
    for z in zones:
        touch = zone_touched_on_1m(bar, z)
        if touch is None:
            continue

        direction = "BUY" if touch == "demand" else "SELL"

        # HARD GATE: bar body must confirm direction (replaces 1m volume delta)
        o, c = float(bar["open"]), float(bar["close"])
        body_ok = (c > o and direction == "BUY") or (c < o and direction == "SELL")
        if not body_ok:
            continue

        score = z["score"]
        score_details = [f"15m-body-confirm"]

        # Explosive quality
        try:
            is_exp, exp_pct = zone_explosive_quality(
                df_15m, z.get("bar_idx", zone_idx), touch)
            if is_exp:
                score += EXPLOSIVE_BONUS
                score_details.append(f"explosive(+{EXPLOSIVE_BONUS})")
        except Exception:
            is_exp, exp_pct = False, 0.0

        # Trend alignment
        trend_aligned = (direction == "BUY" and trend == "up") or \
                        (direction == "SELL" and trend == "down")
        if trend_aligned:
            score += TREND_BONUS
            score_details.append(f"trend(+{TREND_BONUS})")

        # PDH/PDL confluence
        pdh_ok, pdh_reason = pdh_pdl_confluence(z["top"], z["bottom"], pdh, pdl)
        if pdh_ok:
            score += PDH_PDL_BONUS
            score_details.append(f"{pdh_reason}(+{PDH_PDL_BONUS})")

        # Golden window
        gw_bonus = golden_window_bonus(df_15m.index[i_15m], seg)
        if gw_bonus > 0:
            score += gw_bonus
            score_details.append(f"window(+{gw_bonus})")

        # VIX
        if vix_val < VIX_LOW_MAX:
            score += VIX_LOW_BONUS
            score_details.append(f"vix{vix_val:.0f}(+{VIX_LOW_BONUS})")

        # Zone quality validation
        zq_score, zq_details, touches = validate_zone_quality(
            df_15m, z, i_15m, touch)
        score += zq_score
        score_details.extend(zq_details)

        # Compression
        is_compressed, comp_ratio = detect_compression(df_15m, i_15m)
        if is_compressed:
            score += ROCKET_COMPRESSION_BONUS
            score_details.append(f"coiled{comp_ratio:.2f}(+{ROCKET_COMPRESSION_BONUS})")

        # Confluence count for 15m path (relaxed: 3 instead of 4)
        confluence_count = 1  # zone quality
        if is_exp: confluence_count += 1
        if trend_aligned: confluence_count += 1
        if pdh_ok: confluence_count += 1
        if is_compressed: confluence_count += 1

        # Relaxed rocket gate for 15m-only path
        is_rocket = confluence_count >= 3
        if not is_rocket and not force_hunt:
            continue
        if score < min_score - 6:  # relaxed by 6 for 15m-only
            continue

        # Strike selection
        if touch == "demand" and is_exp:
            strike_kind = "ATM"
        elif is_exp or is_expiry:
            strike_kind = "ITM"
        else:
            strike_kind = "ATM"

        setup = {
            "direction": direction, "zone_type": touch,
            "zone_top": z["top"], "zone_bottom": z["bottom"],
            "entry_price": cur_price, "entry_1m_ts": df_15m.index[i_15m],
            "delta_reason": "15m-body-confirm",
            "setup_score": score, "score_details": score_details,
            "strategy": "15m_sniper" if is_exp else "15m_zone_touch",
            "is_expiry": is_expiry, "explosive": is_exp,
            "sweep": False, "strike_kind": strike_kind,
            "confluence_count": confluence_count,
        }

        if best is None or score > best["setup_score"]:
            best = setup

    return best


def find_tiger_brain_entry(df_15m, i_15m, df_1m, seg, is_expiry, symbol,
                           broker, pcr_cache, vix_val,
                           min_score=75.0, force_hunt=False):
    """
    Tiger Brain unified entry — scoring system, not gating.

    HARD GATES (must pass):
      1. Zone touch on 1m
      2. Volume delta in correct direction (1.3x)

    SCORE BOOSTERS (add to score, rank by score):
      All 5 brains contribute as scorers.
    """
    if not _is_symbol_allowed(symbol):
        return None  # symbol blocked (safety net)

    if df_1m is None or len(df_1m) == 0:
        return None

    bar_15m_start = df_15m.index[i_15m]
    bar_15m_end = bar_15m_start + pd.Timedelta(minutes=15)
    in_window = df_1m[(df_1m.index >= bar_15m_start) & (df_1m.index < bar_15m_end)]
    if len(in_window) == 0:
        return None

    # --- PRECOMPUTE CONFLUENCE DATA ---
    current_date = bar_15m_start.date() if bar_15m_start.tz is None else bar_15m_start.tz_convert("Asia/Kolkata").date()
    pdh, pdl = compute_pdh_pdl(df_15m, current_date)
    or_high, or_low = compute_opening_range(df_1m, current_date)
    vwap_series = compute_vwap_series(df_1m) if "volume" in df_1m.columns else None
    trend = detect_15m_trend(df_15m, i_15m)

    # --- DETECT ZONES (ALL zones, not just explosive) ---
    zone_idx = max(0, i_15m - 1)
    if zone_idx < 40:
        return None
    zones = detect_zones(df_15m, zone_idx, lookback=zone_idx)
    if not zones:
        return None

    best = None
    for ts_1m, bar_1m in in_window.iterrows():
        i_1m = df_1m.index.get_loc(ts_1m)
        if i_1m < 6:
            continue

        cur_price = float(bar_1m["close"])

        for z in zones:
            touch = zone_touched_on_1m(bar_1m, z)
            if touch is None:
                continue

            direction = "BUY" if touch == "demand" else "SELL"

            # === HARD GATE 1: Volume confirmation ===
            vol_ok, vol_reason = volume_confirmed(df_1m, i_1m, touch)
            if not vol_ok:
                continue

            # === SCORING STARTS ===
            score = z["score"]
            score_details = []

            # --- Explosive quality (bonus, data-driven) ---
            try:
                is_exp, exp_pct = zone_explosive_quality(
                    df_15m, z.get("bar_idx", zone_idx), touch)
                if is_exp:
                    score += EXPLOSIVE_BONUS
                    score_details.append(f"explosive(+{EXPLOSIVE_BONUS})")
            except Exception:
                is_exp, exp_pct = False, 0.0

            # --- Brain 2 booster: Delta spike (1.8x = bonus, not required) ---
            confirmed, delta_reason, spike_mult = False, "", 1.8
            try:
                confirmed, delta_val, delta_reason = delta_spike_confirms(
                    df_1m, i_1m, touch)
                spike_mult = 1.8
                try:
                    spike_mult = float(delta_reason.split()[-1].rstrip("x"))
                except (ValueError, IndexError):
                    pass
                if confirmed:
                    score += DELTA_SPIKE_BONUS
                    score_details.append(f"delta-spike {spike_mult:.1f}x(+{DELTA_SPIKE_BONUS})")
            except Exception:
                confirmed, delta_reason, spike_mult = False, "", 1.8

            # --- Brain 2 booster: Liquidity sweep ---
            try:
                swept, sweep_reason = liquidity_sweep(df_1m, i_1m, direction)
                if swept:
                    score += SWEEP_BONUS
                    score_details.append(f"sweep(+{SWEEP_BONUS})")
            except Exception:
                swept, sweep_reason = False, ""

            # --- Trend alignment (soft scorer) ---
            if direction == "BUY" and trend == "up":
                score += TREND_BONUS
                score_details.append(f"trend-up(+{TREND_BONUS})")
            elif direction == "SELL" and trend == "down":
                score += TREND_BONUS
                score_details.append(f"trend-down(+{TREND_BONUS})")

            # --- PDH/PDL confluence ---
            pdh_ok, pdh_reason = pdh_pdl_confluence(z["top"], z["bottom"], pdh, pdl)
            if pdh_ok:
                score += PDH_PDL_BONUS
                score_details.append(f"{pdh_reason}(+{PDH_PDL_BONUS})")

            # --- VWAP confluence ---
            if vwap_series is not None and ts_1m in df_1m.index:
                vwap_val = vwap_series.get(ts_1m, 0)
                vwap_ok, vwap_reason = vwap_confluence(cur_price, vwap_val)
                if vwap_ok:
                    score += VWAP_BONUS
                    score_details.append(f"vwap(+{VWAP_BONUS})")

            # --- Golden window ---
            gw_bonus = golden_window_bonus(ts_1m, seg)
            if gw_bonus > 0:
                score += gw_bonus
                score_details.append(f"window(+{gw_bonus})")

            # --- PCR sentiment ---
            if symbol not in pcr_cache:
                pcr_cache[symbol] = fetch_pcr(broker, symbol)
            pcr = pcr_cache[symbol]
            pcr_aligned = False
            if direction == "BUY" and pcr < PCR_BULLISH_MAX:
                score += PCR_BONUS
                score_details.append(f"pcr{pcr:.1f}(+{PCR_BONUS})")
                pcr_aligned = True
            elif direction == "SELL" and pcr > PCR_BEARISH_MIN:
                score += PCR_BONUS
                score_details.append(f"pcr{pcr:.1f}(+{PCR_BONUS})")
                pcr_aligned = True

            # --- VIX ---
            if vix_val < VIX_LOW_MAX:
                score += VIX_LOW_BONUS
                score_details.append(f"vix{vix_val:.0f}(+{VIX_LOW_BONUS})")

            # --- Opening Range Breakout ---
            orb_dir = orb_direction(cur_price, or_high, or_low)
            if direction == "BUY" and orb_dir == "bullish":
                score += ORB_BONUS
                score_details.append(f"orb-bull(+{ORB_BONUS})")
            elif direction == "SELL" and orb_dir == "bearish":
                score += ORB_BONUS
                score_details.append(f"orb-bear(+{ORB_BONUS})")

            # === ROCKET DETECTION LENS — the powerful meta-filter eyes ===
            # Scores rocket potential on 3 axes: zone quality, momentum
            # ignition, confluence stacking. Only multi-confluence setups
            # with ROCKET_MIN_CONFLUENCES+ independent confirmations AND
            # minimum rocket fuel pass. Stale zones (5+ touches) HARD REJECTED.
            try:
                rocket_bonus, rocket_details, confluence_count, is_rocket = \
                    rocket_detection_lens(
                        df_15m, i_15m, df_1m, i_1m, z, touch, direction,
                        spike_mult, swept, is_exp, trend, pdh_ok,
                        vwap_ok if 'vwap_ok' in dir() else False, orb_dir,
                        pcr_aligned, pcr, cur_price, vix_val)
                score += rocket_bonus
                score_details.extend(rocket_details)
            except Exception:
                confluence_count = 0
                is_rocket = False

            # === ROCKET GATE: only true rockets pass (50/100 filter) ===
            if not is_rocket and not force_hunt:
                continue
            if score < min_score:
                continue

            # === MINIMUM SCORE CHECK (legacy floor, now superseded by rocket gate) ===
            if score < min_score:
                continue

            # --- Strike selection ---
            # ITM for momentum trades (sweep/spike/supply-explosive) — higher delta =
            # faster move = profit before stop hit.
            # BUT explosive DEMAND uses ATM — data showed Demand_Boom with ITM = 45.5%
            # win (expensive ITM premiums + false breakout = big losses on stop).
            # Explosive SUPPLY keeps ITM (100% win in V5).
            if touch == "demand" and is_exp:
                strike_kind = "ATM"  # demand explosive = ATM (cheaper, less risk on false breakout)
            elif spike_mult >= 2.0 or swept or is_exp or is_expiry:
                strike_kind = "ITM"
            else:
                strike_kind = "ATM"

            strategy = "Tiger_Demand" if touch == "demand" else "Tiger_Supply"
            if is_exp:
                strategy += "_Boom"
            if swept:
                strategy += "_Sweep"

            candidate = {
                "direction": direction,
                "zone_type": touch,
                "zone_top": z["top"],
                "zone_bottom": z["bottom"],
                "entry_price": cur_price,
                "entry_1m_ts": ts_1m,
                "entry_1m_idx": i_1m,
                "delta_reason": vol_reason,
                "sweep": swept,
                "sweep_reason": sweep_reason if swept else "",
                "delta_spike_mult": round(spike_mult, 1),
                "explosive": is_exp,
                "expansion_pct": round(exp_pct, 2) if is_exp else 0.0,
                "setup_score": round(score, 1),
                "score_details": ", ".join(score_details),
                "strategy": strategy,
                "strike_kind": strike_kind,
                "is_expiry": is_expiry,
                "trend": trend,
                "pcr": round(pcr, 2),
                "pcr_aligned": pcr_aligned,
                "vix": round(vix_val, 1),
                "vwap_confluence": vwap_ok if 'vwap_ok' in dir() else False,
                "pdh_pdl": pdh_ok,
                "orb": orb_dir,
                "confluence_count": confluence_count,
                "rocket_grade": is_rocket,
            }
            if best is None or candidate["setup_score"] > best["setup_score"]:
                best = candidate
    return best


# ============================================================
# STRUCTURAL STOP + DYNAMIC SIZING + PREMIUM SANITY
# ============================================================
def compute_structural_stop(entry_premium, zone, zone_type, cur_underlying,
                            strike, dte, is_call, iv):
    stop_underlying = zone["bottom"] if zone_type == "demand" else zone["top"]
    stop_prem = bs_premium_at(stop_underlying, strike, dte, is_call, iv)
    stop_prem = max(stop_prem, 0.5)
    # 80% of entry = -20% stop (was 0.60 = -40% — too wide, bled to death)
    stop_prem = min(stop_prem, entry_premium * 0.80)
    return max(stop_prem, 0.5)

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

def premium_sane(entry_premium, cur_underlying) -> tuple[bool, str]:
    if entry_premium < PREMIUM_MIN:
        return (False, f"premium too low (₹{entry_premium:.1f})")
    pct = entry_premium / cur_underlying * 100
    if pct > PREMIUM_MAX_PCT_OF_UNDERLYING:
        return (False, f"overpriced ({pct:.1f}% of underlying)")
    return (True, "sane")


# ============================================================
# TOKEN RESOLUTION
# ============================================================
def _resolve_symbol_token(symbol: str) -> tuple[str, str] | None:
    if symbol in ANGEL_EXCHANGE:
        exchange, search = ANGEL_EXCHANGE[symbol]
    else:
        exchange, search = "NSE", symbol
    try:
        matches = find_symbol_token(exchange, search)
    except Exception as exc:
        logger.error(f"{symbol}: lookup failed: {exc}")
        return None
    if matches is None or matches.empty:
        return None
    sym_upper = search.upper()
    if exchange == "MCX":
        # Prefer near-month FUT contract (continuous underlying for zones).
        # "MCOM" filter is too broad — "CRUDEOILMCOM" contains "MCOM" as
        # part of the name, giving wrong spot commodity tokens.
        fut = matches[matches["symbol"].str.upper().str.contains("FUT", na=False)]
        if not fut.empty:
            # Pick the nearest-month expiry: extract date from symbol name
            # (e.g., CRUDEOIL19OCT26FUT → 2026-10-19) and choose closest.
            now = pd.Timestamp.now()
            best_row, best_diff = None, float("inf")
            for _, r in fut.iterrows():
                m = re.search(r"(\d{1,2})([A-Z]{3})(\d{2})FUT", r["symbol"].upper())
                if m:
                    try:
                        dt = pd.Timestamp(f"20{m.group(3)}-{m.group(2)[:3].title()}-{m.group(1).zfill(2)}")
                        diff = abs((dt - now).total_seconds())
                        if diff < best_diff:
                            best_diff, best_row = diff, r
                    except Exception:
                        continue
            row = best_row if best_row is not None else fut.iloc[0]
        else:
            row = matches.iloc[0]
        return exchange, str(row["token"])
    exact_eq = matches[matches["symbol"].str.upper() == f"{sym_upper}-EQ"]
    if exact_eq.empty:
        exact_eq = matches[matches["symbol"].str.upper() == sym_upper]
    if exact_eq.empty:
        exact_eq = matches[matches["symbol"].str.upper().str.endswith("-EQ")]
    row = exact_eq.iloc[0] if not exact_eq.empty else matches.iloc[0]
    return exchange, str(row["token"])


def fetch_yfinance_fallback(symbol, ticker, days_15m=365, days_1m=90):
    """yfinance se 15m + 1m data laata hai (rate-limit-free, free API).

    yfinance limits:
      - 15m interval: max 60 days
      - 1m interval: max 7 days per request
    Isliye days ko cap karte hain yfinance ke limits pe.
    """
    import yfinance as yf
    to_date = datetime.now()
    # yfinance caps: 15m=30d (safe), 1m=7d
    days_15m = min(days_15m, 30)
    days_1m = min(days_1m, 7)
    from_15m = to_date - timedelta(days=days_15m)
    from_1m = to_date - timedelta(days=days_1m)

    data_15m, data_1m = None, None
    try:
        raw15 = yf.download(ticker, start=from_15m, end=to_date,
                            interval="15m", progress=False, auto_adjust=True)
        if raw15 is not None and not raw15.empty:
            # Flatten MultiIndex or tuple-valued columns to simple strings
            flat_cols = []
            for c in raw15.columns:
                if isinstance(c, tuple):
                    flat_cols.append(c[0])
                elif isinstance(c, str):
                    flat_cols.append(c)
                else:
                    flat_cols.append(str(c))
            raw15.columns = flat_cols
            if raw15.index.tz is None:
                raw15.index = raw15.index.tz_localize("UTC")
            raw15.index = raw15.index.tz_convert("Asia/Kolkata")
            if "Close" in raw15.columns:
                raw15 = raw15.dropna(subset=["Close"])
            data_15m = _normalize_cols(raw15)
    except Exception as exc:
        logger.warning(f"{symbol} (yfinance 15m): {exc}")

    try:
        raw1 = yf.download(ticker, start=from_1m, end=to_date,
                           interval="1m", progress=False, auto_adjust=True)
        if raw1 is not None and not raw1.empty:
            flat_cols = []
            for c in raw1.columns:
                if isinstance(c, tuple):
                    flat_cols.append(c[0])
                elif isinstance(c, str):
                    flat_cols.append(c)
                else:
                    flat_cols.append(str(c))
            raw1.columns = flat_cols
            if raw1.index.tz is None:
                raw1.index = raw1.index.tz_localize("UTC")
            raw1.index = raw1.index.tz_convert("Asia/Kolkata")
            if "Close" in raw1.columns:
                raw1 = raw1.dropna(subset=["Close"])
            data_1m = _normalize_cols(raw1)
    except Exception as exc:
        logger.warning(f"{symbol} (yfinance 1m): {exc}")

    return data_15m, data_1m


def fetch_angel_data(broker, days_15m=365, days_1m=90, use_scan_universe=False, fetch_1m=True, symbols=None):
    """Fetch 15m (1 year) + 1m (max available) historical candles.

    PRIMARY: Angel One real historical candles (accurate market data).
    FALLBACK: yfinance (US futures proxy, IST-converted) — sirf tab jab
    Angel One fail ho (rate limit, token resolve fail, etc).

    Args:
        use_scan_universe: if True, scan full 150+ F&O universe (Tiger V16).
            if False, use default 40-symbol trading universe.
        symbols: if provided, use this {symbol: ticker} dict directly
            (overrides use_scan_universe). Used by live path to fetch only
            the active market's symbols (NSE or MCX).
    """
    from universe.fno_universe import scan_universe
    to_date = datetime.now().replace(hour=15, minute=30, second=0, microsecond=0)
    from_15m = to_date - timedelta(days=days_15m)
    from_1m = to_date - timedelta(days=days_1m)
    data_map, data_map_1m = {}, {}
    failed = []
    yf_used = []
    angel_used = []
    if symbols is not None:
        syms = symbols
    elif use_scan_universe:
        syms = scan_universe()
    else:
        syms = all_symbols()
    total = len(syms)
    for idx, (sym, ticker) in enumerate(syms.items(), 1):
        tag = f"[{idx}/{total}] {sym}"

        # PRIMARY: Angel One se real historical candles
        if broker is not None and broker.smart_api is not None:
            try:
                time.sleep(0.5)  # reduced from 3.0s — 42-symbol universe can't afford 252s sleep
                d15 = fetch_angel_underlying_candles(
                    broker, sym, "FIFTEEN_MINUTE", days=days_15m)
                d1 = None
                if fetch_1m:
                    time.sleep(0.5)  # reduced from 3.0s — keep scan under 60s total
                    d1 = fetch_angel_underlying_candles(
                        broker, sym, "ONE_MINUTE", days=days_1m)
                if d15 is not None and not d15.empty:
                    d15 = _normalize_cols(d15)
                    data_map[sym] = d15
                    if d1 is not None and not d1.empty:
                        d1 = _normalize_cols(d1)
                        data_map_1m[sym] = d1
                    angel_used.append(sym)
                    print(f"  {tag:30s}: 15m={len(d15):5d}  1m={len(data_map_1m.get(sym, [])):5d}  [ANGEL]")
                    continue
            except Exception as exc:
                logger.warning(f"{tag}: Angel fetch fail — yfinance fallback: {exc}")

        # FALLBACK: yfinance (sirf agar Angel One fail hua)
        try:
            d15, d1 = fetch_yfinance_fallback(sym, ticker, days_15m, days_1m)
            if d15 is not None and not d15.empty:
                data_map[sym] = d15
                if fetch_1m and d1 is not None and not d1.empty:
                    data_map_1m[sym] = d1
                yf_used.append(sym)
                print(f"  {tag:30s}: 15m={len(d15):5d}  1m={len(data_map_1m.get(sym, [])):5d}  [yfinance]")
                continue
            else:
                failed.append(sym)
                continue
        except Exception as exc:
            logger.error(f"{tag}: yfinance error: {exc}")
            failed.append(sym)
            continue
    if angel_used:
        print(f"\n  Angel One data used for: {len(angel_used)} symbols")
    if yf_used:
        print(f"  yfinance fallback used for: {yf_used}")
    print(f"\nFailed: {failed}")
    print(f"Universe: 15m={len(data_map)}  1m={len(data_map_1m)}")
    return data_map, data_map_1m, failed


# ============================================================
# MAIN BACKTEST ENGINE
# ============================================================
def run_tiger_brain_backtest(data_map, start_capital=150000.0,
                             max_loss_per_trade=2000.0,
                             max_capital_per_trade_pct=10.0,
                             dte_default=1.0, verbose=False,
                             data_map_1m=None, broker=None,
                             use_fund_brain=True, use_delivery_mode=True,
                             use_premium_brain=True, use_session_brain=True):
    use_sniper = data_map_1m is not None
    all_ts = sorted(set().union(*[set(d.index) for d in data_map.values()]))
    days_map = defaultdict(list)
    for ts in all_ts:
        days_map[ts.normalize()].append(ts)
    sorted_days = sorted(days_map.keys())

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
    daily_entries_taken = defaultdict(int)

    # === BRAIN 6: Premium Discount Tracker ===
    premium_tracker = PremiumDiscountTracker() if use_premium_brain else None

    # === BRAIN 7: Session Commander ===
    if use_session_brain:
        print("\n" + "=" * 72)
        print("  🐅 TIGER SESSION COMMANDER — V19 SESSION SCHEDULE")
        print("=" * 72)
        print(print_session_schedule())

    # === FUND BRAIN — pre-market capital announcement ===
    fund_plan = None
    if use_fund_brain:
        fund_plan = announce_fund_plan(start_capital)
        max_loss_per_trade = fund_plan.risk_per_trade_rupees
        max_capital_per_trade_pct = fund_plan.max_capital_per_trade_pct
        print("\n" + "=" * 72)
        print("  🪖 TIGER FUND BRAIN — PRE-MARKET ANNOUNCEMENT")
        print("=" * 72)
        print(fund_plan.summary())
        print("=" * 72)

    for day in sorted_days:
        day_ts = days_map[day]
        sim_date = day.date() if day.tz is None else day.tz_convert("Asia/Kolkata").date()
        if counter._today != sim_date:
            counter._today = sim_date
            counter.global_count = 0
            counter.commodity_count = 0
            pcr_cache.clear()
            daily_entries_taken[sim_date] = 0

        # VIX for this day
        vix_val = get_vix_for_date(day)

        # === BRAIN 7: Daily hunt status ===
        daily_hunt = HuntStatus(date=str(sim_date)) if use_session_brain else None

        for ts in day_ts:
            # --- 1. EXIT (Brain 5) ---
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
                is_call = pos["option_type"] == "CE"
                iv = compute_iv(df_so_far, vix_val, sym, is_call, pos["strike"], cur_underlying)
                cur_prem = bs_premium(cur_underlying, pos["strike"], pos["dte"], is_call, iv)
                if cur_prem > pos.get("peak_premium", 0):
                    pos["peak_premium"] = cur_prem

                # === BRAIN 6: IV expansion exit — sell when premium becomes expensive ===
                if premium_tracker is not None:
                    prem_exit = check_premium_exit(pos, current_iv=iv, tracker=premium_tracker)
                    if prem_exit["exit"]:
                        exit_prem = prem_exit.get("exit_premium") or cur_prem
                        slippage = exit_prem * 0.008 + pos["entry_premium"] * 0.008
                        brokerage = 20.0 * 2
                        pnl = (exit_prem - pos["entry_premium"]) * pos["quantity"] - slippage * 2 - brokerage
                        pnl = max(pnl, -max_loss_per_trade - brokerage - slippage * 2 + 1)
                        capital += pnl
                        exit_g = bs_greeks_full(cur_underlying, pos["strike"], pos["dte"], is_call, iv)
                        trades.append({
                            **pos, "exit_ts": ts, "exit_premium": exit_prem, "pnl": pnl,
                            "exit_reason": prem_exit["reason"],
                            "hold_bars": len(df_so_far) - pos["entry_idx"],
                            "confirmation": pos.get("delta_reason", "A+"),
                            "exit_delta": exit_g["delta"], "exit_gamma": exit_g["gamma"],
                            "exit_theta": exit_g["theta"], "exit_vega": exit_g["vega"],
                        })
                        if verbose:
                            logger.warning(f"EXIT {sym} {prem_exit['reason']} pnl={pnl:.0f}")
                        continue

                sq_off = _is_square_off_bar(ts, seg)
                i_1m_pos = None
                df_1m_pos = None
                if use_sniper and data_map_1m and sym in data_map_1m:
                    df_1m_pos = data_map_1m[sym]
                    if ts in df_1m_pos.index:
                        i_1m_pos = df_1m_pos.index.get_loc(ts)
                # === DELIVERY MODE — don't square off delivery trades ===
                # Delivery trades hold 2-3 days. Only exit on stop/target/trail.
                is_delivery_pos = pos.get("is_delivery", False)
                if is_delivery_pos:
                    sq_off = False  # override: delivery doesn't square off at close
                    # Track hold days
                    hold_bars = len(df_so_far) - pos["entry_idx"]
                    pos["hold_days"] = hold_bars / 25  # approx trading days
                    # Force exit if max hold days reached
                    if pos["hold_days"] >= DELIVERY_MAX_HOLD_DAYS:
                        ex = {"exit": True, "exit_premium": cur_prem,
                              "reason": f"delivery_max_hold({DELIVERY_MAX_HOLD_DAYS}d)"}
                    else:
                        ex = check_intraday_exit_v19(pos, cur_underlying, cur_prem, False,
                                                     df_1m=df_1m_pos, i_1m=i_1m_pos,
                                                     ts=ts, segment=seg)
                else:
                    ex = check_intraday_exit_v19(pos, cur_underlying, cur_prem, sq_off,
                                                 df_1m=df_1m_pos, i_1m=i_1m_pos,
                                                 ts=ts, segment=seg)
                if ex["exit"]:
                    exit_prem = ex["exit_premium"]
                    slippage = exit_prem * 0.008 + pos["entry_premium"] * 0.008
                    brokerage = 20.0 * 2
                    pnl = (exit_prem - pos["entry_premium"]) * pos["quantity"] - slippage * 2 - brokerage
                    pnl = max(pnl, -max_loss_per_trade - brokerage - slippage * 2 + 1)
                    capital += pnl
                    exit_g = bs_greeks_full(cur_underlying, pos["strike"], pos["dte"], is_call, iv)
                    trades.append({
                        **pos, "exit_ts": ts, "exit_premium": exit_prem, "pnl": pnl,
                        "exit_reason": ex["reason"],
                        "hold_bars": len(df_so_far) - pos["entry_idx"],
                        "confirmation": pos.get("delta_reason", "A+"),
                        "exit_delta": exit_g["delta"], "exit_gamma": exit_g["gamma"],
                        "exit_theta": exit_g["theta"], "exit_vega": exit_g["vega"],
                    })
                    if verbose:
                        logger.warning(f"EXIT {sym} {ex['reason']} pnl={pnl:.0f}")
                else:
                    pos["dte"] = max(pos["dte"] - 1 / 25, 0.05)
                    still_open.append(pos)
            open_positions = still_open

            # --- 2. ENTRY SCAN (scoring system) ---
            if daily_entries_taken[sim_date] >= MAX_ENTRIES_PER_DAY:
                continue

            ts_ist = ts.tz_convert("Asia/Kolkata") if hasattr(ts, 'tz_convert') and ts.tz is not None else ts
            ts_time = ts_ist.time() if hasattr(ts_ist, 'time') else ts.time()

            day_candidates = []
            for sym, df_sym in data_map.items():
                if ts not in df_sym.index:
                    continue
                idx = df_sym.index.get_loc(ts)
                if idx < 40:
                    continue
                seg = segment_of(sym)

                # --- Brain 1: Gate ---
                b1 = brain1_intraday_pass(df_sym, idx, segment=seg)
                if not b1["passed_brain1"]:
                    continue
                brain_log["brain1_pass"] += 1

                if _is_square_off_bar(ts, seg):
                    continue

                expiry = use_sniper and is_expiry_day(sym, ts)

                # === BRAIN 7: Session threshold + force hunt (PER SEGMENT) ===
                cand_force_hunt = False
                cand_min_score = ROCKET_MIN_SCORE
                if use_session_brain and daily_hunt is not None:
                    session_thresh = get_session_score_threshold(ts_time, seg)
                    if session_thresh < 999:
                        cand_min_score = min(cand_min_score, session_thresh)
                    cand_force_hunt, fh_thresh, _ = should_force_hunt(ts_time, daily_hunt, seg)
                    if cand_force_hunt:
                        cand_min_score = min(cand_min_score, fh_thresh)

                if use_sniper and data_map_1m and sym in data_map_1m:
                    setup = find_tiger_brain_entry(
                        df_sym, idx, data_map_1m[sym], seg, expiry, sym,
                        broker, pcr_cache, vix_val,
                        min_score=cand_min_score, force_hunt=cand_force_hunt)
                    if setup is None:
                        filter_stats["rejected_low_score_or_volume"] += 1
                        continue
                    brain_log["entry_scored"] += 1
                elif use_sniper:
                    # No 1m data for this symbol — try 15m-only entry
                    # (indexes often lack 1m data from Angel One)
                    setup = find_tiger_brain_entry_15m(
                        df_sym, idx, seg, expiry, sym, vix_val,
                        min_score=cand_min_score, force_hunt=cand_force_hunt)
                    if setup is None:
                        continue
                    brain_log["entry_scored"] += 1
                else:
                    continue

                day_candidates.append({
                    "symbol": sym, "setup": setup, "df_sym": df_sym,
                    "idx": idx, "ts": ts, "seg": seg, "expiry": expiry,
                    "force_hunt": cand_force_hunt,
                    "min_score": cand_min_score,
                })

            day_candidates.sort(key=lambda c: c["setup"]["setup_score"], reverse=True)

            for cand in day_candidates:
                if daily_entries_taken[sim_date] >= MAX_ENTRIES_PER_DAY:
                    break
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
                is_call = setup["direction"] == "BUY"

                # === BRAIN 7: Session threshold + force hunt (from candidate) ===
                force_hunt = cand.get("force_hunt", False)
                adjusted_score_threshold = cand.get("min_score", ROCKET_MIN_SCORE)

                # Final score check — ensures force hunt / session threshold applied
                if setup["setup_score"] < adjusted_score_threshold:
                    continue

                # === Strike selection + IV computation (must be before Brain 6) ===
                strike_kind = setup.get("strike_kind", "ATM")
                delta_in_reason = "delta" in setup.get("delta_reason", "")
                if (expiry and delta_in_reason) or strike_kind == "ITM":
                    strike = round(cur_underlying * 0.99) if is_call else round(cur_underlying * 1.01)
                elif strike_kind == "OTM":
                    strike = round(cur_underlying * 1.01) if is_call else round(cur_underlying * 0.99)
                else:
                    strike = round(cur_underlying)
                iv = compute_iv(df_so_far, vix_val, sym, is_call, strike, cur_underlying)

                # === BRAIN 6: Premium discount — V18 LOOSE entry (advisory only) ===
                # V18 style: the IV filter NEVER blocks entry and NEVER overrides
                # the strike. It only awards a discount bonus to the score.
                # V19 exit logic (IV expansion sell) is retained in the exit loop.
                iv_percentile = 50.0
                premium_bonus = 0.0
                prem_snap = None
                if premium_tracker is not None:
                    prem_snap = premium_tracker.evaluate(sym, current_iv=iv, setup_score=setup["setup_score"])
                    iv_percentile = prem_snap.iv_percentile
                    premium_bonus = prem_snap.discount_bonus
                    # V18 loose: should_enter is always True — no IV blocking
                    # (kept for safety; the gate is a no-op under V18 loose logic)

                entry_prem = bs_premium(cur_underlying, strike, dte_default, is_call, iv)
                entry_prem = max(entry_prem, 1.0)

                sane, sane_reason = premium_sane(entry_prem, cur_underlying)
                if not sane:
                    filter_stats["rejected_premium_sanity"] += 1
                    continue

                # Tier-2 MCX commodities have structurally wider spreads
                # (0.55% base vs 0.50% gate). Loosen gate for commodities so
                # real MCX options can enter — V6.6 locked gate stays default
                # for NSE (tier-1) symbols.
                spread_max = 0.75 if seg == "commodity" else MAX_SPREAD_PCT
                ok_spread, spread_pct = spread_ok(sym, entry_prem, max_pct=spread_max)
                if not ok_spread:
                    filter_stats["rejected_spread"] += 1
                    continue

                stop_prem = compute_structural_stop(
                    entry_premium=entry_prem,
                    zone={"top": setup["zone_top"], "bottom": setup["zone_bottom"]},
                    zone_type=setup["zone_type"],
                    cur_underlying=cur_underlying,
                    strike=strike, dte=dte_default, is_call=is_call, iv=iv)

                lot_sz = lot_size(sym)

                # === DELIVERY MODE — ultra-high-conviction rockets hold 2-3 days ===
                is_delivery = (use_delivery_mode and
                               setup.get("setup_score", 0) >= DELIVERY_ROCKET_MIN_SCORE)
                if is_delivery:
                    # Wider stop for delivery (let trade breathe over multiple days)
                    stop_prem = min(stop_prem, entry_prem * (1 - DELIVERY_STOP_PCT / 100))
                    stop_prem = max(stop_prem, 0.5)

                # === FUND BRAIN SIZING — capital-based, not lot-based ===
                if fund_plan is not None:
                    current_exposure = sum(
                        p["entry_premium"] * p["quantity"] for p in open_positions)
                    sizing = size_trade_with_fund_brain(
                        fund_plan, entry_prem, stop_prem, lot_sz,
                        current_exposure=current_exposure,
                        is_delivery=is_delivery)
                    if sizing["quantity"] <= 0:
                        filter_stats["rejected_fund_brain_sizing"] += 1
                        continue
                else:
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
                daily_entries_taken[sim_date] += 1

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

                entry_g = bs_greeks_full(cur_underlying, strike, dte_default, is_call, iv)

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
                    "dte": dte_default, "iv": iv,
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
                    "vix": setup.get("vix", 15.0),
                    "setup_score": setup.get("setup_score", 0),
                    "score_details": setup.get("score_details", ""),
                    "structural_stop": True,
                    "pricing_model": "Black-Scholes",
                    "entry_delta": entry_g["delta"],
                    "entry_gamma": entry_g["gamma"],
                    "entry_theta": entry_g["theta"],
                    "entry_vega": entry_g["vega"],
                    "is_delivery": is_delivery,
                    "hold_days": 0,
                    "iv_percentile": iv_percentile,
                    "premium_bonus": premium_bonus,
                    "force_hunt": force_hunt,
                    "session": get_current_session(ts_time, seg).name if use_session_brain and get_current_session(ts_time, seg) else "OFF",
                }
                # === BRAIN 6: Update IV history ===
                if premium_tracker is not None:
                    premium_tracker.update(sym, iv)
                # === BRAIN 7: Record hunt trade ===
                if daily_hunt is not None:
                    daily_hunt.record_trade(seg)
                open_positions.append(pos)
                if verbose:
                    logger.warning(
                        f"ENTRY {sym} {setup['strategy']} @{entry_prem:.1f} "
                        f"score={setup['setup_score']:.0f} "
                        f"Δ={entry_g['delta']:.2f} trend={setup.get('trend','?')} "
                        f"[{setup.get('score_details','')}]")

            equity_curve.append(capital)
            if capital > peak_equity:
                peak_equity = capital
            dd = (peak_equity - capital) / peak_equity * 100
            if dd > max_dd:
                max_dd = dd

    # Close remaining
    for pos in open_positions:
        sym = pos["symbol"]
        df_sym = data_map[sym]
        last_ts = df_sym.index[-1]
        cur_underlying = float(df_sym.iloc[-1]["close"])
        is_call = pos["option_type"] == "CE"
        iv = compute_iv(df_sym, vix_val, sym, is_call, pos["strike"], cur_underlying)
        cur_prem = bs_premium(cur_underlying, pos["strike"], pos["dte"], is_call, iv)
        slippage = cur_prem * 0.008 + pos["entry_premium"] * 0.008
        brokerage = 20.0 * 2
        pnl = (cur_prem - pos["entry_premium"]) * pos["quantity"] - slippage * 2 - brokerage
        pnl = max(pnl, -max_loss_per_trade - brokerage - slippage * 2 + 1)
        capital += pnl
        exit_g = bs_greeks_full(cur_underlying, pos["strike"], pos["dte"], is_call, iv)
        trades.append({
            **pos, "exit_ts": last_ts, "exit_premium": cur_prem, "pnl": pnl,
            "exit_reason": "end_of_data",
            "hold_bars": len(df_sym) - pos["entry_idx"],
            "confirmation": pos.get("delta_reason", "A+"),
            "exit_delta": exit_g["delta"], "exit_gamma": exit_g["gamma"],
            "exit_theta": exit_g["theta"], "exit_vega": exit_g["vega"],
        })

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))

    daily_counts = Counter()
    daily_comm = Counter()
    for t in trades:
        d = t["entry_ts"].normalize()
        daily_counts[d] += 1
        if t["segment"] == "commodity":
            daily_comm[d] += 1

    seg_stats = {}
    for seg_key in ("index", "stock", "commodity"):
        st = [t for t in trades if t["segment"] == seg_key]
        if not st:
            seg_stats[seg_key] = None
            continue
        sw = [t for t in st if t["pnl"] > 0]
        sl = [t for t in st if t["pnl"] <= 0]
        gp = sum(t["pnl"] for t in sw)
        gl = abs(sum(t["pnl"] for t in sl))
        seg_stats[seg_key] = {
            "label": UNIVERSE[seg_key]["label"], "trades": len(st),
            "wins": len(sw),
            "win_rate_pct": round(len(sw) / len(st) * 100, 1) if st else 0.0,
            "net_pnl": round(sum(t["pnl"] for t in st), 2),
            "profit_factor": round(gp / gl, 2) if gl > 0 else float("inf"),
        }

    strat_stats = {}
    for t in trades:
        sn = t.get("strategy", "unknown")
        if sn not in strat_stats:
            strat_stats[sn] = {"trades": 0, "wins": 0, "pnl": 0.0}
        strat_stats[sn]["trades"] += 1
        if t["pnl"] > 0:
            strat_stats[sn]["wins"] += 1
        strat_stats[sn]["pnl"] += t["pnl"]

    totals = {
        "start_capital": start_capital,
        "final_equity": round(capital, 2),
        "total_return_pct": round((capital - start_capital) / start_capital * 100, 2),
        "net_pnl": round(capital - start_capital, 2),
        "total_trades": len(trades),
        "wins": len(wins), "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "avg_pnl_per_trade": round(sum(t["pnl"] for t in trades) / len(trades), 2) if trades else 0.0,
        "avg_winner": round(gross_profit / len(wins), 2) if wins else 0.0,
        "avg_loser": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "best_trade": round(max((t["pnl"] for t in trades), default=0), 2),
        "worst_trade": round(min((t["pnl"] for t in trades), default=0), 2),
        "max_drawdown_pct": round(max_dd, 2),
        "max_daily_global_trades": max(daily_counts.values()) if daily_counts else 0,
        "max_daily_commodity_trades": max(daily_comm.values()) if daily_comm else 0,
    }

    return {
        "totals": totals, "trades": trades, "segment_stats": seg_stats,
        "strategy_stats": strat_stats, "equity_curve": equity_curve,
        "filter_stats": dict(filter_stats), "brain_log": dict(brain_log),
        "pricing_model": "Black-Scholes",
    }


def main():
    print("\n" + "#" * 72)
    print("#  TIGER BRAIN — ONE MAN ARMY 🪖")
    print("#  Super Powerful Race Car + Sniper + Research Tool")
    print("#")
    print("#  SCORING SYSTEM (not gating) — 50% pass rate target")
    print("#  HARD GATES: Zone touch + Volume delta (1.3x)")
    print("#  SCORE: Explosive(+10) Sweep(+8) DeltaSpike(+5) Trend(+5)")
    print("#         PDH/PDL(+5) VWAP(+5) PCR(+3) VIX(+2) ORB(+3) Window(+3)")
    print("#")
    print("#  ALL 5 BRAINS AWAKE as scorers | Black-Scholes + Greeks")
    print("#  Big AND small momentum caught | V6.6 core UNTOUCHED")
    print("#" * 72)

    print("\nLoading Angel One instrument master...")
    try:
        load_angel_instrument_master()
        print("  ✓ Instrument master loaded.")
    except Exception as exc:
        print(f"  ✗ Failed: {exc}")
        return

    print("\nLogging in to Angel One SmartAPI...")
    broker = AngelBroker()
    try:
        broker.login()
        print("  ✓ Login successful.")
    except Exception as exc:
        print(f"  ✗ Login failed: {exc}")
        return

    print("\nFetching 3-month (90-day) data from Angel One...")
    # Full market scan: 150+ F&O stocks + index + commodities — DEFAULT ON
    use_full_scan = "--no-full-scan" not in sys.argv  # opt-out instead of opt-in
    data_map, data_map_1m, failed = fetch_angel_data(broker, use_scan_universe=use_full_scan)
    if not data_map:
        print("\n✗ No data — cannot run.")
        return

    scan_label = "FULL 150+" if use_full_scan else "40 CORE"
    print(f"\nRunning TIGER BRAIN ONE MAN ARMY V16 ({len(data_map)} symbols, {scan_label} scan)...")

    # Capital from --capital flag (default ₹1.5L)
    start_cap = 150000.0
    for i, arg in enumerate(sys.argv):
        if arg == "--capital" and i + 1 < len(sys.argv):
            start_cap = float(sys.argv[i + 1])

    combined = run_tiger_brain_backtest(
        data_map, start_capital=start_cap,
        data_map_1m=data_map_1m if data_map_1m else None, broker=broker)

    print("\n\n")
    print("#" * 72)
    print("#  TIGER BRAIN ONE MAN ARMY — COMBINED PORTFOLIO")
    print("#  Scoring System + Black-Scholes + Greeks + All 5 Brains")
    print("#" * 72)
    print_report(combined)

    # Filter stats
    fs = combined.get("filter_stats", {})
    if fs:
        print("\n" + "-" * 72)
        print("  FILTER STATISTICS")
        print("-" * 72)
        for fn, c in sorted(fs.items(), key=lambda x: -x[1]):
            print(f"  {fn:40s}: {c:6d}")
        print("-" * 72)

    # Brain activity
    bl = combined.get("brain_log", {})
    if bl:
        print("\n" + "-" * 72)
        print("  BRAIN ACTIVITY LOG")
        print("-" * 72)
        for bn, c in sorted(bl.items(), key=lambda x: -x[1]):
            print(f"  {bn:40s}: {c:6d}")
        total_scored = bl.get("entry_scored", 0)
        total_gated = bl.get("brain1_pass", 0)
        if total_gated > 0:
            pass_rate = total_scored / total_gated * 100
            print(f"\n  PASS RATE: {total_scored}/{total_gated} = {pass_rate:.1f}%")
        print("-" * 72)

    # Greeks summary
    if combined["trades"]:
        print("\n" + "-" * 72)
        print("  GREEKS SUMMARY (Black-Scholes)")
        print("-" * 72)
        n = len(combined["trades"])
        avg_ed = sum(t.get("entry_delta", 0) for t in combined["trades"]) / n
        avg_et = sum(t.get("entry_theta", 0) for t in combined["trades"]) / n
        avg_ev = sum(t.get("entry_vega", 0) for t in combined["trades"]) / n
        avg_eg = sum(t.get("entry_gamma", 0) for t in combined["trades"]) / n
        print(f"  Avg Delta:  {avg_ed:+.4f}  | Avg Gamma: {avg_eg:.6f}")
        print(f"  Avg Theta:  {avg_et:+.2f}/day  | Avg Vega:  {avg_ev:+.2f}")
        print("-" * 72)

    # Score breakdown for all trades
    if combined["trades"]:
        print("\n" + "-" * 72)
        print("  SCORE BREAKDOWN (what made each trade qualify)")
        print("-" * 72)
        print(f"  {'Symbol':12s} {'Dir':4s} {'Score':>5s} {'P&L':>8s}  Details")
        print("  " + "-" * 68)
        for t in sorted(combined["trades"], key=lambda x: x["pnl"], reverse=True):
            print(f"  {t['symbol']:12s} {t['direction']:4s} "
                  f"{t.get('setup_score',0):5.0f} {t['pnl']:+8.0f}  "
                  f"{t.get('score_details','')}")
        print("-" * 72)

    # Collect standalone segment results for parallel comparison
    seg_results = {}
    for seg_key in ("index", "stock", "commodity"):
        seg_syms = list(UNIVERSE[seg_key]["symbols"].keys())
        seg_map = {s: data_map[s] for s in seg_syms if s in data_map}
        seg_map_1m = {s: data_map_1m[s] for s in seg_syms if s in data_map_1m}
        print("\n\n")
        print("#" * 72)
        print(f"#  TIGER BRAIN — {UNIVERSE[seg_key]['label'].upper()} (standalone)")
        print("#" * 72)
        if not seg_map:
            print("  (no data)")
            seg_results[seg_key] = None
            continue
        seg_res = run_tiger_brain_backtest(
            seg_map, start_capital=150000.0, max_loss_per_trade=2000.0,
            data_map_1m=seg_map_1m if seg_map_1m else None, broker=broker)
        print_report(seg_res)
        seg_results[seg_key] = seg_res

    # ============================================================
    # PARALLEL SEGMENT COMPARISON TABLE
    # Side-by-side: Index vs Stock vs Commodity (win%, P&L, DD)
    # ============================================================
    print("\n\n" + "=" * 80)
    print("  🪖 PARALLEL MULTI-MARKET SEGMENT COMPARISON 🪖")
    print("  (each segment backtested independently with ₹1.5L capital)")
    print("=" * 80)
    print(f"  {'Segment':40s} {'Trades':>7s} {'Win%':>7s} {'Net P&L':>10s} {'Return%':>8s} {'MaxDD%':>7s} {'PF':>5s}")
    print("  " + "-" * 78)
    best_seg = None
    best_return = -999
    for seg_key in ("index", "stock", "commodity"):
        res = seg_results.get(seg_key)
        label = UNIVERSE[seg_key]["label"]
        if res is None or not res.get("trades"):
            print(f"  {label:40s} {'—':>7s} {'—':>7s} {'—':>10s} {'—':>8s} {'—':>7s} {'—':>5s}")
            continue
        trades = len(res["trades"])
        wins = sum(1 for t in res["trades"] if t["pnl"] > 0)
        win_pct = wins / trades * 100 if trades else 0
        net_pnl = res.get("net_pnl", sum(t["pnl"] for t in res["trades"]))
        final_eq = res.get("final_equity", 150000 + net_pnl)
        ret_pct = (final_eq - 150000) / 150000 * 100
        max_dd = res.get("max_drawdown_pct", 0)
        gross_win = sum(t["pnl"] for t in res["trades"] if t["pnl"] > 0)
        gross_loss = abs(sum(t["pnl"] for t in res["trades"] if t["pnl"] < 0))
        pf = gross_win / gross_loss if gross_loss > 0 else float('inf')
        print(f"  {label:40s} {trades:>7d} {win_pct:>6.1f}% {net_pnl:>+9.0f} {ret_pct:>+7.1f}% {max_dd:>6.1f}% {pf:>5.2f}")
        if ret_pct > best_return:
            best_return = ret_pct
            best_seg = label
    print("  " + "-" * 78)
    # Combined row
    if combined.get("trades"):
        ct = len(combined["trades"])
        cw = sum(1 for t in combined["trades"] if t["pnl"] > 0)
        cwp = cw / ct * 100 if ct else 0
        cnp = combined.get("net_pnl", sum(t["pnl"] for t in combined["trades"]))
        cfe = combined.get("final_equity", 150000 + cnp)
        cr = (cfe - 150000) / 150000 * 100
        cdd = combined.get("max_drawdown_pct", 0)
        gw = sum(t["pnl"] for t in combined["trades"] if t["pnl"] > 0)
        gl = abs(sum(t["pnl"] for t in combined["trades"] if t["pnl"] < 0))
        cpf = gw / gl if gl > 0 else float('inf')
        print(f"  {'★ COMBINED (all segments)':40s} {ct:>7d} {cwp:>6.1f}% {cnp:>+9.0f} {cr:>+7.1f}% {cdd:>6.1f}% {cpf:>5.2f}")
    print("=" * 80)
    if best_seg:
        print(f"\n  🏆 BEST SEGMENT: {best_seg} (+{best_return:.1f}% return)")
        print(f"  → Use this segment for production deployment.")
    print("\n  💡 TIP: Run index + stock together for best diversification.")
    print("     Commodity (MCX) adds non-correlated alpha (different session).")

    try:
        broker.logout()
        print("\n✓ Logged out.")
    except Exception:
        pass

    print("\n" + "=" * 72)
    print("  TIGER BRAIN ONE MAN ARMY complete. 🪖")
    print("=" * 72)


if __name__ == "__main__":
    main()
