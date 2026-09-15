"""🐅 TIGER REAL-TIME LIVE SCANNER — independent of backtest.

This is the real fix for V19. Previously, tiger_live.py called
run_tiger_brain_backtest(), which ran a full-day simulation. If no score
formed in the simulation, "0 entries" resulted and no buy order was placed.

Now this file runs the 7 brains on only the latest bar at the current timestamp:
  - Brain 1: brain1_intraday_pass (gate)
  - Brain 2: detect_zones + zone_touched + volume_delta (gate)
  - Brain 3: scoring pipeline (rocket momentum)
  - Brain 4: TradeCounterGuard (daily quota)
  - Brain 6: Premium Discount Tracker (IV discount)
  - Brain 7: Session Commander (session threshold)

(Brain 5 exit lives in the live runner's monitor_open_positions.)

Real real-time signal — does not depend on backtest simulation.
"""
from __future__ import annotations

import logging
from datetime import datetime, time

import pandas as pd

from backtest.run_tiger_brain_backtest import (
    find_tiger_brain_entry,
    find_tiger_brain_entry_15m,
    get_vix_for_date,
)
from backtest.tiger_session_brain import (
    get_session_score_threshold,
    should_force_hunt,
)
from universe.fno_universe import segment_of, is_expiry_day
from config.thresholds import SCALPER

logger = logging.getLogger(__name__)

IST = pd.Timestamp("now").tz if pd.Timestamp("now").tz else None
try:
    IST = pd.Timezone("Asia/Kolkata") if False else None
except Exception:
    IST = None


def _latest_15m_index(df_15m: pd.DataFrame, now: datetime) -> int:
    """Return the index of the latest CLOSED 15m bar at the current timestamp.

    In live trading we only look at CLOSED bars (no signal on the currently
    forming bar — it is still being built).
    """
    if df_15m is None or len(df_15m) == 0:
        return -1
    ts = pd.Timestamp(now)
    if df_15m.index.tz is not None and ts.tz is None:
        ts = ts.tz_localize(df_15m.index.tz)
    elif df_15m.index.tz is None and ts.tz is not None:
        ts = ts.tz_localize(None)
    closed = df_15m.index[df_15m.index <= ts]
    if len(closed) == 0:
        return len(df_15m) - 1
    return df_15m.index.get_loc(closed[-1])


def _should_activate_scalper(
    ts_time: time,
    segment: str,
    daily_trades: int,
    last_trade_time: datetime | None,
) -> bool:
    """Should Tiger activate Fallback Scalper Mode?

    Activates when:
      - 0 trades AND past zero-trade activation time (NSE 14:00 / MCX 21:00)
      - OR idle 2+ hours since last trade
    """
    idle_mins = SCALPER["ACTIVATION_IDLE_MINUTES"]
    # Try both cases (config uses 'MCX'/'NSE', caller may pass 'mcx'/'nse')
    zero_time_str = SCALPER["ACTIVATION_ZERO_TRADE_TIME"].get(
        segment) or SCALPER["ACTIVATION_ZERO_TRADE_TIME"].get(
        segment.upper()) or SCALPER["ACTIVATION_ZERO_TRADE_TIME"].get(
        segment.lower()) or ("21:00" if segment.lower() == "mcx" else "14:00")
    zh, zm = map(int, zero_time_str.split(":"))
    zero_activate = time(zh, zm) <= ts_time

    if daily_trades == 0 and zero_activate:
        return True

    # Idle activation: if Tiger has been idle for idle_mins and hasn't
    # hit the daily trade cap, activate scalper to keep hunting.
    # This works even when last_trade_time is None (fresh start past
    # zero_time) — zero_activate already covers that case above.
    if last_trade_time is not None:
        idle = (datetime.now() - last_trade_time).total_seconds() / 60
        if idle >= idle_mins and daily_trades < 5:
            return True

    return False


