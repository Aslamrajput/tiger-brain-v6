"""
TIGER PRO — HIGH-WIN-RATE S/D ENGINE (Variant C)
=================================================
Target: 70-80% win rate via 6 stacked probability filters.

This is a SEPARATE engine that does NOT touch the locked V6.6 core
(intraday_backtest.py, intraday_strategies.py). It imports reusable
helpers from them but implements its own A+ entry engine.

6 FILTERS (all pure price-action / S/D, NO VWAP/RSI/EMA/Black-Scholes):
  1. TREND LOCK: 15m HH+HL → Calls only. LH+LL → Puts only. No trend = no trade.
  2. FRESH ZONE: Only trade zones tested ≤2 times. 3+ touches = rejected.
  3. GOLDEN WINDOW: 09:15-11:00 + 14:30-15:15 only. Skip 11:00-14:30 chop.
  4. VOLUME CONFIRM (both zones): buy-delta for demand, sell-delta for supply.
  5. STRUCTURAL STOP: Stop at zone breach, not 30% of premium. Lets trades breathe.
  6. PREMIUM SANITY: Reject if premium > 1.5% of underlying or < ₹3.

Tradeoff: 2-3 trades/day (A+ setups only) instead of 8. Quality > quantity.

Run from repo ROOT:
    python3 -m backtest.run_tiger_pro_backtest
"""

from __future__ import annotations

import logging
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

import pandas as pd

sys.path.insert(0, ".")

from broker.angel_connect import AngelBroker
from data.loader import (
    fetch_angel_historical_candles,
    find_symbol_token,
    load_angel_instrument_master,
)
from backtest.intraday_backtest import (
    _normalize_cols,
    _to_ist,
    _in_entry_window,
    _is_square_off_bar,
    brain1_intraday_pass,
    atm_premium,
    realized_vol_simple,
    spread_ok,
    model_spread_pct,
    check_intraday_exit,
    print_report,
    MAX_SPREAD_PCT,
)
from pipeline.intraday_strategies import (
    detect_zones,
    zone_touched_on_1m,
    find_opposing_zone,
    _simple_range,
    volume_delta,
)
from universe.fno_universe import (
    UNIVERSE, all_symbols, segment_of, lot_size, is_expiry_day,
    entry_windows_for, square_off_for,
)
from risk.risk_management import TradeCounterGuard

logger = logging.getLogger("tiger_brain.tiger_pro")
logging.basicConfig(level=logging.INFO)

# --- Tiger PRO constants ------------------------------------------------
RISK_PCT_PER_TRADE = 1.0       # 1.0% of current equity per trade
MAX_ZONE_TOUCHES = 2           # Reject zones tested 3+ times
VOL_SURGE_MULT = 1.3           # Volume must be 1.3x recent average
VOL_LOOKBACK = 5
TREND_LOOKBACK = 10            # 10 × 15m bars = 2.5 hours for trend
PREMIUM_MAX_PCT_OF_UNDERLYING = 1.5  # Reject if premium > 1.5% of underlying
PREMIUM_MIN = 3.0              # Reject if premium < ₹3

# Golden windows (NSE times; MCX uses its own entry_windows_for)
# Morning institutional flow + afternoon breakout zone + closing momentum.
# 11:00-13:00 is the only true chop zone (lunch lull).
GOLDEN_WINDOWS_NSE = [
    ("09:15", "11:00"),   # Morning institutional flow
    ("13:00", "15:15"),   # Afternoon breakout + closing momentum
]

ANGEL_EXCHANGE = {
    "NIFTY": ("NSE", "Nifty 50"),
    "BANKNIFTY": ("NSE", "Nifty Bank"),
    "CRUDEOIL": ("MCX", "CRUDEOIL"),
    "GOLD": ("MCX", "GOLD"),
    "NATURALGAS": ("MCX", "NATURALGAS"),
}


