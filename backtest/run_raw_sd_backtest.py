"""
RAW S/D ZONE-TOUCH BACKTEST — MULTI-ASSET ENGINE (Variant B, Upgraded)
=====================================================================
A separate, standalone backtest that enters on PURE zone touches.

UPGRADED FEATURES (v2):
  1. LOCALIZED DEMAND-ZONE FILTER: Supply zones remain RAW (proven 50%
     win rate). Demand zones get a lightweight volume/momentum confirmation
     to eliminate false bounces (the 16.7% win-rate weakness). NOT the
     full V6.6 1.8x delta-spike gate — just: bullish 1m bar OR volume
     surge >= 1.2x at the touch.
  2. DYNAMIC LOT-SIZING: Position size scales with real-time account
     balance. Risk per trade = min(current_equity * risk_pct, ₹2,000 hard
     stop). Anti-martingale: sizes grow as account grows, shrink on losses.
  3. MULTI-ASSET: NSE index/stock options + MCX commodity options with
     correct session timing (MCX 09:00-11:30 & 17:00-23:00).

CORE RULES (unchanged):
  - PURE S/D SCANNER: detect_zones() — institutional supply/demand zones.
  - Demand touch → BUY ATM Call. Supply touch → BUY ATM Put.
  - ₹2,000 hard stop per trade + 15:15 / 23:15 auto square-off.
  - 5-10 trades/day aggressive across Index, Stock, Commodity.

This file does NOT modify the locked V6.6 core (intraday_backtest.py,
intraday_strategies.py). It imports their reusable helper functions
(detect_zones, zone_touched_on_1m, atm_premium, check_intraday_exit, etc.)
but implements its own entry engine.

Run from repo ROOT:
    python3 -m backtest.run_raw_sd_backtest
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
    size_with_hard_stop,
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
)
from risk.risk_management import TradeCounterGuard

logger = logging.getLogger("tiger_brain.raw_sd_backtest")
logging.basicConfig(level=logging.INFO)

# Angel One exchange + symbol-name mapping.
# MCX commodities use specific futures contract names — the plain name
# (e.g. "CRUDEOIL") resolves to a non-tradable composite. We search for
# the active near-month futures contract instead.
ANGEL_EXCHANGE = {
    "NIFTY": ("NSE", "Nifty 50"),
    "BANKNIFTY": ("NSE", "Nifty Bank"),
    "CRUDEOIL": ("MCX", "CRUDEOIL"),
    "GOLD": ("MCX", "GOLD"),
    "NATURALGAS": ("MCX", "NATURALGAS"),
}

# --- Upgraded engine constants ------------------------------------------
# Dynamic lot-sizing: risk this fraction of current equity per trade,
# capped at the ₹2,000 hard stop. Anti-martingale (sizes scale with P&L).
RISK_PCT_PER_TRADE = 1.5  # 1.5% of current account balance

# Demand-zone localized filter thresholds (supply zones stay RAW).
# A demand touch is confirmed if EITHER:
#   (a) the 1m touch bar is bullish (close > open) — momentum up, OR
#   (b) the 1m touch bar volume >= 1.2x the 5-bar average — volume surge.
DEMAND_VOL_SURGE_MULT = 1.2
DEMAND_VOL_LOOKBACK = 5


def demand_zone_confirmed(df_1m, i_1m) -> tuple[bool, str]:
    """
    Localized volume/momentum filter for DEMAND zones only.

    Checks whether the 1m bar touching a demand zone shows institutional
    buying pressure. This is NOT the V6.6 1.8x delta-spike gate — it's a
    lighter check to reject obvious false bounces (dead-cat bounces on
    zero-volume touches).

    Pass condition (either):
      - Momentum: close > open on the touch bar (bullish body), OR
      - Volume surge: bar volume >= 1.2x the average of the last 5 bars.

    Returns (confirmed, reason).
    """
    if i_1m < DEMAND_VOL_LOOKBACK + 1:
        return (True, "insufficient-bars (pass)")
    bar = df_1m.iloc[i_1m]
    close, opn = float(bar["close"]), float(bar["open"])
    vol = float(bar.get("volume", 0) or 0)

    # (a) Momentum: bullish 1m bar
    if close > opn:
        return (True, "bullish-momentum")

    # (b) Volume surge: current vol >= 1.2x recent average
    if vol > 0:
        recent_vols = [float(df_1m.iloc[j].get("volume", 0) or 0)
                       for j in range(i_1m - DEMAND_VOL_LOOKBACK, i_1m)]
        avg_vol = sum(recent_vols) / len(recent_vols) if recent_vols else 0.0
        if avg_vol > 0 and vol >= avg_vol * DEMAND_VOL_SURGE_MULT:
            return (True, f"vol-surge {vol / avg_vol:.1f}x")

    return (False, "no-buy-confirmation")


def size_dynamic(entry_premium, stop_premium, lot_sz, current_capital,
                 max_loss_cap=2000.0, risk_pct=RISK_PCT_PER_TRADE):
    """
    Dynamic lot-sizing based on real-time account balance.

    Risk per trade = min(current_capital * risk_pct, max_loss_cap).
    This scales position size with equity: after wins, sizes grow;
    after losses, sizes shrink. Always capped at the ₹2,000 hard stop.

    Returns dict compatible with size_with_hard_stop output:
      {lots, quantity, max_loss, stop_per_unit, risk_amount}
    """
    stop_per_unit = abs(entry_premium - stop_premium)
    if stop_per_unit <= 0:
        return {"lots": 1, "quantity": lot_sz, "max_loss": entry_premium * lot_sz,
                "stop_per_unit": 0.0, "risk_amount": 0.0}

    # Dynamic risk amount: fraction of current equity, capped at hard stop
    risk_amount = min(current_capital * risk_pct / 100.0, max_loss_cap)

    loss_per_lot = stop_per_unit * lot_sz
    lots = max(1, int(risk_amount // loss_per_lot))
    quantity = lots * lot_sz
    actual_risk = stop_per_unit * quantity
    return {"lots": lots, "quantity": quantity, "max_loss": actual_risk,
            "stop_per_unit": stop_per_unit, "risk_amount": round(actual_risk, 2)}


def _resolve_symbol_token(symbol: str) -> tuple[str, str] | None:
    """Map a universe symbol to (exchange, symboltoken) via instrument master."""
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

    # MCX commodities: Angel One lists futures contracts (e.g. CRUDEOILMCOM,
    # GOLDMCOM, NATURALGAS27OCT26FUT). Prefer the near-month "MCOM" composite
    # if available (continuous front-month), else the first futures contract.
    if exchange == "MCX":
        mcom = matches[matches["symbol"].str.upper().str.contains("MCOM", na=False)]
        if not mcom.empty:
            row = mcom.iloc[0]
        else:
            # fall back to first futures-like entry (contains FUT or the base name)
            fut = matches[matches["symbol"].str.upper().str.contains(
                "FUT", na=False) | matches["symbol"].str.upper().str.startswith(sym_upper)]
            row = fut.iloc[0] if not fut.empty else matches.iloc[0]
        token = str(row["token"])
        logger.info(f"{symbol}: {exchange} token={token} ({row['symbol']})")
        return exchange, token

    # NSE stocks: prefer exact "{SYMBOL}-EQ" match
    exact_eq = matches[matches["symbol"].str.upper() == f"{sym_upper}-EQ"]
    if exact_eq.empty:
        exact_eq = matches[matches["symbol"].str.upper() == sym_upper]
    if exact_eq.empty:
        exact_eq = matches[matches["symbol"].str.upper().str.endswith("-EQ")]
    row = exact_eq.iloc[0] if not exact_eq.empty else matches.iloc[0]
    token = str(row["token"])
    logger.info(f"{symbol}: {exchange} token={token} ({row['symbol']})")
    return exchange, token


def fetch_angel_data(broker, days_15m: int = 60, days_1m: int = 7):
    """Fetch 15m + 1m OHLCV for the full universe from Angel One."""
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
                broker, exchange, token, "FIFTEEN_MINUTE", from_15m, to_date,
            )
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
                broker, exchange, token, "ONE_MINUTE", from_1m, to_date,
            )
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


def find_raw_zone_touch_entry(df_15m, i_15m, df_1m, seg, is_expiry=False):
    """
    RAW zone-touch entry with localized demand-zone filter.

    SUPPLY zones: RAW entry (no filter — proven 50% win rate).
    DEMAND zones: must pass demand_zone_confirmed() (bullish 1m bar OR
    volume surge >= 1.2x) to eliminate false bounces.

    NO explosive-quality gate, NO 1.8x delta-spike gate, NO liquidity sweep.

    Returns dict (same shape as V6.6 sniper) or None.
    """
    if df_1m is None or len(df_1m) == 0:
        return None

    bar_15m_start = df_15m.index[i_15m]
    bar_15m_end = bar_15m_start + pd.Timedelta(minutes=15)
    in_window = df_1m[(df_1m.index >= bar_15m_start) & (df_1m.index < bar_15m_end)]
    if len(in_window) == 0:
        return None

    # Detect zones using the FULL available 15m history (no lookahead,
    # no explosive filter — detect_zones returns ALL institutional zones).
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

            # DEMAND zones: apply localized volume/momentum filter.
            # SUPPLY zones: raw entry (no filter).
            demand_reason = "raw-touch"
            if touch == "demand":
                confirmed, d_reason = demand_zone_confirmed(df_1m, i_1m)
                if not confirmed:
                    continue  # reject false bounce
                demand_reason = d_reason

            direction = "BUY" if touch == "demand" else "SELL"
            score = z["score"]
            # Small score boost for confirmed demand entries
            if touch == "demand" and "raw" not in demand_reason:
                score += 2
            strike_kind = "ITM" if is_expiry else "ATM"
            strategy = ("Demand_Confirmed" if touch == "demand"
                        else "Raw_Supply_Touch")
            candidate = {
                "direction": direction,
                "zone_type": touch,
                "zone_top": z["top"],
                "zone_bottom": z["bottom"],
                "entry_price": float(bar_1m["close"]),
                "entry_1m_ts": ts_1m,
                "entry_1m_idx": i_1m,
                "delta_reason": demand_reason if touch == "demand" else "raw-supply-touch",
                "sweep": False,
                "sweep_reason": "",
                "delta_spike_mult": 0.0,
                "explosive": False,
                "expansion_pct": 0.0,
                "setup_score": round(score, 1),
                "strategy": strategy,
                "strike_kind": strike_kind,
                "is_expiry": is_expiry,
            }
            if best is None or candidate["setup_score"] > best["setup_score"]:
                best = candidate
    return best


def run_raw_sd_backtest(data_map, start_capital=150000.0, max_loss_per_trade=2000.0,
                        max_capital_per_trade_pct=10.0, dte_default=1.0,
                        verbose=False, data_map_1m=None):
    """
    Raw S/D zone-touch backtest engine.
    Same walk-forward structure as run_intraday_backtest, but entry uses
    find_raw_zone_touch_entry (no confirmation gates).
    """
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
                        "confirmation": "raw-touch",
                    })
                    if verbose:
                        logger.warning(f"EXIT {sym} {ex['reason']} pnl={pnl:.0f}")
                else:
                    pos["dte"] = max(pos["dte"] - 1 / 25, 0.05)
                    still_open.append(pos)
            open_positions = still_open

            # --- 2. RAW ZONE-TOUCH SCAN + ENTER ---
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
                expiry = use_sniper and is_expiry_day(sym, ts)

                if use_sniper and data_map_1m and sym in data_map_1m:
                    setup = find_raw_zone_touch_entry(
                        df_sym, idx, data_map_1m[sym], seg, is_expiry=expiry,
                    )
                    if setup is None:
                        continue
                else:
                    # 15m fallback: raw zone touch on the previous 15m bar.
                    # Demand filter can't run (no 1m data) — accept raw touch.
                    setups = detect_zones(df_sym, idx, lookback=40)
                    if not setups:
                        continue
                    z = max(setups, key=lambda s: s["score"])
                    touch = "demand" if z["type"] == "demand" else "supply"
                    setup = {
                        "direction": "BUY" if touch == "demand" else "SELL",
                        "zone_type": touch,
                        "zone_top": z["top"], "zone_bottom": z["bottom"],
                        "entry_price": float(df_sym.iloc[idx]["close"]),
                        "setup_score": z["score"],
                        "strategy": "Demand_Confirmed" if touch == "demand" else "Raw_Supply_Touch",
                        "strike_kind": "ITM" if expiry else "ATM",
                        "is_expiry": expiry,
                        "delta_reason": "raw-15m-touch",
                        "sweep": False, "delta_spike_mult": 0.0,
                        "explosive": False, "expansion_pct": 0.0,
                    }
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

                ok_spread, spread_pct = spread_ok(sym, entry_prem)
                if not ok_spread:
                    if verbose:
                        logger.warning(f"REJECT {sym} spread {spread_pct:.2f}% > 0.5%")
                    continue

                stop_prem = max(entry_prem * 0.70, 0.5)
                lot_sz = lot_size(sym)
                # DYNAMIC LOT-SIZING: risk scales with current account balance
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
                }
                open_positions.append(pos)
                if verbose:
                    logger.warning(
                        f"ENTRY {sym} {setup['strategy']} @{entry_prem:.1f} "
                        f"strike={strike} dir={setup['direction']}"
                    )

            equity_curve.append(capital)
            if capital > peak_equity:
                peak_equity = capital
            dd = (peak_equity - capital) / peak_equity * 100
            if dd > max_dd:
                max_dd = dd

    # Close any remaining open positions at the last available bar
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
            "confirmation": "raw-touch",
        })

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))

    # Daily trade counts
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

    # Segment stats
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

    # Strategy stats (by strategy name)
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
    }


def main():
    print("\n" + "#" * 72)
    print("#  TIGER BRAIN V6.6 — RAW S/D MULTI-ASSET ENGINE (Variant B, v2)")
    print("#  Upgraded: demand-zone filter + dynamic lot-sizing + MCX commodities")
    print("#  Supply zones: RAW touch. Demand zones: vol/momentum confirmed.")
    print("#  Lot sizing: 1.5% of real-time equity, capped at ₹2,000 stop.")
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

    print(f"\nRunning RAW S/D portfolio (₹1.5L, {len(data_map)} symbols)...")
    combined = run_raw_sd_backtest(
        data_map, start_capital=150000.0, max_loss_per_trade=2000.0,
        data_map_1m=data_map_1m if data_map_1m else None,
    )

    print("\n\n")
    print("#" * 72)
    print("#  RAW S/D — COMBINED PORTFOLIO (Variant B, Angel One data)")
    print("#" * 72)
    print_report(combined)

    for seg_key in ("index", "stock", "commodity"):
        seg_syms = list(UNIVERSE[seg_key]["symbols"].keys())
        seg_map = {s: data_map[s] for s in seg_syms if s in data_map}
        seg_map_1m = {s: data_map_1m[s] for s in seg_syms if s in data_map_1m}
        print("\n\n")
        print("#" * 72)
        print(f"#  RAW S/D — {UNIVERSE[seg_key]['label'].upper()} (standalone, ₹1.5L)")
        print("#" * 72)
        if not seg_map:
            print("  (no data for this segment)")
            continue
        print(f"  Running {seg_key} standalone ({len(seg_map)} symbols)...")
        seg_res = run_raw_sd_backtest(
            seg_map, start_capital=150000.0, max_loss_per_trade=2000.0,
            data_map_1m=seg_map_1m if seg_map_1m else None,
        )
        print_report(seg_res)

    try:
        broker.logout()
        print("\n✓ Angel One session logged out.")
    except Exception:
        pass

    print("\n" + "=" * 72)
    print("  RAW S/D backtest complete.")
    print("=" * 72)


if __name__ == "__main__":
    main()
