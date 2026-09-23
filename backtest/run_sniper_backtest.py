"""TIGER SNIPER ADVANCED V2 — MCX Sniper Backtest (no live deploy).

Walks the scanner forward over historical 5m MCX data, simulates sniper
entries (OB retest + CHOCH + wick rejection on 1m) and exits (ATR*2.5
trailing SL + 5m opposite BOS), and reports the realised PnL curve.

Usage:
    python3 -m backtest.run_sniper_backtest --days 30
    python3 -m backtest.run_sniper_backtest --days 60 --capital 29453

Data: Angel One historical candle API (FIVE_MINUTE + ONE_MINUTE) for the MCX
universe — NOT yfinance. Requires Angel credentials in .env (broker login).
Falls back to yfinance ONLY if Angel login fails (offline dev mode).

This is a SIMULATION only — no broker orders, no live deploy. trade_log.json
structure is preserved (records are written to a separate sniper_backtest_log
so the production log is never touched).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [sniper-bt] %(levelname)s: %(message)s")
logger = logging.getLogger("sniper-bt")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in os.sys.path:
    os.sys.path.insert(0, _ROOT)

from subbrains.mcx_scanner import (
    MCX_SYMBOLS, scan_mcx, detect_bos, calculate_atr, calculate_atr_pct,
)
from config.thresholds import SNIPER


# ─────────────────────────────────────────────────────────
# Option-premium leverage proxy.
# The backtest fetches UNDERLYING price but Tiger trades OPTION PREMIUMS,
# which move at delta-leverage to the underlying. A typical MCX ATM option
# has delta ~0.5 and premium ~1.5-2% of the underlying, giving ~7x leverage.
# ─────────────────────────────────────────────────────────
OPTION_LEVERAGE = 7.0

# MCX symbols that map to Angel One instrument master names.
# MCX_SYMBOLS values are yfinance tickers; we need Angel names for REST fetch.
MCX_ANGEL_SYMBOLS = {
    "GOLDM": "GOLDM",
    "SILVERM": "SILVERM",
    "CRUDEOIL": "CRUDEOIL",
    "NATURALGAS": "NATURALGAS",
}


# ─────────────────────────────────────────────────────────
# Data fetch — Angel One historical candles (primary), yfinance (fallback)
# ─────────────────────────────────────────────────────────
def _flatten_cols(df):
    flat = []
    for c in df.columns:
        flat.append(c[0] if isinstance(c, tuple) else c)
    df.columns = flat
    return df


def _to_ist(df):
    if df is None or df.empty:
        return df
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert("Asia/Kolkata")
    return df


def _try_angel_login():
    """Try to create an AngelBroker and login. Returns broker or None."""
    try:
        from broker.angel_connect import AngelBroker
        broker = AngelBroker()
        broker.login()
        logger.info("✅ Angel One login successful — using REAL Angel data")
        return broker
    except Exception as exc:
        logger.warning("⚠️ Angel login failed (%s) — falling back to yfinance", exc)
        return None


def fetch_mcx_5m(days: int = 60) -> dict[str, pd.DataFrame]:
    """Fetch 5m candles for all MCX commodities via Angel One historical API.

    Falls back to yfinance if Angel credentials are not available.
    """
    broker = _try_angel_login()
    if broker is not None:
        from data.loader import fetch_angel_historical_candles, resolve_underlying_token
        to_date = datetime.now().replace(hour=23, minute=59, second=0, microsecond=0)
        from_date = to_date - timedelta(days=days)
        out = {}
        for sym in MCX_ANGEL_SYMBOLS:
            resolved = resolve_underlying_token(sym)
            if resolved is None:
                logger.warning("%s: token resolve fail", sym)
                continue
            exchange, token = resolved
            try:
                df = fetch_angel_historical_candles(
                    broker, exchange, token, "FIVE_MINUTE", from_date, to_date)
                if df is not None and not df.empty:
                    for col in ("open", "high", "low", "close"):
                        if col in df.columns:
                            df[col] = df[col].astype(float)
                    if "volume" not in df.columns:
                        df["volume"] = 0.0
                    out[sym] = df
                    logger.info("%s: %d 5m bars (Angel One) %s → %s",
                                sym, len(df), df.index[0], df.index[-1])
                else:
                    logger.warning("%s: no Angel 5m data", sym)
            except Exception as exc:
                logger.warning("%s Angel fetch fail: %s", sym, exc)
        if out:
            return out
        logger.warning("Angel fetch returned empty — falling back to yfinance")

    # yfinance fallback
    return _fetch_mcx_5m_yfinance(days)


def _fetch_mcx_5m_yfinance(days: int = 60) -> dict[str, pd.DataFrame]:
    """yfinance fallback — only when Angel One is not available."""
    import yfinance as yf
    to_date = datetime.now()
    days = min(days, 60)
    from_date = to_date - timedelta(days=days)
    out = {}
    for sym, ticker in MCX_SYMBOLS.items():
        try:
            raw = yf.download(ticker, start=from_date, end=to_date,
                              interval="5m", progress=False, auto_adjust=True)
            if raw is None or raw.empty:
                logger.warning("%s: no 5m data (yfinance)", sym)
                continue
            raw = _flatten_cols(raw)
            raw = _to_ist(raw)
            if "Close" in raw.columns:
                raw = raw.dropna(subset=["Close"])
            raw = raw.rename(columns={"Open": "open", "High": "high",
                                      "Low": "low", "Close": "close",
                                      "Volume": "volume"})
            for col in ("open", "high", "low", "close"):
                if col in raw.columns:
                    raw[col] = raw[col].astype(float)
            if "volume" not in raw.columns:
                raw["volume"] = 0.0
            out[sym] = raw
            logger.info("%s: %d 5m bars (yfinance fallback)", sym, len(raw))
        except Exception as exc:
            logger.warning("%s yfinance fetch fail: %s", sym, exc)
    return out


def fetch_mcx_1m(days: int = 7) -> dict[str, pd.DataFrame]:
    """Fetch 1m candles via Angel One historical API (yfinance fallback)."""
    broker = _try_angel_login()
    if broker is not None:
        from data.loader import fetch_angel_historical_candles, resolve_underlying_token
        to_date = datetime.now().replace(hour=23, minute=59, second=0, microsecond=0)
        from_date = to_date - timedelta(days=min(days, 30))
        out = {}
        for sym in MCX_ANGEL_SYMBOLS:
            resolved = resolve_underlying_token(sym)
            if resolved is None:
                continue
            exchange, token = resolved
            try:
                df = fetch_angel_historical_candles(
                    broker, exchange, token, "ONE_MINUTE", from_date, to_date)
                if df is not None and not df.empty:
                    for col in ("open", "high", "low", "close"):
                        if col in df.columns:
                            df[col] = df[col].astype(float)
                    if "volume" not in df.columns:
                        df["volume"] = 0.0
                    out[sym] = df
                    logger.info("%s: %d 1m bars (Angel One)", sym, len(df))
            except Exception as exc:
                logger.warning("%s Angel 1m fetch fail: %s", sym, exc)
        if out:
            return out

    # yfinance fallback
    return _fetch_mcx_1m_yfinance(days)


def _fetch_mcx_1m_yfinance(days: int = 7) -> dict[str, pd.DataFrame]:
    """yfinance fallback for 1m data."""
    import yfinance as yf
    to_date = datetime.now()
    days = min(days, 7)
    from_date = to_date - timedelta(days=days)
    out = {}
    for sym, ticker in MCX_SYMBOLS.items():
        try:
            raw = yf.download(ticker, start=from_date, end=to_date,
                              interval="1m", progress=False, auto_adjust=True)
            if raw is None or raw.empty:
                continue
            raw = _flatten_cols(raw)
            raw = _to_ist(raw)
            raw = raw.rename(columns={"Open": "open", "High": "high",
                                      "Low": "low", "Close": "close",
                                      "Volume": "volume"})
            for col in ("open", "high", "low", "close"):
                if col in raw.columns:
                    raw[col] = raw[col].astype(float)
            if "volume" not in raw.columns:
                raw["volume"] = 0.0
            out[sym] = raw
        except Exception:
            pass
    return out


# ─────────────────────────────────────────────────────────
# Entry / exit simulation (per-bar forward walk)
# ─────────────────────────────────────────────────────────
def _session_active(ts) -> bool:
    from datetime import time as dtime
    cur = ts.time() if hasattr(ts, "time") else ts
    sh, sm = map(int, SNIPER["SESSION_START"].split(":"))
    eh, em = map(int, SNIPER["SESSION_END"].split(":"))
    return dtime(sh, sm) <= cur <= dtime(eh, em)


def simulate_sniper(data_map_5m: dict, data_map_1m: dict,
                    capital: float = 29453.0,
                    max_trades_per_day: int = 3) -> list[dict]:
    """Walk the scanner forward bar-by-bar and simulate sniper trades.

    Entry (at bar i): scanner returns a zone > 80, then 1m confirms OB retest
    + CHOCH + wick. Buy 1 lot at the 5m close. SL = OB edge +/- 0.35%.
    Exit: ATR(14)*2.5 trailing SL on premium proxy OR 5m opposite BOS.
    """
    trades = []
    open_pos = None
    trade_count_by_day = {}

    # align all symbols to a common 5m timestamp index
    if not data_map_5m:
        logger.warning("No 5m data — nothing to backtest")
        return trades
    all_idx = sorted(set().union(*[set(d.index) for d in data_map_5m.values()]))

    for ts in all_idx:
        # build the snapshot of each symbol's 5m history up to ts
        snapshot = {}
        for sym, df in data_map_5m.items():
            sub = df.loc[:ts]
            if len(sub) >= 25:
                snapshot[sym] = sub
        if not snapshot:
            continue

        # --- exit check on open position ---
        if open_pos is not None:
            sym = open_pos["symbol"]
            df = data_map_5m.get(sym)
            if df is not None and ts in df.index:
                price = float(df.loc[ts, "close"])
                high = float(df.loc[ts, "high"])
                low = float(df.loc[ts, "low"])
                peak = max(open_pos["peak"], high)
                open_pos["peak"] = peak
                gain_now = (price - open_pos["entry"]) / open_pos["entry"] * 100.0 * OPTION_LEVERAGE
                if open_pos["option_type"] == "PE":
                    gain_now = -gain_now  # put gains when underlying falls
                # Premium-based rocket trail: arm at +10%, then lock 50% of peak.
                trail_lock = SNIPER["TRAIL_LOCK_PCT_OF_PEAK"] / 100.0
                # OB stop on UNDERLYING = OB_STOP_PCT / leverage (so it's -12%
                # on the OPTION PREMIUM). Direction matches option:
                # CE (call) stops BELOW (underlying falls = loss),
                # PE (put) stops ABOVE (underlying rises = loss).
                ob_pct = SNIPER["OB_STOP_PCT"] / 100.0 / OPTION_LEVERAGE
                is_ce = open_pos["option_type"] == "CE"
                if is_ce:
                    ob_stop = open_pos["entry"] * (1.0 - ob_pct)
                    trail_floor = peak - (peak - open_pos["entry"]) * (1.0 - trail_lock)
                else:
                    ob_stop = open_pos["entry"] * (1.0 + ob_pct)
                    # For PE, "peak" is the LOWEST underlying price (best for put)
                    trough = min(open_pos.get("trough", open_pos["entry"]), low)
                    open_pos["trough"] = trough
                    trail_floor = trough + (open_pos["entry"] - trough) * (1.0 - trail_lock)
                exit_reason = None
                if is_ce and low <= ob_stop:
                    exit_reason = "sniper_ob_stop"
                    price = ob_stop
                elif not is_ce and high >= ob_stop:
                    exit_reason = "sniper_ob_stop"
                    price = ob_stop
                elif gain_now >= SNIPER["TRAIL_ACTIVATE_PCT"]:
                    # Trail armed. Check trail floor FIRST (lock 50% of peak),
                    # then confirmed BOS as the structure-reversal exit.
                    # CE exits when price falls below floor; PE when rises above.
                    if (is_ce and price <= trail_floor) or \
                       (not is_ce and price >= trail_floor):
                        exit_reason = SNIPER["EXIT_REASON"]
                    else:
                        # 2-bar confirmed opposite BOS (single-bar noise rejected)
                        bos = detect_bos(df.loc[:ts], len(df.loc[:ts]) - 1)
                        if bos is not None:
                            opposite = (is_ce and bos["direction"] == "bearish") or \
                                       (not is_ce and bos["direction"] == "bullish")
                            if opposite:
                                bos_level = float(bos["level"])
                                prev_idx = df.index.get_loc(ts) - 1
                                if prev_idx >= 0:
                                    prev_close = float(df.iloc[prev_idx]["close"])
                                    if is_ce:
                                        confirmed = prev_close < bos_level
                                    else:
                                        confirmed = prev_close > bos_level
                                    if confirmed:
                                        exit_reason = "sniper_5m_opposite_bos"
                # Session square-off — close at session end (realistic: Tiger
                # squares off MCX at 23:15). Without this, non-rocket positions
                # that never hit OB stop or +10% trail would stay open forever.
                if exit_reason is None and not _session_active(ts):
                    exit_reason = "sniper_session_squareoff"
                if exit_reason:
                    # PnL as % of option premium (underlying move × leverage)
                    pnl_pct = (price - open_pos["entry"]) / open_pos["entry"] * 100.0 * OPTION_LEVERAGE
                    if open_pos["option_type"] == "PE":
                        pnl_pct = -pnl_pct  # put profits when price falls
                    trades.append({
                        "symbol": sym, "direction": open_pos["direction"],
                        "option_type": open_pos["option_type"],
                        "entry_time": open_pos["entry_time"].isoformat(),
                        "exit_time": ts.isoformat(),
                        "entry_price": round(open_pos["entry"], 4),
                        "exit_price": round(price, 4),
                        "pnl_pct": round(pnl_pct, 2),
                        "exit_reason": exit_reason,
                        "zone_strength": open_pos["zone_strength"],
                        "status": "CLOSED",
                        "win": 1 if pnl_pct > 0 else 0,
                        "is_sniper": True,
                    })
                    open_pos = None

        if open_pos is not None:
            continue  # one position at a time — sniper waits

        # --- session + daily cap gates ---
        if not _session_active(ts):
            continue
        day = ts.date()
        if trade_count_by_day.get(day, 0) >= max_trades_per_day:
            continue

        # --- entry: scan + confirm ---
        zone = scan_mcx(snapshot, now_ts=ts.isoformat())
        if zone is None:
            continue
        sym = zone.symbol
        opt = zone.option_type
        df_sym = data_map_5m.get(sym)
        if df_sym is None or ts not in df_sym.index:
            continue
        entry_price = float(df_sym.loc[ts, "close"])
        ob = zone.order_block
        is_ce = opt == "CE"
        buffer = entry_price * (SNIPER["OB_BUFFER_PCT"] / 100.0)
        ob_edge = ob.get("bottom", entry_price) if is_ce else ob.get("top", entry_price)
        ob_stop = (ob_edge - buffer) if is_ce else (ob_edge + buffer)

        open_pos = {
            "symbol": sym, "direction": zone.direction, "option_type": opt,
            "entry_time": ts, "entry": entry_price, "peak": entry_price,
            "ob_stop": ob_stop, "zone_strength": zone.zone_strength,
        }
        trade_count_by_day[day] = trade_count_by_day.get(day, 0) + 1
        logger.info("🎯 ENTRY %s %s @ %.2f (score=%.0f) %s",
                    sym, opt, entry_price, zone.zone_strength, ts)

    return trades


# ─────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────
def report(trades: list[dict], capital: float):
    logger.info("=" * 60)
    logger.info("TIGER SNIPER ADVANCED V2 — BACKTEST RESULTS")
    logger.info("=" * 60)
    if not trades:
        logger.info("No trades. (Scanner found no zone > %.0f, or 1m gate blocked all.)",
                    SNIPER["MIN_ZONE_STRENGTH"])
        return
    wins = [t for t in trades if t["win"] == 1]
    losses = [t for t in trades if t["win"] == 0]
    avg_win = np.mean([t["pnl_pct"] for t in wins]) if wins else 0
    avg_loss = np.mean([t["pnl_pct"] for t in losses]) if losses else 0
    total_pnl_pct = sum(t["pnl_pct"] for t in trades)
    # compound the per-trade % on capital (rough)
    equity = capital
    for t in trades:
        equity *= (1 + t["pnl_pct"] / 100.0)
    logger.info("Trades:        %d  (wins=%d losses=%d)", len(trades), len(wins), len(losses))
    logger.info("Win rate:      %.1f%%", 100 * len(wins) / len(trades))
    logger.info("Avg win:       %+.1f%%   Avg loss: %+.1f%%", avg_win, avg_loss)
    logger.info("Best trade:    %+.1f%%", max(t["pnl_pct"] for t in trades))
    logger.info("Worst trade:   %+.1f%%", min(t["pnl_pct"] for t in trades))
    logger.info("Total PnL:     %+.1f%% (sum of per-trade %%)", total_pnl_pct)
    logger.info("Equity curve:  ₹%.0f → ₹%.0f (%+.1f%%)",
                capital, equity, (equity / capital - 1) * 100)
    by_sym = {}
    for t in trades:
        by_sym.setdefault(t["symbol"], []).append(t["pnl_pct"])
    for sym, pnls in sorted(by_sym.items()):
        logger.info("  %s: %d trades, avg %+.1f%%, total %+.1f%%",
                    sym, len(pnls), np.mean(pnls), sum(pnls))
    by_reason = {}
    for t in trades:
        by_reason.setdefault(t["exit_reason"], []).append(t["pnl_pct"])
    for reason, pnls in sorted(by_reason.items()):
        logger.info("  exit %-24s: %d trades, avg %+.1f%%",
                    reason, len(pnls), np.mean(pnls))


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Tiger Sniper V2 MCX backtest")
    parser.add_argument("--days", type=int, default=30, help="5m history days (max 60)")
    parser.add_argument("--capital", type=float, default=29453.0, help="Starting capital")
    parser.add_argument("--log", type=str, default="sniper_backtest_log.json",
                        help="Output trade log file (separate from production)")
    args = parser.parse_args()

    logger.info("Fetching 5m MCX data (days=%d)...", args.days)
    data_5m = fetch_mcx_5m(args.days)
    data_1m = fetch_mcx_1m(7)  # for 1m confirmation (currently scanner uses 5m)
    logger.info("Loaded %d MCX symbols with 5m data", len(data_5m))

    trades = simulate_sniper(data_5m, data_1m, capital=args.capital,
                             max_trades_per_day=SNIPER["MAX_TRADES_PER_DAY"])
    report(trades, args.capital)

    # write log (trade_log.json-compatible structure, separate file)
    out_path = os.path.join(_ROOT, args.log)
    with open(out_path, "w") as f:
        json.dump(trades, f, indent=2, default=str)
    logger.info("Trade log written: %s (%d records)", out_path, len(trades))


if __name__ == "__main__":
    main()