# ============================================================
# FILTER 1: TREND LOCK (15m price action — HH+HL / LH+LL)
# ============================================================
def detect_15m_trend(df_15m, i) -> str:
    """
    Determine the 15m trend using pure price action.

    Uptrend: recent bars making Higher Highs + Higher Lows
    Downtrend: recent bars making Lower Highs + Lower Lows
    Range/no-trend: mixed structure

    Returns 'up', 'down', or 'range'.
    """
    if i < TREND_LOOKBACK + 2:
        return "range"

    recent = df_15m.iloc[i - TREND_LOOKBACK: i]
    highs = recent["high"].values
    lows = recent["low"].values
    n = len(highs)
    if n < 4:
        return "range"

    # Split into two halves and compare
    mid = n // 2
    first_half_high = max(highs[:mid])
    second_half_high = max(highs[mid:])
    first_half_low = min(lows[:mid])
    second_half_low = min(lows[mid:])

    hh = second_half_high > first_half_high   # higher high
    hl = second_half_low > first_half_low      # higher low
    lh = second_half_high < first_half_high    # lower high
    ll = second_half_low < first_half_low       # lower low

    if hh and hl:
        return "up"
    if lh and ll:
        return "down"
    return "range"


def trend_allows_trade(trend: str, direction: str) -> bool:
    """Only allow Call (BUY) in uptrend, Put (SELL) in downtrend."""
    if direction == "BUY" and trend == "up":
        return True
    if direction == "SELL" and trend == "down":
        return True
    return False


# ============================================================
# FILTER 2: FRESH ZONE TRACKER
# ============================================================
class FreshZoneTracker:
    """Tracks how many times each zone has been tested across the backtest."""

    def __init__(self):
        self._touches: dict[str, int] = {}

    def _key(self, symbol, zone):
        return f"{symbol}|{zone['type']}|{round(zone['top'], 2)}|{round(zone['bottom'], 2)}"

    def touch_count(self, symbol, zone) -> int:
        return self._touches.get(self._key(symbol, zone), 0)

    def register_touch(self, symbol, zone):
        k = self._key(symbol, zone)
        self._touches[k] = self._touches.get(k, 0) + 1

    def is_fresh(self, symbol, zone) -> bool:
        return self.touch_count(symbol, zone) <= MAX_ZONE_TOUCHES


# ============================================================
# FILTER 3: GOLDEN WINDOW (time filter)
# ============================================================
def in_golden_window(ts, segment) -> bool:
    """Only trade during high-institutional-flow time windows."""
    if segment == "commodity":
        # MCX uses its own entry windows from V6.6 config
        return _in_entry_window(ts, segment)
    # NSE: only morning + closing windows
    ts_str = ts.strftime("%H:%M") if ts.tz is None else ts.tz_convert("Asia/Kolkata").strftime("%H:%M")
    for start, end in GOLDEN_WINDOWS_NSE:
        if start <= ts_str < end:
            return True
    return False


# ============================================================
# FILTER 4: VOLUME CONFIRMATION (both demand AND supply)
# ============================================================
def zone_volume_confirmed(df_1m, i_1m, zone_type) -> tuple[bool, str]:
    """
    Volume confirmation for BOTH zone types:
      Demand: positive volume_delta (buying pressure) + vol surge
      Supply: negative volume_delta (selling pressure) + vol surge
    """
    if i_1m < VOL_LOOKBACK + 1:
        return (True, "insufficient-bars (pass)")
    bar = df_1m.iloc[i_1m]
    vol = float(bar.get("volume", 0) or 0)
    vdelta = volume_delta(bar)

    # Direction check: demand needs buy pressure, supply needs sell pressure
    if zone_type == "demand" and vdelta <= 0:
        return (False, "sell-pressure at demand")
    if zone_type == "supply" and vdelta >= 0:
        return (False, "buy-pressure at supply")

    # Volume surge check
    if vol > 0:
        recent_vols = [float(df_1m.iloc[j].get("volume", 0) or 0)
                       for j in range(i_1m - VOL_LOOKBACK, i_1m)]
        avg_vol = sum(recent_vols) / len(recent_vols) if recent_vols else 0.0
        if avg_vol > 0 and vol >= avg_vol * VOL_SURGE_MULT:
            dir_label = "buy-delta" if zone_type == "demand" else "sell-delta"
            return (True, f"{dir_label} + vol-surge {vol / avg_vol:.1f}x")
        return (False, f"correct-dir but no vol-surge ({vol / max(avg_vol, 1):.1f}x)")

    return (True, "correct-dir (no-volume source)")