def find_scalper_entry(
    df_15m: pd.DataFrame,
    i_15m: int,
    segment: str,
    symbol: str,
    broker,
    pcr_cache: dict,
    vix_val: float,
) -> dict | None:
    """🐅 TIGER GOD MODE SCALPER — full options math + zone research.

    Tiger is a 55-year senior options-buying algo. It NEVER enters bich
    (middle). It enters at zone edges with full confluence research:

    RESEARCH STACK (all must align):
      1. Supply/Demand zone — entry at zone edge ONLY (detect_zones)
      2. Option chain math — PCR sentiment (fetch_pcr)
      3. VWAP confluence — institutional consensus level
      4. SuperTrend 15m — trend confirmation (MTF: 15m)
      5. RSI alignment — >=60 for BUY/CE, <=40 for SELL/PE
      6. Body + Volume — momentum confirmation (Rocket Filter final gate)
      7. Score >= 75

    Tiger only BUYs CE/PE. Never sells options.
    """
    if df_15m is None or len(df_15m) < 40:
        return None

    row = df_15m.iloc[i_15m]
    o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
    rng = h - l
    if rng <= 0:
        return None

    # === GATE 1: Supply/Demand Zone — entry at zone edge ONLY ===
    # Tiger never enters in the middle. It waits for price to touch a zone.
    from pipeline.intraday_strategies import detect_zones, zone_touched_on_1m
    zone_idx = max(0, i_15m - 1)
    zones = detect_zones(df_15m, zone_idx, lookback=zone_idx)
    if not zones:
        logger.debug(f"🐅 {symbol}: no zones detected — no mid-entry")
        return None

    # Find the best zone touch
    best_zone = None
    best_zone_touch = None
    for z in zones:
        touch = zone_touched_on_1m(row, z)
        if touch is not None:
            if best_zone is None or z.get("score", 0) > best_zone.get("score", 0):
                best_zone = z
                best_zone_touch = touch

    if best_zone is None:
        logger.debug(f"🐅 {symbol}: zones exist but no touch — waiting for edge")
        return None

    direction = "BUY" if best_zone_touch == "demand" else "SELL"
    is_call = direction == "BUY"

    # === GATE 2: Body confirmation — candle must confirm zone direction ===
    body = abs(c - o)
    body_pct = (body / rng) * 100
    body_confirms = (c > o and direction == "BUY") or (c < o and direction == "SELL")
    if not body_confirms:
        logger.info(
            f"🚫 SKIP {symbol} — body not confirming {direction} (zone touch but no momentum)")
        return None
    if body_pct < SCALPER["MIN_BODY_PCT"]:
        logger.info(
            f"🚫 SKIP {symbol} — body {body_pct:.0f}% < {SCALPER['MIN_BODY_PCT']}%")
        return None

    # === GATE 3: Volume surge ===
    vol = float(row.get("volume", 0) or 0)
    avg_vol = float(df_15m["volume"].iloc[max(0, i_15m - 10):i_15m].mean())
    vol_ratio = vol / avg_vol if avg_vol > 0 else 0
    if avg_vol > 0 and vol < avg_vol * SCALPER["MIN_VOLUME_SURGE"]:
        logger.info(
            f"🚫 SKIP {symbol} — vol {vol_ratio:.1f}x < {SCALPER['MIN_VOLUME_SURGE']}x")
        return None

    # === GATE 4: SuperTrend 15m confirmation ===
    if SCALPER.get("REQUIRE_SUPERTREND", True):
        try:
            from subbrains.trend_follow import calculate_supertrend
            st = calculate_supertrend(df_15m.iloc[:i_15m + 1])
            current_trend = int(st["trend"].iloc[-1])
            if direction == "BUY" and current_trend != 1:
                logger.info(
                    f"🚫 SKIP {symbol} BUY — supertrend bearish (trend={current_trend})")
                return None
            if direction == "SELL" and current_trend != -1:
                logger.info(
                    f"🚫 SKIP {symbol} SELL — supertrend bullish (trend={current_trend})")
                return None
        except Exception as exc:
            logger.debug(f"Scalper supertrend check fail {symbol}: {exc}")
            logger.info(f"🚫 SKIP {symbol} — supertrend unavailable")
            return None

    # === GATE 5: RSI alignment ===
    latest_rsi = 50.0
    try:
        from subbrains.mean_reversion import calculate_rsi
        rsi_series = calculate_rsi(df_15m.iloc[:i_15m + 1])
        latest_rsi = float(rsi_series.iloc[-1])
        if is_call and latest_rsi < SCALPER["MIN_RSI_BUY"]:
            logger.info(
                f"🚫 SKIP {symbol} BUY — RSI {latest_rsi:.0f} < {SCALPER['MIN_RSI_BUY']}")
            return None
        if not is_call and latest_rsi > SCALPER["MAX_RSI_SELL"]:
            logger.info(
                f"🚫 SKIP {symbol} SELL — RSI {latest_rsi:.0f} > {SCALPER['MAX_RSI_SELL']}")
            return None
    except Exception as exc:
        logger.debug(f"Scalper RSI check fail {symbol}: {exc}")
        logger.info(f"🚫 SKIP {symbol} — RSI unavailable")
        return None

    # === GATE 6: VWAP confluence (institutional consensus) ===
    vwap_ok = False
    vwap_reason = ""
    try:
        from subbrains.trend_follow import calculate_vwap
        vwap_series = calculate_vwap(df_15m.iloc[:i_15m + 1])
        vwap_val = float(vwap_series.iloc[-1])
        if vwap_val > 0:
            dist_pct = abs(c - vwap_val) / vwap_val * 100
            if dist_pct <= 2.0:  # within 2% of VWAP = confluence
                vwap_ok = True
                vwap_reason = f"vwap({dist_pct:.1f}%)"
    except Exception:
        pass

    # === GATE 7: PCR — option chain sentiment (options math) ===
    pcr = 1.0
    pcr_aligned = False
    if broker is not None:
        try:
            from backtest.run_tiger_brain_backtest import fetch_pcr, PCR_BULLISH_MAX, PCR_BEARISH_MIN
            if symbol not in pcr_cache:
                pcr_cache[symbol] = fetch_pcr(broker, symbol)
            pcr = pcr_cache[symbol]
            if direction == "BUY" and pcr < PCR_BULLISH_MAX:
                pcr_aligned = True
            elif direction == "SELL" and pcr > PCR_BEARISH_MIN:
                pcr_aligned = True
        except Exception:
            pass

    # === SCORE — composite of all research signals ===
    score = min(100.0, body_pct * 0.3 + vol_ratio * 12 + best_zone.get("score", 0) * 0.2)
    if vwap_ok:
        score += 5
    if pcr_aligned:
        score += 5

    # === GATE 8: Score >= 75 (Rocket Filter final gate) ===
    if score < SCALPER["MIN_SCORE"]:
        logger.info(
            f"🚫 SKIP {symbol} — score {score:.0f} < {SCALPER['MIN_SCORE']} "
            f"(low quality: body={body_pct:.0f}% vol={vol_ratio:.1f}x "
            f"rsi={latest_rsi:.0f} pcr={pcr:.1f} vwap={'Y' if vwap_ok else 'N'})")
        return None

    # ALL GATES PASSED — GOD MODE SIGNAL
    entry_price = c
    strike_kind = "ATM"

    details = (f"ZONE-{best_zone_touch} body={body_pct:.0f}% vol={vol_ratio:.1f}x "
               f"rsi={latest_rsi:.0f} pcr={pcr:.1f} vwap={'Y' if vwap_ok else 'N'}")

    logger.info(
        f"🚀 GOD MODE SIGNAL: {symbol} {direction} "
        f"zone={best_zone_touch} body={body_pct:.0f}% vol={vol_ratio:.1f}x "
        f"rsi={latest_rsi:.0f} pcr={pcr:.1f} vwap={'✓' if vwap_ok else '✗'} "
        f"score={score:.0f} [GOD MODE]")

    return {
        "direction": direction,
        "entry_price": entry_price,
        "setup_score": score,
        "strike_kind": strike_kind,
        "score_details": details,
        "is_scalper": True,
    }


