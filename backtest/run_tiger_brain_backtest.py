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
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import pandas as pd

sys.path.insert(0, ".")

from broker.angel_connect import AngelBroker
from data.loader import (
    fetch_angel_historical_candles,
    fetch_india_vix_history,
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
MAX_ENTRIES_PER_DAY = 2     # top 2 entries by score per day (sniper quality)

# Score thresholds
MIN_SCORE_TO_ENTER = 68     # need 2-3 confluence bonuses to qualify (was 58 — too noisy)
EXPLOSIVE_BONUS = 10
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

ANGEL_EXCHANGE = {
    "NIFTY": ("NSE", "Nifty 50"),
    "BANKNIFTY": ("NSE", "Nifty Bank"),
    "CRUDEOIL": ("MCX", "CRUDEOIL"),
    "GOLD": ("MCX", "GOLD"),
    "NATURALGAS": ("MCX", "NATURALGAS"),
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
    target_date = date if not hasattr(date, 'date') else date.date() if date.tz is None else date.tz_convert("Asia/Kolkata").date()
    try:
        mask = _vix_cache.index.date == target_date
        if mask.any():
            return float(_vix_cache.loc[mask].iloc[-1][close_col])
    except Exception:
        pass
    return VIX_FALLBACK


# --- PCR (Put-Call Ratio) ---
def fetch_pcr(broker, underlying: str) -> float:
    try:
        from data.loader import fetch_option_chain_oi
        chain = fetch_option_chain_oi(broker, underlying=underlying, strikes_around_atm=15)
        if chain is None or chain.empty:
            return PCR_FALLBACK
        ce_oi = float(chain["CE_oi"].sum()) if "CE_oi" in chain.columns else 0
        pe_oi = float(chain["PE_oi"].sum()) if "PE_oi" in chain.columns else 0
        if ce_oi <= 0:
            return PCR_FALLBACK
        return pe_oi / ce_oi
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
def find_tiger_brain_entry(df_15m, i_15m, df_1m, seg, is_expiry, symbol,
                           broker, pcr_cache, vix_val):
    """
    Tiger Brain unified entry — scoring system, not gating.

    HARD GATES (must pass):
      1. Zone touch on 1m
      2. Volume delta in correct direction (1.3x)

    SCORE BOOSTERS (add to score, rank by score):
      All 5 brains contribute as scorers.
    """
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

            # --- Brain 2 booster: Explosive quality ---
            try:
                is_exp, exp_pct = zone_explosive_quality(
                    df_15m, z.get("bar_idx", zone_idx), touch)
                if is_exp:
                    score += EXPLOSIVE_BONUS
                    score_details.append(f"explosive(+{EXPLOSIVE_BONUS})")
            except Exception:
                is_exp, exp_pct = False, 0.0

            # --- Brain 2 booster: Delta spike (1.8x = bonus, not required) ---
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

            # === MINIMUM SCORE CHECK ===
            if score < MIN_SCORE_TO_ENTER:
                continue

            # --- Strike selection ---
            strike_kind = "ITM" if (spike_mult >= 3.0 or swept or is_exp or is_expiry) else "ATM"

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
    # Tighter cap: 60% of entry (was 85% — too wide, caused 28% drawdown)
    stop_prem = min(stop_prem, entry_premium * 0.60)
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
        mcom = matches[matches["symbol"].str.upper().str.contains("MCOM", na=False)]
        if not mcom.empty:
            row = mcom.iloc[0]
        else:
            fut = matches[matches["symbol"].str.upper().str.contains(
                "FUT", na=False) | matches["symbol"].str.upper().str.startswith(sym_upper)]
            row = fut.iloc[0] if not fut.empty else matches.iloc[0]
        return exchange, str(row["token"])
    exact_eq = matches[matches["symbol"].str.upper() == f"{sym_upper}-EQ"]
    if exact_eq.empty:
        exact_eq = matches[matches["symbol"].str.upper() == sym_upper]
    if exact_eq.empty:
        exact_eq = matches[matches["symbol"].str.upper().str.endswith("-EQ")]
    row = exact_eq.iloc[0] if not exact_eq.empty else matches.iloc[0]
    return exchange, str(row["token"])


def fetch_angel_data(broker, days_15m=60, days_1m=60):
    to_date = datetime.now().replace(hour=15, minute=30, second=0, microsecond=0)
    from_15m = to_date - timedelta(days=days_15m)
    from_1m = to_date - timedelta(days=days_1m)
    data_map, data_map_1m = {}, {}
    failed = []
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
            logger.error(f"{tag}: 15m error: {exc}")
            failed.append(sym)
            continue
        try:
            d1 = fetch_angel_historical_candles(
                broker, exchange, token, "ONE_MINUTE", from_1m, to_date)
            if d1 is not None and not d1.empty:
                data_map_1m[sym] = _normalize_cols(d1)
        except Exception as exc:
            logger.warning(f"{tag}: 1m error: {exc}")
        print(f"  {tag:30s}: 15m={len(data_map[sym]):5d}  1m={len(data_map_1m.get(sym, [])):5d}")
        time.sleep(0.4)
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
                             data_map_1m=None, broker=None):
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
                iv = max(min(realized_vol_simple(df_so_far), 0.80), 0.12)
                is_call = pos["option_type"] == "CE"
                cur_prem = bs_premium(cur_underlying, pos["strike"], pos["dte"], is_call, iv)
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

                if use_sniper and data_map_1m and sym in data_map_1m:
                    setup = find_tiger_brain_entry(
                        df_sym, idx, data_map_1m[sym], seg, expiry, sym,
                        broker, pcr_cache, vix_val)
                    if setup is None:
                        filter_stats["rejected_low_score_or_volume"] += 1
                        continue
                    brain_log["entry_scored"] += 1
                else:
                    continue

                day_candidates.append({
                    "symbol": sym, "setup": setup, "df_sym": df_sym,
                    "idx": idx, "ts": ts, "seg": seg, "expiry": expiry,
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
                iv = max(min(realized_vol_simple(df_so_far), 0.80), 0.12)
                is_call = setup["direction"] == "BUY"

                strike_kind = setup.get("strike_kind", "ATM")
                delta_in_reason = "delta" in setup.get("delta_reason", "")
                if (expiry and delta_in_reason) or strike_kind == "ITM":
                    strike = round(cur_underlying * 0.99) if is_call else round(cur_underlying * 1.01)
                else:
                    strike = round(cur_underlying)

                entry_prem = bs_premium(cur_underlying, strike, dte_default, is_call, iv)
                entry_prem = max(entry_prem, 1.0)

                sane, sane_reason = premium_sane(entry_prem, cur_underlying)
                if not sane:
                    filter_stats["rejected_premium_sanity"] += 1
                    continue

                ok_spread, spread_pct = spread_ok(sym, entry_prem)
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
                }
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
        iv = max(min(realized_vol_simple(df_sym), 0.80), 0.12)
        is_call = pos["option_type"] == "CE"
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

    print("\nFetching 60-day data from Angel One...")
    data_map, data_map_1m, failed = fetch_angel_data(broker)
    if not data_map:
        print("\n✗ No data — cannot run.")
        return

    print(f"\nRunning TIGER BRAIN ONE MAN ARMY (₹1.5L, {len(data_map)} symbols)...")
    combined = run_tiger_brain_backtest(
        data_map, start_capital=150000.0, max_loss_per_trade=2000.0,
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
            continue
        seg_res = run_tiger_brain_backtest(
            seg_map, start_capital=150000.0, max_loss_per_trade=2000.0,
            data_map_1m=seg_map_1m if seg_map_1m else None, broker=broker)
        print_report(seg_res)

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