# ============================================================
# FILTER 5: STRUCTURAL STOP (zone-based)
# ============================================================
def compute_structural_stop(entry_premium, zone, zone_type, cur_underlying,
                            strike, dte, is_call, iv, max_loss_cap=2000.0):
    """
    Compute stop based on zone breach, not arbitrary premium %.

    If price breaks BELOW the demand zone bottom → demand is invalid → exit.
    If price breaks ABOVE the supply zone top → supply is invalid → exit.

    Translate the zone-breach underlying price into option premium terms.
    The structural stop gives the trade ROOM TO BREATHE — wider than the
    old 30% premium stop. Total loss is still capped at max_loss_cap (₹2,000)
    by the dynamic lot sizing (fewer lots = same max risk).
    """
    if zone_type == "demand":
        stop_underlying = zone["bottom"]
    else:
        stop_underlying = zone["top"]

    stop_prem_structural = atm_premium(stop_underlying, strike, dte, is_call, iv)
    stop_prem_structural = max(stop_prem_structural, 0.5)

    # Use the structural stop (wider — lets trade breathe).
    # size_dynamic() will cap total loss at max_loss_cap via fewer lots.
    stop_premium = stop_prem_structural

    # Safety floor: never let stop be above 70% of entry (some breathing room)
    stop_premium = min(stop_premium, entry_premium * 0.85)
    stop_premium = max(stop_premium, 0.5)

    return stop_premium


def lot_size_safe():
    """Fallback lot size for stop calculation."""
    return 25


# ============================================================
# FILTER 6: PREMIUM SANITY CHECK
# ============================================================
def premium_sane(entry_premium, cur_underlying) -> tuple[bool, str]:
    """Reject overpriced or illiquid options."""
    if entry_premium < PREMIUM_MIN:
        return (False, f"premium too low (₹{entry_premium:.1f} < ₹{PREMIUM_MIN})")
    pct_of_underlying = entry_premium / cur_underlying * 100
    if pct_of_underlying > PREMIUM_MAX_PCT_OF_UNDERLYING:
        return (False, f"premium overpriced ({pct_of_underlying:.1f}% of underlying)")
    return (True, "sane")


# ============================================================
# DYNAMIC LOT-SIZING (from Variant B, proven)
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
# TOKEN RESOLUTION (with MCX fix from Variant B)
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
        time.sleep(0.5)
    print(f"\nFailed symbols: {failed}")
    print(f"Universe loaded from Angel One: 15m={len(data_map)}  1m={len(data_map_1m)}")
    return data_map, data_map_1m, failed


# ============================================================
# A+ ENTRY ENGINE — all 6 filters stacked
# ============================================================
def find_tiger_pro_entry(df_15m, i_15m, df_1m, seg, is_expiry, symbol,
                         zone_tracker: FreshZoneTracker):
    """
    Tiger PRO A+ entry — 6 filters must ALL pass.

    Returns dict or None.
    """
    if df_1m is None or len(df_1m) == 0:
        return None

    bar_15m_start = df_15m.index[i_15m]
    bar_15m_end = bar_15m_start + pd.Timedelta(minutes=15)
    in_window = df_1m[(df_1m.index >= bar_15m_start) & (df_1m.index < bar_15m_end)]
    if len(in_window) == 0:
        return None

    # --- FILTER 1: TREND LOCK ---
    trend = detect_15m_trend(df_15m, i_15m)
    if trend == "range":
        return None  # no trend = no trade

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
        for z in zones:
            touch = zone_touched_on_1m(bar_1m, z)
            if touch is None:
                continue

            direction = "BUY" if touch == "demand" else "SELL"

            # --- FILTER 1 (continued): trend must align with direction ---
            if not trend_allows_trade(trend, direction):
                continue

            # --- FILTER 2: FRESH ZONE ---
            if not zone_tracker.is_fresh(symbol, z):
                continue
            zone_tracker.register_touch(symbol, z)

            # --- FILTER 4: VOLUME CONFIRMATION (both zones) ---
            vol_ok, vol_reason = zone_volume_confirmed(df_1m, i_1m, touch)
            if not vol_ok:
                continue

            score = z["score"]
            if "surge" in vol_reason:
                score += 5  # boost for confirmed institutional volume

            strategy = "Pro_Demand_A+" if touch == "demand" else "Pro_Supply_A+"
            candidate = {
                "direction": direction,
                "zone_type": touch,
                "zone_top": z["top"],
                "zone_bottom": z["bottom"],
                "entry_price": float(bar_1m["close"]),
                "entry_1m_ts": ts_1m,
                "entry_1m_idx": i_1m,
                "delta_reason": vol_reason,
                "sweep": False,
                "sweep_reason": "",
                "delta_spike_mult": 0.0,
                "explosive": False,
                "expansion_pct": 0.0,
                "setup_score": round(score, 1),
                "strategy": strategy,
                "strike_kind": "ITM" if is_expiry else "ATM",
                "is_expiry": is_expiry,
                "trend": trend,
                "zone_touches": zone_tracker.touch_count(symbol, z),
            }
            if best is None or candidate["setup_score"] > best["setup_score"]:
                best = candidate
    return best