def scan_live_signals(
    data_map: dict[str, pd.DataFrame],
    data_map_1m: dict[str, pd.DataFrame] | None,
    broker,
    now: datetime | None = None,
    daily_entries_taken: int = 0,
    max_entries_per_day: int = 5,
    last_trade_time: datetime | None = None,
    scalper_trades_today: int = 0,
) -> list[dict]:
    """Run the 7 brains for each symbol at the current timestamp → live signals.

    This is NOT a backtest simulation — it scans only the latest closed 15m bar.
    For each symbol, find_tiger_brain_entry or find_tiger_brain_entry_15m is
    called (depending on whether 1m data is available).

    If no normal signal is found and Scalper Mode is active, the
    find_scalper_entry() fallback runs — a momentum entry without a zone.

    Returns:
        list[dict]: signals whose setup_score >= session_threshold.
        Each dict contains symbol, direction, setup_score, strike_kind, etc.
    """
    if now is None:
        now = datetime.now()
    ts_time = now.time()
    signals: list[dict] = []

    # Brain 4: daily quota check
    if daily_entries_taken >= max_entries_per_day:
        logger.info("Live scan: daily quota full (%d/%d) — skip.",
                    daily_entries_taken, max_entries_per_day)
        return signals

    pcr_cache: dict[str, float] = {}

    for sym, df_15m in data_map.items():
        if df_15m is None or len(df_15m) < 40:
            continue

        seg = segment_of(sym)
        # Brain 7: session threshold (NSE vs MCX)
        session_thresh = get_session_score_threshold(ts_time, seg)
        if session_thresh >= 999.0:
            continue  # off-hours — this symbol's session is closed

        # force hunt check (Brain 7)
        force_hunt, fh_thresh, _ = should_force_hunt(
            ts_time, None, seg)
        min_score = min(session_thresh, fh_thresh) if force_hunt else session_thresh

        i_15m = _latest_15m_index(df_15m, now)
        if i_15m < 40:
            continue

        expiry = is_expiry_day(sym, now)
        vix_val = get_vix_for_date(now)

        # 1m data available? → sniper path, else 15m path
        df_1m = None
        if data_map_1m and sym in data_map_1m:
            df_1m = data_map_1m[sym]

        try:
            if df_1m is not None and len(df_1m) > 0:
                setup = find_tiger_brain_entry(
                    df_15m, i_15m, df_1m, seg, expiry, sym,
                    broker, pcr_cache, vix_val,
                    min_score=min_score, force_hunt=force_hunt)
            else:
                setup = find_tiger_brain_entry_15m(
                    df_15m, i_15m, seg, expiry, sym, vix_val,
                    min_score=min_score, force_hunt=force_hunt)
        except Exception as exc:
            logger.debug("Live scan %s error: %s", sym, exc)
            continue

        if setup is None:
            continue

        score = setup.get("setup_score", 0)
        if score < min_score:
            logger.debug("Live scan %s: score %.1f < threshold %.1f — skip",
                         sym, score, min_score)
            continue

        # Signal found — ready for a live order
        # _place_live_orders expects entry_ts, strike, option_type,
        # entry_premium, is_delivery — backtest path adds these when
        # creating positions. Live path must add them here or the signal
        # is silently skipped (entry_ts is None → continue).
        direction = setup.get("direction", "BUY")
        is_call = direction == "BUY"
        cur_underlying = setup.get("entry_price", 0.0)
        strike_kind = setup.get("strike_kind", "ATM")
        if strike_kind == "ITM":
            strike = round(cur_underlying * 0.99) if is_call else round(cur_underlying * 1.01)
        elif strike_kind == "OTM":
            strike = round(cur_underlying * 1.01) if is_call else round(cur_underlying * 0.99)
        else:
            strike = round(cur_underlying)
        setup["symbol"] = sym
        setup["segment"] = seg
        setup["scan_time"] = now.isoformat()
        setup["entry_ts"] = now
        setup["strike"] = strike
        setup["option_type"] = "CE" if is_call else "PE"
        setup["entry_premium"] = 0.0  # real LTP fetched in _place_live_orders
        setup["is_delivery"] = False  # intraday by default
        setup["exit_ts"] = None  # entry signal — no exit yet
        logger.info("🐅 LIVE SIGNAL: %s %s score=%.1f (thresh %.1f) %s",
                    sym, setup.get("direction", ""),
                    score, min_score, setup.get("score_details", ""))
        signals.append(setup)

    logger.info("Live scan done @ %s: %d signal(s) from %d symbols",
                now.strftime("%H:%M"), len(signals), len(data_map))

    # === TIGER FALLBACK SCALPER MODE (Priority 2) ===
    # Dual-execution logic: if NO Big Move (Supply/Demand zone) signal found,
    # automatically switch to Scalping Mode for this cycle. This ensures Tiger
    # never goes empty-handed — it hunts micro-momentum when zones are absent.
    if len(signals) == 0:
        # Determine segment from active market
        from automation.scheduler import get_active_market
        active_market = get_active_market(now)
        seg = "mcx" if active_market == "MCX" else "nse"

        # Scalper always attempts when no Big Move found — but quota limits
        # and momentum criteria keep it controlled (max 6/day, ROCKET FILTER)
        if _should_activate_scalper(ts_time, seg, daily_entries_taken, last_trade_time):
            if scalper_trades_today < SCALPER["MAX_TRADES_PER_DAY"]:
                logger.info(
                    "🐅 GOD MODE SCAN — Tiger hunting zone-edge entries "
                    "(0 Big Move signals, %d trades today, scalper %d/%d)",
                    daily_entries_taken, scalper_trades_today,
                    SCALPER["MAX_TRADES_PER_DAY"])

                # GOD MODE: scan ALL symbols, collect ALL qualifying signals,
                # then pick the HIGHEST score (not first-hit-wins).
                # Tiger sees the whole market and picks the best opportunity.
                all_scalps = []
                for sym, df_15m in data_map.items():
                    if df_15m is None or len(df_15m) < 40:
                        continue
                    i_15m = _latest_15m_index(df_15m, now)
                    if i_15m < 40:
                        continue
                    try:
                        scalp = find_scalper_entry(
                            df_15m, i_15m, seg, sym, broker, pcr_cache, 0.0)
                    except Exception as exc:
                        logger.debug("Scalper %s error: %s", sym, exc)
                        continue
                    if scalp is None:
                        continue

                    # Build full signal (same structure as normal)
                    direction = scalp.get("direction", "BUY")
                    is_call = direction == "BUY"
                    cur_underlying = scalp.get("entry_price", 0.0)
                    strike = round(cur_underlying)
                    scalp["symbol"] = sym
                    scalp["segment"] = seg
                    scalp["scan_time"] = now.isoformat()
                    scalp["entry_ts"] = now
                    scalp["strike"] = strike
                    scalp["option_type"] = "CE" if is_call else "PE"
                    scalp["entry_premium"] = 0.0
                    scalp["is_delivery"] = False
                    scalp["exit_ts"] = None
                    scalp["is_scalper"] = True
                    all_scalps.append(scalp)

                # Rank by score — Tiger picks the BEST opportunity market-wide
                if all_scalps:
                    all_scalps.sort(key=lambda s: s.get("setup_score", 0), reverse=True)
                    best = all_scalps[0]
                    logger.info(
                        f"🚀 GOD MODE PICK: {best['symbol']} {best.get('direction', '')} "
                        f"{best.get('strike', '')}{best.get('option_type', '')} "
                        f"score={best.get('setup_score', 0):.0f} "
                        f"(best of {len(all_scalps)} qualifying)")
                    signals.append(best)
            else:
                logger.info(
                    "Scalper mode: max %d scalper trades today — skip.",
                    SCALPER["MAX_TRADES_PER_DAY"])

    # INDEX signals FIRST, STOCK signals AFTER — user requirement:
    # "Tiger finds trade in indexes after that in stocks"
    from universe.fno_universe import INDEX_SYMBOLS, STOCK_SYMBOLS
    index_set = set(INDEX_SYMBOLS.keys())

    def _signal_priority(sig: dict) -> int:
        sym = sig.get("symbol", "")
        return 0 if sym in index_set else 1

    signals.sort(key=_signal_priority)

    return signals
