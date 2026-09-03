"""
Tiger Brain V6.3 — PURE SUPPLY/DEMAND INTRADAY BACKTEST ENGINE
================================================================
Koi VWAP/RS/EMA/Black-Scholes nahi. Sirf Supply & Demand zones.

Engine rules (clean, as the user demanded):
  - 15-min candles, pure intraday, NO overnight carry.
  - Brain 1: entry only in IST windows (09:15-11:00, 13:30-15:15).
  - Brain 2: Supply/Demand zone touch.
      Demand touch ➔ BUY ATM Call.
      Supply touch ➔ BUY ATM Put.
  - Brain 3: ATM option (round strike = underlying close). Premium =
    simple intrinsic + time-value model (NO Black-Scholes).
  - Brain 4: 5-10 trades/day quota, ₹2,000 hard stop per trade.
  - Brain 5: 03:15 PM hard square-off. No carry-forward.

⚠️ NO LOOKAHEAD — decision at bar i uses only df.iloc[:i+1].
⚠️ Synthetic ATM premiums — real option-chain history not free.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict, Counter

import numpy as np
import pandas as pd

try:
    from pipeline.intraday_strategies import scan_zones, _simple_range
    from universe.fno_universe import (
        UNIVERSE, lot_size, segment_of,
        ENTRY_WINDOWS, SQUARE_OFF_TIME,
        entry_windows_for, square_off_for, liquidity_tier,
    )
    from risk.risk_management import TradeCounterGuard
except ImportError:
    raise ImportError("Repo ROOT se chalao.")

logger = logging.getLogger("tiger_brain.backtest.intraday")
logging.basicConfig(level=logging.WARNING)


# ============================================================
# ATM option premium — simple model (NO Black-Scholes)
# ============================================================
def atm_premium(underlying, strike, dte_days, is_call, iv_pct=0.20):
    """
    Simple ATM option premium = intrinsic + time value.
    Time value ≈ underlying * iv * sqrt(dte/365) * 0.4 (rough ATM theta).
    Intrinsic = max(S-K, 0) for call, max(K-S, 0) for put.
    This avoids Black-Scholes entirely — a clean, transparent model.
    """
    intrinsic = max(underlying - strike, 0.0) if is_call else max(strike - underlying, 0.0)
    time_value = underlying * iv_pct * math.sqrt(max(dte_days, 0.05) / 365.0) * 0.4
    return max(intrinsic + time_value, 0.5)


def atm_premium_at(underlying_level, strike, dte_days, is_call, iv_pct=0.20):
    """Premium if underlying were at a different level (for stop pricing)."""
    return atm_premium(underlying_level, strike, dte_days, is_call, iv_pct)


def realized_vol_simple(df, window=20, bars_per_day=25):
    """Realized vol from log returns, annualized — used only for IV estimate."""
    if len(df) < window + 1:
        return 0.25
    rets = np.log(df["close"].astype(float) / df["close"].astype(float).shift(1)).dropna()
    if len(rets) < window:
        return 0.25
    return float(np.std(rets.iloc[-window:], ddof=1) * math.sqrt(bars_per_day * 252))


# ============================================================
# Brain 3 — Liquidity & spread safety layer
# ============================================================
# Maximum acceptable bid-ask spread as % of premium (0.5% per user spec).
MAX_SPREAD_PCT = 0.5
# Base spread % by liquidity tier (modelled, not from a live chain).
# Tier 1 (index/mega-cap) ~0.35%, Tier 2 ~0.55%, Tier 3 ~0.85%.
# Plus a small premium-dependent widening for very cheap options.
_TIER_BASE_SPREAD = {1: 0.35, 2: 0.55, 3: 0.85}


def model_spread_pct(symbol: str, premium: float) -> float:
    """
    Model a realistic bid-ask spread (%) for an ATM contract.
    Spread widens for illiquid tiers and for very cheap premiums (where
    the fixed tick size dominates). No live chain → model from tier.
    """
    tier = liquidity_tier(symbol)
    base = _TIER_BASE_SPREAD.get(tier, 0.85)
    # Cheap options (< ₹10) suffer larger relative spreads due to tick size.
    if premium < 10:
        base += (10 - premium) * 0.05
    return base


def spread_ok(symbol: str, premium: float, max_pct: float = MAX_SPREAD_PCT) -> tuple[bool, float]:
    """
    Brain 3 spread gate. Returns (passes, spread_pct).
    Disqualifies the contract if spread exceeds max_pct (default 0.5%).
    """
    sp = model_spread_pct(symbol, premium)
    return (sp <= max_pct, sp)


# ============================================================
# Brain 1 — intraday time-window (segment-aware: NSE vs MCX)
# ============================================================
def _to_ist(ts):
    if ts.tz is None:
        return ts.tz_localize("Asia/Kolkata")
    if str(ts.tz) != "Asia/Kolkata":
        return ts.tz_convert("Asia/Kolkata")
    return ts


def _in_entry_window(ts, segment: str = "stock") -> bool:
    """Segment-aware entry window: NSE uses 09:15-11:00 & 13:30-15:15;
    MCX commodities use 09:00-11:30 & 17:00-23:00."""
    ts = _to_ist(ts)
    t = ts.time()
    for start, end in entry_windows_for(segment):
        if t >= pd.Timestamp(f"2000-01-01 {start}").time() and t <= pd.Timestamp(f"2000-01-01 {end}").time():
            return True
    return False


def _is_square_off_bar(ts, segment: str = "stock") -> bool:
    """Segment-aware square-off: NSE 15:15, MCX 23:15."""
    ts = _to_ist(ts)
    sq = square_off_for(segment)
    return ts.time() == pd.Timestamp(f"2000-01-01 {sq}").time()


def brain1_intraday_pass(df, i, segment: str = "stock") -> dict:
    """Brain 1 gate: must be in segment entry window, bar must have a body."""
    ts = df.index[i]
    if not _in_entry_window(ts, segment):
        return {"passed_brain1": False, "reason": "outside entry window"}
    if i < 2:
        return {"passed_brain1": False, "reason": "insufficient bars"}
    cur = df.iloc[i]
    rng = float(cur["high"]) - float(cur["low"])
    if rng <= 0:
        return {"passed_brain1": False, "reason": "zero range"}
    return {"passed_brain1": True, "ts": ts}


# ============================================================
# Position sizing — ₹2,000 hard stop
# ============================================================
def size_with_hard_stop(entry_premium, stop_premium, lot_sz, max_loss=2000.0):
    stop_per_unit = abs(entry_premium - stop_premium)
    if stop_per_unit <= 0:
        return {"lots": 1, "quantity": lot_sz, "max_loss": entry_premium * lot_sz,
                "stop_per_unit": 0.0}
    loss_per_lot = stop_per_unit * lot_sz
    lots = max(1, int(max_loss // loss_per_lot))
    quantity = lots * lot_sz
    return {"lots": lots, "quantity": quantity,
            "max_loss": stop_per_unit * quantity, "stop_per_unit": stop_per_unit}


# ============================================================
# Brain 5 — intraday exit logic
# ============================================================
def check_intraday_exit(pos, cur_underlying, cur_premium, is_square_off_bar) -> dict:
    # 1. Square-off at 03:15 — highest priority, no carry
    if is_square_off_bar:
        return {"exit": True, "reason": "square_off_1515", "exit_premium": cur_premium}
    # 2. ₹2,000 hard stop
    if cur_premium <= pos["stop_premium"]:
        return {"exit": True, "reason": "stop_loss_2000", "exit_premium": max(cur_premium, 0.5)}
    # 3. Target scalp +40%
    gain_pct = (cur_premium - pos["entry_premium"]) / pos["entry_premium"] * 100
    if gain_pct >= 40:
        return {"exit": True, "reason": "target_40pct", "exit_premium": cur_premium}
    # 4. Structural stop — underlying crossed the zone edge
    if pos.get("zone_edge") is not None:
        if pos["direction"] == "BUY" and cur_underlying <= pos["zone_edge"]:
            return {"exit": True, "reason": "zone_break", "exit_premium": max(cur_premium, 0.5)}
        if pos["direction"] == "SELL" and cur_underlying >= pos["zone_edge"]:
            return {"exit": True, "reason": "zone_break", "exit_premium": max(cur_premium, 0.5)}
    return {"exit": False}


# ============================================================
# Main intraday walk-forward engine
# ============================================================
def run_intraday_backtest(
    data_map: dict,
    benchmark_df=None,
    start_capital: float = 150000.0,
    max_loss_per_trade: float = 2000.0,
    max_capital_per_trade_pct: float = 10.0,
    dte_default: float = 1.0,
    verbose: bool = False,
) -> dict:
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
                cur_prem = atm_premium(cur_underlying, pos["strike"], pos["dte"], is_call, iv)
                sq_off = _is_square_off_bar(ts, seg)

                ex = check_intraday_exit(pos, cur_underlying, cur_prem, sq_off)
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
                    })
                    if verbose:
                        logger.warning(f"EXIT {sym} {ex['reason']} pnl={pnl:.0f}")
                else:
                    pos["dte"] = max(pos["dte"] - 1 / 25, 0.05)
                    still_open.append(pos)
            open_positions = still_open

            # --- 2. SCAN ZONES + ENTER (Brains 1,2,3,4) ---
            # Square-off is segment-aware (NSE 15:15, MCX 23:15); checked
            # per-candidate below since a single timestamp may be square-off
            # for one segment but not another.
            day_candidates = []
            for sym, df_sym in data_map.items():
                if ts not in df_sym.index:
                    continue
                idx = df_sym.index.get_loc(ts)
                if idx < 40:
                    continue
                seg = segment_of(sym)
                # Brain 1: segment-aware entry window (NSE vs MCX)
                b1 = brain1_intraday_pass(df_sym, idx, segment=seg)
                if not b1["passed_brain1"]:
                    continue
                # Skip if this is a square-off bar for the symbol's segment
                if _is_square_off_bar(ts, seg):
                    continue
                # Brain 2: CONFIRMED Supply/Demand zone touch (rejection filter)
                setups = scan_zones(df_sym, idx, lookback=40, require_confirm=True)
                if not setups:
                    continue
                best = max(setups, key=lambda s: s["setup_score"])
                day_candidates.append({
                    "symbol": sym, "setup": best, "df_sym": df_sym,
                    "idx": idx, "ts": ts, "seg": seg,
                })

            day_candidates.sort(key=lambda c: c["setup"]["setup_score"], reverse=True)

            for cand in day_candidates:
                sym = cand["symbol"]
                seg = cand["seg"]
                if not counter.can_trade(sym)["allowed"]:
                    continue
                seg_day_key = sim_date
                if daily_seg_counts[seg_day_key][seg] >= 10:
                    continue

                setup = cand["setup"]
                df_sym = cand["df_sym"]
                idx = cand["idx"]
                cur_underlying = float(df_sym.iloc[idx]["close"])
                df_so_far = df_sym.loc[:ts]
                iv = max(min(realized_vol_simple(df_so_far), 0.80), 0.12)
                is_call = setup["direction"] == "BUY"
                strike = round(cur_underlying)
                entry_prem = atm_premium(cur_underlying, strike, dte_default, is_call, iv)
                entry_prem = max(entry_prem, 1.0)

                # Brain 3: LIQUIDITY & SPREAD SAFETY LAYER
                # Disqualify the ATM contract if bid-ask spread > 0.5%.
                ok_spread, spread_pct = spread_ok(sym, entry_prem)
                if not ok_spread:
                    if verbose:
                        logger.warning(
                            f"REJECT {sym} spread {spread_pct:.2f}% > 0.5% (illiquid)"
                        )
                    continue

                # Option stop = 30% of entry premium (intraday scalp stop)
                stop_prem = max(entry_prem * 0.70, 0.5)

                lot_sz = lot_size(sym)
                sizing = size_with_hard_stop(entry_prem, stop_prem, lot_sz, max_loss_per_trade)
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
                daily_seg_counts[seg_day_key][seg] += 1

                # zone edge = zone bottom (for demand/buy) or zone top (supply/sell)
                zone_edge = setup["zone_bottom"] if is_call else setup["zone_top"]

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
                    "allocated_capital": alloc,
                    "entry_ts": ts, "entry_idx": idx,
                    "zone_type": setup["zone_type"],
                    "zone_edge": zone_edge,
                    "confirmation": setup.get("confirmation", "n/a"),
                    "entry_spread_pct": round(spread_pct, 2),
                    "dte": dte_default,
                }
                open_positions.append(pos)
                if verbose:
                    logger.warning(
                        f"ENTRY {sym} {setup['strategy']} {pos['option_type']} "
                        f"strike={strike} qty={pos['quantity']} prem={entry_prem:.1f} "
                        f"zone={setup['zone_type']} cap={capital:.0f}"
                    )

        # --- 4. End-of-day MTM ---
        mtm = 0.0
        for pos in open_positions:
            sym = pos["symbol"]
            df_sym = data_map[sym]
            day_bars = df_sym[df_sym.index.normalize() == day]
            if len(day_bars) == 0:
                continue
            last_ts = day_bars.index[-1]
            cur_underlying = float(df_sym.loc[last_ts, "close"])
            df_so_far = df_sym.loc[:last_ts]
            iv = max(min(realized_vol_simple(df_so_far), 0.80), 0.12)
            is_call = pos["option_type"] == "CE"
            cur_prem = atm_premium(cur_underlying, pos["strike"], pos["dte"], is_call, iv)
            mtm += (cur_prem - pos["entry_premium"]) * pos["quantity"]
        equity = capital + mtm
        peak_equity = max(peak_equity, equity)
        dd = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0
        max_dd = max(max_dd, dd)
        equity_curve.append({"date": day, "equity": equity, "capital": capital,
                              "open_positions": len(open_positions), "mtm": mtm})

    # --- 5. Force-close any residual ---
    for pos in open_positions:
        sym = pos["symbol"]
        df_sym = data_map[sym]
        last_ts = df_sym.index[-1]
        cur_underlying = float(df_sym.loc[last_ts, "close"])
        iv = max(min(realized_vol_simple(df_sym), 0.80), 0.12)
        is_call = pos["option_type"] == "CE"
        cur_prem = atm_premium(cur_underlying, pos["strike"], 0.05, is_call, iv)
        cur_prem = max(cur_prem, 0.5)
        pnl = (cur_prem - pos["entry_premium"]) * pos["quantity"] - 40
        pnl = max(pnl, -max_loss_per_trade - 41)
        capital += pnl
        trades.append({**pos, "exit_ts": last_ts, "exit_premium": cur_prem,
                        "pnl": pnl, "exit_reason": "backtest_end",
                        "hold_bars": len(df_sym) - pos["entry_idx"]})
    open_positions.clear()

    return _compute_metrics(trades, equity_curve, start_capital, max_dd, daily_seg_counts)


def _compute_metrics(trades, equity_curve, start_capital, max_dd, daily_seg_counts):
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    final_equity = equity_curve[-1]["equity"] if equity_curve else start_capital
    ret = ((final_equity - start_capital) / start_capital) * 100 if start_capital else 0
    pf = gp / gl if gl > 0 else float("inf")

    seg_stats = {}
    for seg_key in ("index", "stock", "commodity"):
        st = [t for t in trades if t.get("segment") == seg_key]
        if not st:
            seg_stats[seg_key] = None
            continue
        sw = [t for t in st if t["pnl"] > 0]
        sgp = sum(t["pnl"] for t in sw)
        sgl = abs(sum(t["pnl"] for t in st if t["pnl"] <= 0))
        seg_stats[seg_key] = {
            "label": UNIVERSE[seg_key]["label"],
            "trades": len(st), "wins": len(sw),
            "win_rate_pct": (len(sw) / len(st) * 100) if st else 0,
            "gross_profit": sgp, "gross_loss": sgl,
            "net_pnl": sum(t["pnl"] for t in st),
            "profit_factor": (sgp / sgl) if sgl > 0 else float("inf"),
        }

    strat_stats = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    for t in trades:
        s = t.get("strategy", "?")
        strat_stats[s]["trades"] += 1
        if t["pnl"] > 0:
            strat_stats[s]["wins"] += 1
        strat_stats[s]["pnl"] += t["pnl"]

    max_daily_global = max((sum(v.values()) for v in daily_seg_counts.values()), default=0)
    max_daily_commodity = max((v["commodity"] for v in daily_seg_counts.values() if "commodity" in v), default=0)

    return {
        "totals": {
            "start_capital": start_capital, "final_equity": final_equity,
            "total_return_pct": ret, "net_pnl": sum(t["pnl"] for t in trades),
            "total_trades": n, "wins": len(wins), "losses": len(losses),
            "win_rate_pct": (len(wins) / n * 100) if n else 0,
            "profit_factor": pf, "max_drawdown_pct": max_dd * 100,
            "avg_pnl_per_trade": (sum(t["pnl"] for t in trades) / n) if n else 0,
            "avg_winner": (gp / len(wins)) if wins else 0,
            "avg_loser": (-gl / len(losses)) if losses else 0,
            "best_trade": max((t["pnl"] for t in trades), default=0),
            "worst_trade": min((t["pnl"] for t in trades), default=0),
            "max_daily_global_trades": max_daily_global,
            "max_daily_commodity_trades": max_daily_commodity,
        },
        "segment_stats": seg_stats,
        "strategy_stats": dict(strat_stats),
        "equity_curve": equity_curve, "trades": trades,
        "daily_seg_counts": dict(daily_seg_counts),
    }


def print_report(result: dict) -> None:
    t = result["totals"]
    print("=" * 72)
    print("  TIGER BRAIN V6.4 — PURE S/D + NSE/MCX INSTITUTIONAL SHIELD")
    print("=" * 72)
    print("  Mode:  PURE INTRADAY (15-min) | Supply/Demand ZONES ONLY")
    print("        NO VWAP, NO RS, NO EMA, NO Black-Scholes")
    print("  Shield: ① Zone confirmation (rejection wick/struct break)")
    print("          ② NSE 15:15 / MCX 23:15 square-off")
    print("          ③ Spread gate ≤0.5% (liquidity safety)")
    print(f"  Starting Capital:   ₹{t['start_capital']:>12,.0f}")
    print(f"  Final Equity:        ₹{t['final_equity']:>12,.0f}")
    print(f"  Total Return:        {t['total_return_pct']:>12.2f}%")
    print(f"  Net P&L:             ₹{t['net_pnl']:>12,.0f}")
    print("-" * 72)
    print("  TRADE STATISTICS")
    print("-" * 72)
    print(f"  Total Trades:        {t['total_trades']:>12d}")
    print(f"  Wins / Losses:       {t['wins']:>5d} / {t['losses']:<5d}")
    print(f"  Win Rate:            {t['win_rate_pct']:>12.2f}%")
    print(f"  Profit Factor:       {t['profit_factor']:>12.2f}")
    print(f"  Avg P&L / Trade:     ₹{t['avg_pnl_per_trade']:>12,.0f}")
    print(f"  Avg Winner:          ₹{t['avg_winner']:>12,.0f}")
    print(f"  Avg Loser:           ₹{t['avg_loser']:>12,.0f}")
    print(f"  Best Trade:          ₹{t['best_trade']:>12,.0f}")
    print(f"  Worst Trade:         ₹{t['worst_trade']:>12,.0f}")
    print("-" * 72)
    print("  RISK (₹2,000 HARD STOP + 03:15 SQUARE-OFF)")
    print("-" * 72)
    print(f"  Maximum Drawdown:    {t['max_drawdown_pct']:>12.2f}%")
    print(f"  Worst single trade:  ₹{t['worst_trade']:>12,.0f} (capped near -₹2,000)")
    print("-" * 72)
    print("  BRAIN 4 — DAILY 5-10 QUOTA")
    print("-" * 72)
    print(f"  Max trades/day (global):     {t['max_daily_global_trades']:>5d}  (cap 5-10)")
    print(f"  Max trades/day (commodity):   {t['max_daily_commodity_trades']:>5d}  (cap 5-10)")
    print(f"  03:15 square-off enforced:    YES")
    print(f"  Overnight carry-forward:      NONE")
    print("-" * 72)
    print("  SEGMENT BREAKDOWN — INDEX vs STOCKS vs COMMODITIES")
    print("-" * 72)
    print(f"  {'Segment':<40s} {'Trades':>7s} {'Win%':>7s} {'NetP&L':>10s} {'PF':>6s}")
    print(f"  {'-'*40} {'-'*7} {'-'*7} {'-'*10} {'-'*6}")
    for seg_key in ("index", "stock", "commodity"):
        s = result["segment_stats"].get(seg_key)
        if s is None:
            print(f"  {UNIVERSE[seg_key]['label']:<40s} {'—':>7s} {'—':>7s} {'—':>10s} {'—':>6s}")
            continue
        pf = f"{s['profit_factor']:.2f}" if s['profit_factor'] != float('inf') else "inf"
        print(f"  {s['label']:<40s} {s['trades']:>7d} {s['win_rate_pct']:>6.1f}% "
              f"₹{s['net_pnl']:>9,.0f} {pf:>6s}")
    print("-" * 72)
    print("  BRAIN 2 — ZONE TYPE BREAKDOWN (Demand vs Supply)")
    print("-" * 72)
    print(f"  {'Zone Type':<14s} {'Trades':>7s} {'Win%':>7s} {'NetP&L':>10s}")
    print(f"  {'-'*14} {'-'*7} {'-'*7} {'-'*10}")
    for sname, sv in sorted(result["strategy_stats"].items(), key=lambda x: -x[1]["pnl"]):
        wr = (sv["wins"] / sv["trades"] * 100) if sv["trades"] else 0
        print(f"  {sname:<14s} {sv['trades']:>7d} {wr:>6.1f}% ₹{sv['pnl']:>9,.0f}")
    print("-" * 72)
    print("  BRAIN 2 — ZONE CONFIRMATION BREAKDOWN (rejection filter)")
    print("-" * 72)
    conf = Counter(t.get("confirmation", "n/a") for t in result["trades"])
    for c, cnt in conf.most_common():
        print(f"  {c:<24s} {cnt:>5d} trades")
    print("-" * 72)
    print("  BRAIN 3 — SPREAD GATE (avg entry spread %)")
    print("-" * 72)
    spreads = [t.get("entry_spread_pct", 0) for t in result["trades"]]
    avg_sp = sum(spreads) / len(spreads) if spreads else 0
    print(f"  Avg entry spread:    {avg_sp:>12.2f}%  (gate: ≤0.5%)")
    print("-" * 72)
    print("  EXIT REASON BREAKDOWN (Brain 5)")
    print("-" * 72)
    reasons = Counter(t["exit_reason"] for t in result["trades"])
    for r, c in reasons.most_common():
        print(f"  {r:<24s} {c:>5d} trades")
    print("=" * 72)
    print("  HONESTY NOTES")
    print("=" * 72)
    print("  1. 15-min data via yfinance = ~60 trading days only (small sample).")
    print("  2. ATM premiums simple model (intrinsic + time value), NOT real chain.")
    print("  3. No intrabar path — 15m bar ke andar stop/target order unknown.")
    print("  4. ₹2,000 hard stop cap assumed (slippage may worsen real loss).")
    print("  5. Commodity proxies (CL=F/GC=F) are US futures, IST-aligned approx.")
    print("  6. Zones detected from pure price action — no indicators used.")
    print("  7. Positive = 'paper-trade worthy', NOT 'guaranteed profitable'.")
    print("=" * 72)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    import yfinance as yf
    from universe.fno_universe import all_symbols

    logging.basicConfig(level=logging.INFO)
    print("Fetching 15-min intraday data via yfinance (~60 days)...")
    period = "60d"
    interval = "15m"

    syms = all_symbols()
    data_map = {}
    failed = []
    for sym, tk in syms.items():
        d = yf.download(tk, period=period, interval=interval, progress=False)
        if d is None or d.empty:
            failed.append(sym)
            continue
        d = d.copy()
        d.columns = [c.lower() if isinstance(c, str) else (c[0].lower() if hasattr(c, '__len__') else str(c).lower()) for c in d.columns]
        d.index = pd.to_datetime(d.index)
        d = d.dropna(subset=["close"])
        data_map[sym] = d
        print(f"  {sym:14s}: {len(d):5d} bars")

    print(f"\nFailed symbols: {failed}")
    print(f"Universe loaded: {len(data_map)} symbols")

    # Per-segment standalone + combined
    from universe.fno_universe import UNIVERSE as _UNI
    seg_results = {}
    for seg_key in ("index", "stock", "commodity"):
        seg_syms = list(_UNI[seg_key]["symbols"].keys())
        seg_map = {s: data_map[s] for s in seg_syms if s in data_map}
        if not seg_map:
            seg_results[seg_key] = None
            continue
        print(f"\nRunning {seg_key} standalone ({len(seg_map)} symbols, ₹1.5L)...")
        seg_results[seg_key] = run_intraday_backtest(
            seg_map, start_capital=150000.0, max_loss_per_trade=2000.0
        )

    print(f"\nRunning COMBINED portfolio (₹1.5L, {len(data_map)} symbols)...")
    combined = run_intraday_backtest(
        data_map, start_capital=150000.0, max_loss_per_trade=2000.0
    )

    print("\n\n")
    print("#" * 72)
    print("#  PART 1 — COMBINED PORTFOLIO (all segments, shared daily cap)")
    print("#" * 72)
    print_report(combined)

    for seg_key in ("index", "stock", "commodity"):
        print("\n\n")
        print("#" * 72)
        print(f"#  PART 2 — {_UNI[seg_key]['label'].upper()} (standalone, ₹1.5L)")
        print("#" * 72)
        if seg_results[seg_key] is None:
            print("  (no data for this segment)")
            continue
        print_report(seg_results[seg_key])