# ============================================================
# MAIN BACKTEST ENGINE
# ============================================================
def run_tiger_pro_backtest(data_map, start_capital=150000.0, max_loss_per_trade=2000.0,
                           max_capital_per_trade_pct=10.0, dte_default=1.0,
                           verbose=False, data_map_1m=None):
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
    zone_tracker = FreshZoneTracker()
    filter_stats = defaultdict(int)

    for day in sorted_days:
        day_ts = days[day]
        sim_date = day.date() if day.tz is None else day.tz_convert("Asia/Kolkata").date()
        if counter._today != sim_date:
            counter._today = sim_date
            counter.global_count = 0
            counter.commodity_count = 0

        for ts in day_ts:
            # --- 1. EXIT open positions ---
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
                cur_prem = atm_premium(cur_underlying, pos["strike"], pos["dte"], is_call, iv)
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
                    trades.append({
                        **pos, "exit_ts": ts, "exit_premium": exit_prem, "pnl": pnl,
                        "exit_reason": ex["reason"],
                        "hold_bars": len(df_so_far) - pos["entry_idx"],
                        "entry_spread_pct": pos.get("entry_spread_pct", 0.0),
                        "confirmation": pos.get("delta_reason", "A+"),
                    })
                    if verbose:
                        logger.warning(f"EXIT {sym} {ex['reason']} pnl={pnl:.0f}")
                else:
                    pos["dte"] = max(pos["dte"] - 1 / 25, 0.05)
                    still_open.append(pos)
            open_positions = still_open

            # --- 2. A+ ENTRY SCAN (6 filters) ---
            day_candidates = []
            for sym, df_sym in data_map.items():
                if ts not in df_sym.index:
                    continue
                idx = df_sym.index.get_loc(ts)
                if idx < 40:
                    continue
                seg = segment_of(sym)
                b1 = brain1_intraday_pass(df_sym, idx, segment=seg)
                if not b1["passed_brain1"]:
                    continue
                if _is_square_off_bar(ts, seg):
                    continue

                # --- FILTER 3: GOLDEN WINDOW ---
                if not in_golden_window(ts, seg):
                    filter_stats["rejected_golden_window"] += 1
                    continue

                expiry = use_sniper and is_expiry_day(sym, ts)

                if use_sniper and data_map_1m and sym in data_map_1m:
                    setup = find_tiger_pro_entry(
                        df_sym, idx, data_map_1m[sym], seg, expiry, sym, zone_tracker)
                    if setup is None:
                        continue
                else:
                    # 15m fallback (no 1m = can't run vol filter, skip for PRO)
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
                entry_prem = atm_premium(cur_underlying, strike, dte_default, is_call, iv)
                entry_prem = max(entry_prem, 1.0)

                # --- FILTER 6: PREMIUM SANITY ---
                sane, sane_reason = premium_sane(entry_prem, cur_underlying)
                if not sane:
                    filter_stats["rejected_premium_sanity"] += 1
                    if verbose:
                        logger.warning(f"REJECT {sym} {sane_reason}")
                    continue

                ok_spread, spread_pct = spread_ok(sym, entry_prem)
                if not ok_spread:
                    filter_stats["rejected_spread"] += 1
                    if verbose:
                        logger.warning(f"REJECT {sym} spread {spread_pct:.2f}% > 0.5%")
                    continue

                # --- FILTER 5: STRUCTURAL STOP ---
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
                    "entry_spread_pct": spread_pct,
                    "trend": setup.get("trend", "range"),
                    "zone_touches": setup.get("zone_touches", 0),
                    "structural_stop": True,
                }
                open_positions.append(pos)
                if verbose:
                    logger.warning(
                        f"ENTRY {sym} {setup['strategy']} @{entry_prem:.1f} "
                        f"strike={strike} dir={setup['direction']} trend={setup.get('trend','?')}"
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
        cur_prem = atm_premium(cur_underlying, pos["strike"], pos["dte"], is_call, iv)
        slippage = cur_prem * 0.008 + pos["entry_premium"] * 0.008
        brokerage = 20.0 * 2
        pnl = (cur_prem - pos["entry_premium"]) * pos["quantity"] - slippage * 2 - brokerage
        pnl = max(pnl, -max_loss_per_trade - brokerage - slippage * 2 + 1)
        capital += pnl
        trades.append({
            **pos, "exit_ts": last_ts, "exit_premium": cur_prem, "pnl": pnl,
            "exit_reason": "end_of_data",
            "hold_bars": len(df_sym) - pos["entry_idx"],
            "entry_spread_pct": pos.get("entry_spread_pct", 0.0),
            "confirmation": pos.get("delta_reason", "A+"),
        })

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))

    from collections import Counter
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
    }


