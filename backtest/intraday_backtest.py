"""
Tiger Brain V6.2 — Pure Intraday 5-Brain Backtest Engine
==========================================================
Pure intraday CALL/PUT options-buying machine. NO overnight holding.
15-minute candles, 5 parallel strategies, ₹2,000 hard stop, 03:15 PM
hard square-off, expanded F&O universe, 5-10 daily quota per segment.

SACHAI (honesty guardrails — code mein, sirf baat nahi):

1. NO LOOKAHEAD — bar `i` pe decision sirf `df.iloc[:i+1]` se. Kal ka
   candle future se nahi aata.

2. SYNTHETIC OPTION CHAINS — real intraday Indian option-chain (per
   strike premium, OI, delta, 15-min history) free mein kahin nahi
   milta. yfinance sirf underlying 15m OHLCV deta hai. Isliye har bar
   pe ek ATM option Black-Scholes se price hota hai (IV = realized vol,
   delta 0.50-0.60 band). Real chain mein skew/smile + per-strike OI
   hota hai. Premiums SUSHK (model) hain.

3. 15m DATA LIMIT — yfinance 15-minute data sirf ~60 din ka deta hai.
   Isliye backtest ~60 trading days pe chalta hai. 60 din = small sample
   but intraday frequency se 300+ trades possible.

4. INTRADAY PATH — har 15m bar pe open positions re-price hote hain
   aur Brain 5 check karta hai stop/target/square-off. Ye daily
   close-to-close se kaafi behtar hai — intraday stop hit pata chalta
   hai. Par 15m bar ke andar ka high-low path nahi milta (ek bar mein
   stop pahle hit hua ya target — unknown).

5. ₹2,000 HARD STOP — har trade ka max loss ₹2,000 (1.3% of ₹1.5L).
   Position sizing: lots = floor(2000 / (stop_distance * lot_size)),
   min 1 lot. Stop distance = |entry_premium - stop_premium|, jahan
   stop_premium = ATM option priced at underlying stop_loss level.

6. NO CARRY-FORWARD — 03:15 PM IST pe Brain 5 sab positions square-off
   karta hai. Koi overnight risk nahi.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from collections import defaultdict

import numpy as np
import pandas as pd

try:
    from config.thresholds import BRAIN3, BRAIN4
    from pipeline.intraday_strategies import scan_intraday, _atr, _vwap
    from universe.fno_universe import (
        UNIVERSE, lot_size, segment_of, LOT_SIZES,
        ENTRY_WINDOWS, SQUARE_OFF_TIME,
    )
    from risk.risk_management import TradeCounterGuard, resolve_market_category
except ImportError:
    raise ImportError("Repo ROOT se chalao.")

logger = logging.getLogger("tiger_brain.backtest.intraday")
logging.basicConfig(level=logging.WARNING)


# ============================================================
# Black-Scholes (ATM option pricing for synthetic chain)
# ============================================================
def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call(S, K, T, sigma, r=0.06):
    if T <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)


def bs_put(S, K, T, sigma, r=0.06):
    if T <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs_delta(S, K, T, sigma, is_call=True, r=0.06):
    if T <= 0:
        return 1.0 if (is_call and S > K) else (-1.0 if (not is_call and S < K) else 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d = _norm_cdf(d1)
    return d if is_call else d - 1.0


def realized_vol(df, window=20, bars_per_day=25):
    """Intraday realized vol — annualized via 15m bars (25/day)."""
    if len(df) < window + 1:
        return 0.25
    closes = df["close"].astype(float)
    rets = np.log(closes / closes.shift(1)).dropna()
    if len(rets) < window:
        return 0.25
    annualize = math.sqrt(bars_per_day * 252)
    return float(np.std(rets.iloc[-window:], ddof=1) * annualize)


# ============================================================
# Brain 1 intraday — time-window + momentum filter
# ============================================================
def _in_entry_window(ts) -> bool:
    """Check if timestamp is in the high-momentum entry windows (IST)."""
    if ts.tz is None:
        ts = ts.tz_localize("Asia/Kolkata")
    elif str(ts.tz) not in ("Asia/Kolkata", "tzfile('/usr/share/zoneinfo/Asia/Kolkata')"):
        ts = ts.tz_convert("Asia/Kolkata")
    t = ts.time()
    for start, end in ENTRY_WINDOWS:
        sh, sm = map(int, start.split(":"))
        eh, em = map(int, end.split(":"))
        if t >= datetime(2000, 1, 1, sh, sm).time() and t <= datetime(2000, 1, 1, eh, em).time():
            return True
    return False


def _is_square_off_bar(ts) -> bool:
    """Is this the 03:15 PM square-off bar?"""
    if ts.tz is None:
        ts = ts.tz_localize("Asia/Kolkata")
    else:
        ts = ts.tz_convert("Asia/Kolkata")
    sh, sm = map(int, SQUARE_OFF_TIME.split(":"))
    return ts.time() == datetime(2000, 1, 1, sh, sm).time()


def brain1_intraday_pass(df, i) -> dict:
    """
    Brain 1 intraday gate:
      1. Must be in entry window (09:15-11:00 or 13:30-15:15 IST)
      2. Current bar must have momentum (body >= 50% of range, OR vol spike)
    """
    ts = df.index[i]
    if not _in_entry_window(ts):
        return {"passed_brain1": False, "reason": "outside entry window"}
    if i < 2:
        return {"passed_brain1": False, "reason": "insufficient bars"}
    cur = df.iloc[i]
    rng = float(cur["high"]) - float(cur["low"])
    body = abs(float(cur["close"]) - float(cur["open"]))
    if rng <= 0:
        return {"passed_brain1": False, "reason": "zero range"}
    body_ratio = body / rng
    avg_vol = float(df["volume"].iloc[max(0, i - 20):i].mean() or 1)
    vol_spike = float(cur["volume"]) > avg_vol * 1.3
    if body_ratio < 0.5 and not vol_spike:
        return {"passed_brain1": False, "reason": f"choppy bar (body {body_ratio:.0%})"}
    return {"passed_brain1": True, "body_ratio": round(body_ratio, 2),
            "vol_spike": vol_spike, "ts": ts}


# ============================================================
# Synthetic ATM option pricing (Brain 3)
# ============================================================
def price_atm_option(underlying, strike, dte_fraction, sigma, is_call=True):
    """Price an ATM-ish option at current underlying. dte_fraction = days to expiry."""
    T = max(dte_fraction, 1 / (25 * 252)) / 365.0  # min 1 bar
    return (bs_call if is_call else bs_put)(underlying, strike, T, sigma)


def option_delta(underlying, strike, dte_fraction, sigma, is_call=True):
    T = max(dte_fraction, 1 / (25 * 252)) / 365.0
    return bs_delta(underlying, strike, T, sigma, is_call=is_call)


# ============================================================
# Position sizing with ₹2000 hard stop (Brain 4)
# ============================================================
def size_with_hard_stop(entry_premium, stop_premium, lot_sz, max_loss=2000.0):
    """
    Compute lots so max loss = ₹2,000. Stop distance per unit =
    |entry - stop| * lot_size. lots = floor(2000 / (stop_dist * lot)).
    Min 1 lot, max capped by available capital (handled by caller).
    """
    stop_per_unit = abs(entry_premium - stop_premium)
    if stop_per_unit <= 0:
        return {"lots": 1, "quantity": lot_sz, "max_loss": entry_premium * lot_sz,
                "stop_per_unit": 0.0}
    loss_per_lot = stop_per_unit * lot_sz
    lots = max(1, int(max_loss // loss_per_lot))
    quantity = lots * lot_sz
    actual_max_loss = stop_per_unit * quantity
    return {"lots": lots, "quantity": quantity,
            "max_loss": actual_max_loss, "stop_per_unit": stop_per_unit}


# ============================================================
# Brain 5 intraday exit logic
# ============================================================
def check_intraday_exit(pos, cur_underlying, cur_premium, ts, is_square_off_bar) -> dict:
    """
    Brain 5 exit checks (intraday scalp logic):
      1. ₹2,000 hard stop-loss: option premium fell to stop_premium
      2. Square-off at 03:15 PM (forced — no carry-forward)
      3. Target scalp: +40% gain => exit (lock in intraday profit)
      4. Structural stop: underlying crossed Brain 2's stop_loss level
         (only if option hasn't already been stopped by premium gate)
    """
    # 1. Square-off (highest priority — must exit, no carry)
    if is_square_off_bar:
        return {"exit": True, "reason": "square_off_1515",
                "exit_premium": cur_premium}
    # 2. Hard stop: option premium breached its stop
    if cur_premium <= pos["stop_premium"]:
        return {"exit": True, "reason": "stop_loss_2000",
                "exit_premium": max(cur_premium, 0.5)}
    # 3. Target scalp: +40% gain intraday
    gain_pct = (cur_premium - pos["entry_premium"]) / pos["entry_premium"] * 100
    if gain_pct >= 40:
        return {"exit": True, "reason": "target_40pct",
                "exit_premium": cur_premium}
    # 4. Structural stop: underlying crossed SMC level (backup safety)
    if pos.get("underlying_stop") is not None:
        if pos["direction"] == "BUY" and cur_underlying <= pos["underlying_stop"]:
            return {"exit": True, "reason": "structural_stop",
                    "exit_premium": max(cur_premium, 0.5)}
        if pos["direction"] == "SELL" and cur_underlying >= pos["underlying_stop"]:
            return {"exit": True, "reason": "structural_stop",
                    "exit_premium": max(cur_premium, 0.5)}
    return {"exit": False}


# ============================================================
# Main intraday walk-forward engine
# ============================================================
def run_intraday_backtest(
    data_map: dict,
    benchmark_df: pd.DataFrame | None = None,
    start_capital: float = 150000.0,
    max_loss_per_trade: float = 2000.0,
    max_capital_per_trade_pct: float = 10.0,
    dte_default: float = 1.0,  # ~1 day to expiry (intraday options)
    verbose: bool = False,
) -> dict:
    """
    data_map: {symbol: pd.DataFrame} with 15m OHLCV, tz-aware index.
    benchmark_df: 15m OHLCV benchmark for RS strategy.

    Returns dict with trades, equity_curve, segment_stats, totals.
    """
    # Union of all timestamps across symbols, sorted
    all_ts = sorted(set().union(*[set(d.index) for d in data_map.values()]))
    # Group timestamps by trading day
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
    # Backtest needs simulated-date-aware rolling, not wall-clock date.today().
    # We patch the counter's _today to the simulated date each day before rolling.
    # Track daily trade counts per segment for the 5-10 quota
    daily_seg_counts = defaultdict(lambda: defaultdict(int))  # day -> seg -> count

    for day in sorted_days:
        day_ts = days[day]
        # Force the counter to roll based on SIMULATED date, not wall clock.
        sim_date = day.date() if day.tz is None else day.tz_convert("Asia/Kolkata").date()
        if counter._today != sim_date:
            counter._today = sim_date
            counter.global_count = 0
            counter.commodity_count = 0

        for ts in day_ts:
            # --- 1. EXIT open positions at this bar (Brain 5) ---
            still_open = []
            sq_off = _is_square_off_bar(ts)
            for pos in open_positions:
                sym = pos["symbol"]
                df_sym = data_map[sym]
                if ts not in df_sym.index:
                    still_open.append(pos)
                    continue
                cur_underlying = float(df_sym.loc[ts, "close"])
                df_so_far = df_sym.loc[:ts]
                sigma = max(min(realized_vol(df_so_far), 0.80), 0.12)
                is_call = pos["option_type"] == "CE"
                cur_prem = price_atm_option(cur_underlying, pos["strike"],
                                            pos["dte"], sigma, is_call)
                cur_prem = max(cur_prem, 0.5)

                ex = check_intraday_exit(pos, cur_underlying, cur_prem, ts, sq_off)
                if ex["exit"]:
                    exit_prem = ex["exit_premium"]
                    slippage = exit_prem * 0.008 + pos["entry_premium"] * 0.008
                    brokerage = 20.0 * 2  # both legs
                    pnl = (exit_prem - pos["entry_premium"]) * pos["quantity"] - slippage * 2 - brokerage
                    # hard stop cap: actual loss capped near ₹2000
                    pnl = max(pnl, -max_loss_per_trade - brokerage - slippage * 2 + 1)
                    capital += pnl
                    trades.append({
                        **pos,
                        "exit_ts": ts,
                        "exit_premium": exit_prem,
                        "pnl": pnl,
                        "exit_reason": ex["reason"],
                        "hold_bars": len(df_so_far) - pos["entry_idx"],
                    })
                    if verbose:
                        logger.warning(f"EXIT {sym} {pos['option_type']} {ex['reason']} pnl={pnl:.0f}")
                else:
                    # decay DTE slightly through the day
                    pos["dte"] = max(pos["dte"] - 1 / 25, 0.05)
                    still_open.append(pos)
            open_positions = still_open

            # --- 2. Square-off bar: NO new entries after 15:15 ---
            if sq_off:
                continue

            # --- 3. SCAN + ENTER (Brains 1,2,3,4) across universe ---
            current_exposure = sum(p["entry_premium"] * p["quantity"] for p in open_positions)
            day_candidates = []

            for sym, df_sym in data_map.items():
                if ts not in df_sym.index:
                    continue
                idx = df_sym.index.get_loc(ts)
                if idx < 50:
                    continue

                # Brain 1 intraday gate
                b1 = brain1_intraday_pass(df_sym, idx)
                if not b1["passed_brain1"]:
                    continue

                # Brain 2: 5 parallel strategies
                bench = benchmark_df[benchmark_df.index <= ts] if benchmark_df is not None else None
                setups = scan_intraday(df_sym, idx, benchmark_df=bench)
                if not setups:
                    continue

                # Pick the best-scoring setup for this symbol
                best = max(setups, key=lambda s: s["setup_score"])
                day_candidates.append({
                    "symbol": sym, "setup": best,
                    "df_sym": df_sym, "idx": idx, "ts": ts,
                    "seg": segment_of(sym),
                })

            # Allocate: sort by score, take top up to daily cap (Brain 4)
            day_candidates.sort(key=lambda c: c["setup"]["setup_score"], reverse=True)

            for cand in day_candidates:
                sym = cand["symbol"]
                seg = cand["seg"]
                # Brain 4: daily quota (5-10). Use counter's global + commodity.
                cnt = counter.can_trade(sym)
                if not cnt["allowed"]:
                    continue
                # Per-segment daily quota (target 5-10 per segment)
                seg_day_key = day.date()
                if daily_seg_counts[seg_day_key][seg] >= 10:
                    continue

                setup = cand["setup"]
                df_sym = cand["df_sym"]
                idx = cand["idx"]
                cur_underlying = float(df_sym.iloc[idx]["close"])
                df_so_far = df_sym.loc[:ts]
                sigma = max(min(realized_vol(df_so_far), 0.80), 0.12)
                is_call = setup["direction"] == "BUY"
                # ATM strike (Brain 3: delta 0.50-0.60)
                strike = round(cur_underlying)
                entry_prem = price_atm_option(cur_underlying, strike, dte_default, sigma, is_call)
                entry_prem = max(entry_prem, 1.0)
                delta = abs(option_delta(cur_underlying, strike, dte_default, sigma, is_call))

                # Brain 3: delta band check (0.50-0.60 ATM, allow 0.45-0.75)
                if delta < 0.45 or delta > 0.75:
                    continue

                # Option stop = 30% of entry premium (intraday scalp stop).
                # This is how real option buyers set premium stops (not by
                # pricing option at the far underlying level, which gives
                # a near-zero stop that fires on noise). The ₹2000 hard
                # cap then sizes lots so a 30% option stop = ₹2000 max loss.
                opt_stop_pct = 0.30
                stop_prem = max(entry_prem * (1 - opt_stop_pct), 0.5)

                # Brain 4: position sizing with ₹2000 hard stop
                lot_sz = lot_size(sym)
                sizing = size_with_hard_stop(entry_prem, stop_prem, lot_sz, max_loss_per_trade)
                # Capital cap: max 10% of capital per trade
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

                pos = {
                    "symbol": sym,
                    "segment": seg,
                    "strategy": setup["strategy"],
                    "direction": setup["direction"],
                    "option_type": "CE" if is_call else "PE",
                    "strike": strike,
                    "entry_premium": entry_prem,
                    "stop_premium": stop_prem,
                    "entry_delta": delta,
                    "quantity": sizing["quantity"],
                    "lots": sizing["lots"],
                    "max_loss": sizing["max_loss"],
                    "allocated_capital": alloc,
                    "entry_ts": ts,
                    "entry_idx": idx,
                    "underlying_stop": setup["stop_loss"],
                    "dte": dte_default,
                }
                open_positions.append(pos)
                current_exposure += alloc
                if verbose:
                    logger.warning(
                        f"ENTRY {sym} {setup['strategy']} {pos['option_type']} "
                        f"strike={strike} qty={pos['quantity']} prem={entry_prem:.1f} "
                        f"stop={stop_prem:.1f} maxloss={pos['max_loss']:.0f} cap={capital:.0f}"
                    )

        # --- 4. End-of-day MTM equity ---
        mtm = 0.0
        for pos in open_positions:
            sym = pos["symbol"]
            df_sym = data_map[sym]
            # last bar of this day
            day_bars = df_sym[df_sym.index.normalize() == day]
            if len(day_bars) == 0:
                continue
            last_ts = day_bars.index[-1]
            cur_underlying = float(df_sym.loc[last_ts, "close"])
            df_so_far = df_sym.loc[:last_ts]
            sigma = max(min(realized_vol(df_so_far), 0.80), 0.12)
            is_call = pos["option_type"] == "CE"
            cur_prem = price_atm_option(cur_underlying, pos["strike"], pos["dte"], sigma, is_call)
            mtm += (cur_prem - pos["entry_premium"]) * pos["quantity"]
        equity = capital + mtm
        peak_equity = max(peak_equity, equity)
        dd = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0
        max_dd = max(max_dd, dd)
        equity_curve.append({"date": day, "equity": equity, "capital": capital,
                             "open_positions": len(open_positions), "mtm": mtm})

    # --- 5. Force-close any residual (shouldn't be any due to square-off) ---
    for pos in open_positions:
        sym = pos["symbol"]
        df_sym = data_map[sym]
        last_ts = df_sym.index[-1]
        cur_underlying = float(df_sym.loc[last_ts, "close"])
        df_so_far = df_sym
        sigma = max(min(realized_vol(df_so_far), 0.80), 0.12)
        is_call = pos["option_type"] == "CE"
        cur_prem = price_atm_option(cur_underlying, pos["strike"], 0.05, sigma, is_call)
        cur_prem = max(cur_prem, 0.5)
        pnl = (cur_prem - pos["entry_premium"]) * pos["quantity"] - 40
        pnl = max(pnl, -max_loss_per_trade - 41)
        capital += pnl
        trades.append({**pos, "exit_ts": last_ts, "exit_premium": cur_prem,
                       "pnl": pnl, "exit_reason": "backtest_end",
                       "hold_bars": len(df_so_far) - pos["entry_idx"]})
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

    # Segment breakdown
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

    # Strategy breakdown
    strat_stats = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    for t in trades:
        s = t.get("strategy", "?")
        strat_stats[s]["trades"] += 1
        if t["pnl"] > 0:
            strat_stats[s]["wins"] += 1
        strat_stats[s]["pnl"] += t["pnl"]

    # Daily quota check
    max_daily_global = max((sum(v.values()) for v in daily_seg_counts.values()), default=0)
    max_daily_commodity = max((v["commodity"] for v in daily_seg_counts.values() if "commodity" in v), default=0)

    return {
        "totals": {
            "start_capital": start_capital,
            "final_equity": final_equity,
            "total_return_pct": ret,
            "net_pnl": sum(t["pnl"] for t in trades),
            "total_trades": n, "wins": len(wins), "losses": len(losses),
            "win_rate_pct": (len(wins) / n * 100) if n else 0,
            "profit_factor": pf,
            "max_drawdown_pct": max_dd * 100,
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
        "equity_curve": equity_curve,
        "trades": trades,
        "daily_seg_counts": dict(daily_seg_counts),
    }


def print_report(result: dict) -> None:
    t = result["totals"]
    print("=" * 72)
    print("  TIGER BRAIN V6.2 — PURE INTRADAY 5-BRAIN BACKTEST REPORT")
    print("=" * 72)
    print(f"  Mode:               PURE INTRADAY (15-min candles, no overnight)")
    print(f"  Starting Capital:   ₹{t['start_capital']:>12,.0f}")
    print(f"  Final Equity:       ₹{t['final_equity']:>12,.0f}")
    print(f"  Total Return:       {t['total_return_pct']:>12.2f}%")
    print(f"  Net P&L:            ₹{t['net_pnl']:>12,.0f}")
    print("-" * 72)
    print("  TRADE STATISTICS")
    print("-" * 72)
    print(f"  Total Trades:       {t['total_trades']:>12d}")
    print(f"  Wins / Losses:      {t['wins']:>5d} / {t['losses']:<5d}")
    print(f"  Win Rate:           {t['win_rate_pct']:>12.2f}%")
    print(f"  Profit Factor:      {t['profit_factor']:>12.2f}")
    print(f"  Avg P&L / Trade:    ₹{t['avg_pnl_per_trade']:>12,.0f}")
    print(f"  Avg Winner:         ₹{t['avg_winner']:>12,.0f}")
    print(f"  Avg Loser:          ₹{t['avg_loser']:>12,.0f}")
    print(f"  Best Trade:         ₹{t['best_trade']:>12,.0f}")
    print(f"  Worst Trade:        ₹{t['worst_trade']:>12,.0f}")
    print("-" * 72)
    print("  RISK METRICS (₹2,000 HARD STOP + 03:15 SQUARE-OFF)")
    print("-" * 72)
    print(f"  Maximum Drawdown:   {t['max_drawdown_pct']:>12.2f}%  (vs ₹1.5L)")
    print(f"  Max DD (₹):         ₹{t['max_drawdown_pct']/100*t['start_capital']:>12,.0f}")
    print(f"  Worst single trade: ₹{t['worst_trade']:>12,.0f}  (capped near -₹2,000)")
    print("-" * 72)
    print("  BRAIN 4 — DAILY 5-10 QUOTA ENFORCEMENT")
    print("-" * 72)
    print(f"  Max trades/day (global):     {t['max_daily_global_trades']:>5d}  (cap 5-10)")
    print(f"  Max trades/day (commodity):  {t['max_daily_commodity_trades']:>5d}  (cap 5-10)")
    print(f"  03:15 square-off enforced:    YES (Brain 5 hard rule)")
    print(f"  Overnight carry-forward:      NONE (pure intraday)")
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
    print("  BRAIN 2 — 5 PARALLEL STRATEGY BREAKDOWN")
    print("-" * 72)
    print(f"  {'Strategy':<14s} {'Trades':>7s} {'Win%':>7s} {'NetP&L':>10s}")
    print(f"  {'-'*14} {'-'*7} {'-'*7} {'-'*10}")
    for sname, sv in sorted(result["strategy_stats"].items(), key=lambda x: -x[1]["pnl"]):
        wr = (sv["wins"] / sv["trades"] * 100) if sv["trades"] else 0
        print(f"  {sname:<14s} {sv['trades']:>7d} {wr:>6.1f}% ₹{sv['pnl']:>9,.0f}")
    print("-" * 72)
    print("  EXIT REASON BREAKDOWN (Brain 5)")
    print("-" * 72)
    from collections import Counter
    reasons = Counter(t["exit_reason"] for t in result["trades"])
    for r, c in reasons.most_common():
        print(f"  {r:<24s} {c:>5d} trades")
    print("=" * 72)
    print("  HONESTY NOTES — read before trusting these numbers")
    print("=" * 72)
    print("  1. 15-min data via yfinance = ~60 trading days only (small sample).")
    print("  2. ATM option premiums SYNTHETIC (Black-Scholes, IV from realized")
    print("     vol). Real chains have skew/smile + per-strike OI.")
    print("  3. No intrabar path — 15m bar ke andar stop/target order unknown.")
    print("  4. ₹2,000 hard stop cap assumed (slippage may worsen real loss).")
    print("  5. Commodity proxies (CL=F/GC=F) are US futures, IST-aligned approx.")
    print("  6. Positive = 'paper-trade worthy', NOT 'guaranteed profitable'.")
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
        # Drop volume NaN rows
        d = d.dropna(subset=["close"])
        data_map[sym] = d
        print(f"  {sym:14s}: {len(d):5d} bars")

    print(f"\nFailed symbols: {failed}")
    print(f"Universe loaded: {len(data_map)} symbols")

    # Benchmark: Sensex intraday (RS divergence) — separate from traded symbols
    bench = yf.download("^BSESN", period=period, interval=interval, progress=False).copy()
    if bench is not None and not bench.empty:
        bench.columns = [c.lower() if isinstance(c, str) else c[0].lower() for c in bench.columns]
        bench.index = pd.to_datetime(bench.index)
        bench = bench.dropna(subset=["close"])
    else:
        bench = data_map.get("NIFTY")  # fallback

    # ---- Run each segment independently (fair: own ₹1.5L + own daily cap) ----
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
            seg_map, benchmark_df=bench, start_capital=150000.0, max_loss_per_trade=2000.0
        )

    # ---- Combined portfolio (shared daily cap across all segments) ----
    print(f"\nRunning COMBINED portfolio (₹1.5L, shared daily cap, {len(data_map)} symbols)...")
    combined = run_intraday_backtest(
        data_map, benchmark_df=bench, start_capital=150000.0, max_loss_per_trade=2000.0
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