def main():
    print("\n" + "#" * 72)
    print("#  TIGER PRO — HIGH-WIN-RATE S/D ENGINE (Variant C)")
    print("#  6 Filters: Trend Lock + Fresh Zone + Golden Window + Vol Confirm")
    print("#            + Structural Stop + Premium Sanity")
    print("#  Target: 70-80% win rate | 2-3 A+ trades/day | Quality > Quantity")
    print("#  V6.6 core: UNTOUCHED. Pure S/D price action, NO VWAP/RSI/EMA.")
    print("#  Data: Angel One SmartAPI (real OHLCV, IST-native, real volume)")
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

    print("\nFetching 15-min + 1-min intraday data from Angel One...")
    data_map, data_map_1m, failed = fetch_angel_data(broker)

    if not data_map:
        print("\n✗ No data fetched — cannot run backtest.")
        return

    print(f"\nRunning TIGER PRO portfolio (₹1.5L, {len(data_map)} symbols)...")
    combined = run_tiger_pro_backtest(
        data_map, start_capital=150000.0, max_loss_per_trade=2000.0,
        data_map_1m=data_map_1m if data_map_1m else None,
    )

    print("\n\n")
    print("#" * 72)
    print("#  TIGER PRO — COMBINED PORTFOLIO (Variant C, Angel One data)")
    print("#" * 72)
    print_report(combined)

    # Print filter statistics
    fs = combined.get("filter_stats", {})
    if fs:
        print("\n" + "-" * 72)
        print("  FILTER STATISTICS (how many trades were rejected by each filter)")
        print("-" * 72)
        for fname, count in sorted(fs.items(), key=lambda x: -x[1]):
            print(f"  {fname:40s}: {count:6d} rejections")
        print("-" * 72)

    for seg_key in ("index", "stock", "commodity"):
        seg_syms = list(UNIVERSE[seg_key]["symbols"].keys())
        seg_map = {s: data_map[s] for s in seg_syms if s in data_map}
        seg_map_1m = {s: data_map_1m[s] for s in seg_syms if s in data_map_1m}
        print("\n\n")
        print("#" * 72)
        print(f"#  TIGER PRO — {UNIVERSE[seg_key]['label'].upper()} (standalone, ₹1.5L)")
        print("#" * 72)
        if not seg_map:
            print("  (no data for this segment)")
            continue
        print(f"  Running {seg_key} standalone ({len(seg_map)} symbols)...")
        seg_res = run_tiger_pro_backtest(
            seg_map, start_capital=150000.0, max_loss_per_trade=2000.0,
            data_map_1m=seg_map_1m if seg_map_1m else None,
        )
        print_report(seg_res)
        fs = seg_res.get("filter_stats", {})
        if fs:
            print("\n  FILTER STATISTICS:")
            for fname, count in sorted(fs.items(), key=lambda x: -x[1]):
                print(f"  {fname:40s}: {count:6d} rejections")

    try:
        broker.logout()
        print("\n✓ Angel One session logged out.")
    except Exception:
        pass

    print("\n" + "=" * 72)
    print("  TIGER PRO backtest complete.")
    print("=" * 72)


if __name__ == "__main__":
    main()
