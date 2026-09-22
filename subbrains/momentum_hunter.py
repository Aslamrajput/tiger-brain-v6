"""
Tiger V19 — Momentum Hunter 🐅🚀
================================

Tiger ka wo dimaag jo kabhi nahi soota. Har symbol pe nazar,
har tick pe kaan. Jab market mein kahin bhi momentum dikhe —
Tiger wahin entry leta hai. Right time, right price, full rocket.

STRATEGY LAYERS:
  1. Opening Range Breakout (ORB) — 9:15-9:30 range build,
     breakout with volume surge = Tiger catches the morning rocket
  2. Momentum Spike Detection — 3-bar acceleration with volume
     explosion = Tiger catches mid-morning momentum
  3. VWAP Reclaim — price reclaiming VWAP with conviction =
     Tiger catches institutional re-entry
  4. Options Math Gate — IV percentile + delta estimate = Tiger
     NEVER buys overpriced premium

ENTRY TRIGGER:
  All layers run in parallel. Any ONE firing = signal.
  But options math gate is MANDATORY — Tiger never enters
  without checking premium fairness.

Tiger Options Buying Only. CE = bullish, PE = bearish.
Never sells options. Pure momentum → rocket.
"""
from __future__ import annotations

import logging
from datetime import datetime, time

import pandas as pd

logger = logging.getLogger(__name__)

# === ORB CONFIG ===
ORB_RANGE_MINUTES = 15          # 9:15-9:30 = opening range
ORB_BREAKOUT_VOL_MULT = 2.0     # volume must be 2x average
ORB_MIN_BODY_PCT = 50.0         # breakout candle body >= 50% of range

# === MOMENTUM SPIKE CONFIG ===
SPIKE_BAR_COUNT = 3             # look at last 3 bars
SPIKE_VOL_MULT = 1.8            # volume explosion threshold
SPIKE_MIN_RANGE_PCT = 0.8       # each bar must move >= 0.8% of price
SPIKE_DIRECTION_CONSISTENT = True  # all 3 bars same direction

# === VWAP RECLAIM CONFIG ===
VWAP_RECLAIM_MAX_DIST_PCT = 1.5  # within 1.5% of VWAP after reclaiming
VWAP_RECLAIM_MIN_VOL_MULT = 1.5  # reclaim candle volume >= 1.5x avg

# === OPTIONS MATH GATE ===
IV_EXPENSIVE_PERCENTILE = 85.0   # IV > 85th percentile = BLOCKED (was 65 — too strict)
IV_DISCOUNT_BONUS = 5.0          # IV < 35th percentile = +5 score bonus
DELTA_MIN_BUY = 0.10             # CE delta must be >= 0.10 (was 0.40 — allows cheap OTM)
DELTA_MAX_BUY = 0.85             # CE delta must be <= 0.85 (was 0.75 — wider band)
DELTA_MIN_SELL = 0.10            # |PE delta| must be >= 0.10 (was 0.40)
DELTA_MAX_SELL = 0.85            # |PE delta| must be <= 0.85 (was 0.75)


def _get_opening_range(df_1m: pd.DataFrame, now: datetime) -> tuple[float, float] | None:
    """Extract 9:15-9:30 opening range high/low from 1m data.

    Returns (range_high, range_low) or None if range not yet established.
    """
    if df_1m is None or len(df_1m) < 5:
        return None

    today = now.date()
    try:
        # Filter to today's candles within 9:15-9:30
        if not isinstance(df_1m.index, pd.DatetimeIndex):
            df_1m.index = pd.to_datetime(df_1m.index)
        day_mask = df_1m.index.date == today
        time_mask = (df_1m.index.time >= pd.Timestamp("09:15").time()) & \
                     (df_1m.index.time < pd.Timestamp("09:30").time())
        orb_df = df_1m[day_mask & time_mask]
        if len(orb_df) < 3:
            return None
        return float(orb_df["high"].max()), float(orb_df["low"].min())
    except Exception as exc:
        logger.debug("ORB range extract fail: %s", exc)
        return None


def detect_orb_breakout(
    df_15m: pd.DataFrame,
    i_15m: int,
    df_1m: pd.DataFrame | None,
    now: datetime,
) -> dict | None:
    """Opening Range Breakout detector.

    If price breaks above ORB high → BUY/CE signal.
    If price breaks below ORB low → SELL/PE signal.

    Volume must surge on breakout candle. Body must be strong.

    Time window: 9:30 AM to 11:30 AM (first 2 hours after range forms).
    ORB is a morning strategy — late-day range breaks are unreliable.
    After 11:30, momentum_spike + vwap_reclaim handle breakouts.
    """
    # === ORB TIME WINDOW — morning only (9:30-11:30 IST) ===
    # The 9:15-9:30 range is the anchor; breakouts are most explosive in
    # the first 2 hours. After 11:30, let momentum_spike/vwap_reclaim work.
    cur_time = now.time()
    if not (time(9, 30) <= cur_time <= time(11, 30)):
        return None

    orb_range = _get_opening_range(df_1m, now) if df_1m is not None else None
    if orb_range is None:
        return None

    orb_high, orb_low = orb_range
    row = df_15m.iloc[i_15m]
    o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
    rng = h - l
    if rng <= 0:
        return None

    body = abs(c - o)
    body_pct = (body / rng) * 100

    vol = float(row.get("volume", 0) or 0)
    avg_vol = float(df_15m["volume"].iloc[max(0, i_15m - 10):i_15m].mean()) if i_15m > 10 else vol
    vol_mult = vol / avg_vol if avg_vol > 0 else 0

    # Breakout ABOVE opening range high → BUY
    if h > orb_high and c > orb_high and body_pct >= ORB_MIN_BODY_PCT:
        if vol_mult >= ORB_BREAKOUT_VOL_MULT:
            score = min(100.0, body_pct * 0.4 + vol_mult * 15 + 30)
            # Structural stop: below ORB low (the bottom of the opening range)
            structural_stop = orb_low * 0.98
            logger.info(
                f"🚀 ORB BREAKOUT UP: high ₹{h:.2f} > ORB ₹{orb_high:.2f} "
                f"body={body_pct:.0f}% vol={vol_mult:.1f}x score={score:.0f}")
            return {
                "direction": "BUY",
                "entry_price": c,
                "setup_score": score,
                "strike_kind": "ATM",
                "score_details": f"ORB-UP body={body_pct:.0f}% vol={vol_mult:.1f}x",
                "strategy": "ORB_BREAKOUT",
                "structural_stop": structural_stop,
            }

    # Breakout BELOW opening range low → SELL
    if l < orb_low and c < orb_low and body_pct >= ORB_MIN_BODY_PCT:
        if vol_mult >= ORB_BREAKOUT_VOL_MULT:
            score = min(100.0, body_pct * 0.4 + vol_mult * 15 + 30)
            structural_stop = orb_high * 1.02
            logger.info(
                f"🚀 ORB BREAKOUT DOWN: low ₹{l:.2f} < ORB ₹{orb_low:.2f} "
                f"body={body_pct:.0f}% vol={vol_mult:.1f}x score={score:.0f}")
            return {
                "direction": "SELL",
                "entry_price": c,
                "setup_score": score,
                "strike_kind": "ATM",
                "score_details": f"ORB-DOWN body={body_pct:.0f}% vol={vol_mult:.1f}x",
                "strategy": "ORB_BREAKOUT",
                "structural_stop": structural_stop,
            }

    return None


def detect_momentum_spike(
    df_15m: pd.DataFrame,
    i_15m: int,
) -> dict | None:
    """3-bar momentum acceleration detector.

    Looks at the last 3 closed 15m bars. If all 3 move in the same
    direction with increasing volume and meaningful range, Tiger
    catches the accelerating momentum.

    This is Tiger's "rocket detection" — the moment momentum
    starts accelerating, Tiger is IN.
    """
    if i_15m < SPIKE_BAR_COUNT:
        return None

    bars = df_15m.iloc[i_15m - SPIKE_BAR_COUNT + 1: i_15m + 1]
    if len(bars) < SPIKE_BAR_COUNT:
        return None

    # Check direction consistency
    closes = bars["close"].values
    opens = bars["open"].values
    highs = bars["high"].values
    lows = bars["low"].values
    vols = bars["volume"].values

    all_bullish = all(closes[j] > opens[j] for j in range(SPIKE_BAR_COUNT))
    all_bearish = all(closes[j] < opens[j] for j in range(SPIKE_BAR_COUNT))

    if SPIKE_DIRECTION_CONSISTENT and not (all_bullish or all_bearish):
        return None

    direction = "BUY" if all_bullish else "SELL"

    # Check volume explosion — last bar volume >= threshold × avg of NORMAL bars
    # (compare against bars BEFORE the spike window, not the spike bars themselves)
    normal_vol_end = i_15m - SPIKE_BAR_COUNT
    if normal_vol_end >= 10:
        normal_avg_vol = float(df_15m["volume"].iloc[max(0, normal_vol_end - 10):normal_vol_end].mean())
    else:
        normal_avg_vol = float(vols[:-1].mean()) if len(vols) > 1 else float(vols[0])
    last_vol = float(vols[-1])
    vol_mult = last_vol / normal_avg_vol if normal_avg_vol > 0 else 0
    if vol_mult < SPIKE_VOL_MULT:
        return None

    # Check range — each bar must move meaningfully
    last_close = float(closes[-1])
    ranges = [float(highs[j] - lows[j]) for j in range(SPIKE_BAR_COUNT)]
    range_pcts = [r / last_close * 100 for r in ranges if last_close > 0]
    if not range_pcts or min(range_pcts) < SPIKE_MIN_RANGE_PCT * 0.5:
        return None

    # Acceleration check: last bar range >= average of prior bars
    avg_prior_range = sum(ranges[:-1]) / max(1, len(ranges) - 1)
    if ranges[-1] < avg_prior_range * 0.8:
        return None

    # Volume trend: increasing (each bar >= prior)
    vol_increasing = all(vols[j] >= vols[j - 1] * 0.8 for j in range(1, SPIKE_BAR_COUNT))
    vol_bonus = 5 if vol_increasing else 0

    body_pct = abs(closes[-1] - opens[-1]) / max(ranges[-1], 0.001) * 100
    score = min(100.0, body_pct * 0.3 + vol_mult * 10 + 25 + vol_bonus)

    # === STRUCTURAL STOP — swing low/high of spike, not fixed -7% ===
    # For BUY: stop below the lowest low of the 3 spike bars (the bottom)
    # For SELL: stop above the highest high of the 3 spike bars
    if direction == "BUY":
        spike_low = float(min(lows))
        structural_stop = spike_low * 0.98  # 2% below spike bottom
    else:
        spike_high = float(max(highs))
        structural_stop = spike_high * 1.02  # 2% above spike top

    logger.info(
        f"🚀 MOMENTUM SPIKE {direction}: 3-bar accel "
        f"vol={vol_mult:.1f}x body={body_pct:.0f}% "
        f"range={range_pcts[-1]:.1f}% score={score:.0f}")

    return {
        "direction": direction,
        "entry_price": last_close,
        "setup_score": score,
        "strike_kind": "ATM",
        "score_details": f"SPIKE-{direction} vol={vol_mult:.1f}x body={body_pct:.0f}%",
        "strategy": "MOMENTUM_SPIKE",
        "structural_stop": structural_stop,
    }


def detect_vwap_reclaim(
    df_15m: pd.DataFrame,
    i_15m: int,
) -> dict | None:
    """VWAP reclaim detector.

    Price was below VWAP, then reclaims VWAP with strong volume.
    This signals institutional re-entry — Tiger catches the turn.

    Conversely, price was above VWAP, breaks below, then fails
    to reclaim → bearish. Tiger catches the breakdown.
    """
    if i_15m < 10:
        return None

    try:
        from subbrains.trend_follow import calculate_vwap
        vwap_series = calculate_vwap(df_15m.iloc[:i_15m + 1])
        vwap_val = float(vwap_series.iloc[-1])
        if vwap_val <= 0:
            return None
    except Exception:
        return None

    row = df_15m.iloc[i_15m]
    prev_row = df_15m.iloc[i_15m - 1]
    o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
    prev_c = float(prev_row["close"])

    rng = h - l
    if rng <= 0:
        return None

    body = abs(c - o)
    body_pct = (body / rng) * 100

    vol = float(row.get("volume", 0) or 0)
    avg_vol = float(df_15m["volume"].iloc[max(0, i_15m - 10):i_15m].mean()) if i_15m > 10 else vol
    vol_mult = vol / avg_vol if avg_vol > 0 else 0

    # BULLISH VWAP reclaim: prev close below VWAP, current close above
    if prev_c < vwap_val and c > vwap_val:
        dist_pct = abs(c - vwap_val) / vwap_val * 100
        if dist_pct <= VWAP_RECLAIM_MAX_DIST_PCT and vol_mult >= VWAP_RECLAIM_MIN_VOL_MULT:
            score = min(100.0, body_pct * 0.3 + vol_mult * 12 + 25)
            # Structural stop: below the bar's low (the dip before reclaim)
            structural_stop = l * 0.98
            logger.info(
                f"🚀 VWAP RECLAIM UP: close ₹{c:.2f} > VWAP ₹{vwap_val:.2f} "
                f"vol={vol_mult:.1f}x score={score:.0f}")
            return {
                "direction": "BUY",
                "entry_price": c,
                "setup_score": score,
                "strike_kind": "ATM",
                "score_details": f"VWAP-RECLAIM vol={vol_mult:.1f}x body={body_pct:.0f}%",
                "strategy": "VWAP_RECLAIM",
                "structural_stop": structural_stop,
            }

    # BEARISH VWAP rejection: prev close above VWAP, current close below
    if prev_c > vwap_val and c < vwap_val:
        dist_pct = abs(c - vwap_val) / vwap_val * 100
        if dist_pct <= VWAP_RECLAIM_MAX_DIST_PCT and vol_mult >= VWAP_RECLAIM_MIN_VOL_MULT:
            score = min(100.0, body_pct * 0.3 + vol_mult * 12 + 25)
            structural_stop = h * 1.02
            logger.info(
                f"🚀 VWAP REJECT DOWN: close ₹{c:.2f} < VWAP ₹{vwap_val:.2f} "
                f"vol={vol_mult:.1f}x score={score:.0f}")
            return {
                "direction": "SELL",
                "entry_price": c,
                "setup_score": score,
                "strike_kind": "ATM",
                "score_details": f"VWAP-REJECT vol={vol_mult:.1f}x body={body_pct:.0f}%",
                "strategy": "VWAP_RECLAIM",
                "structural_stop": structural_stop,
            }

    return None


def options_math_gate(
    df_15m: pd.DataFrame,
    i_15m: int,
    direction: str,
    strike: float,
    underlying_price: float,
    vix_val: float,
    symbol: str,
) -> tuple[bool, float, str]:
    """Options mathematics gate — Tiger NEVER buys overpriced premium.

    Checks:
      1. IV percentile — if > 65th percentile, BLOCK (premium too expensive)
         If < 35th percentile, give +5 score bonus (discount premium)
      2. Delta estimate — must be within 0.40-0.75 band (proper options buying)
      3. Returns (allowed, bonus, reason)

    This is Tiger's premium protection — the reason Tiger never gets
    tricked by inflated premiums.
    """
    from backtest.run_tiger_brain_backtest import compute_iv
    from broker.option_selector import estimate_delta
    from backtest.intraday_backtest import realized_vol_simple

    is_call = direction == "BUY"
    iv = compute_iv(df_15m.iloc[:i_15m + 1], vix_val, symbol, is_call, strike, underlying_price)

    # IV percentile approximation using realized vol history
    # Compare current IV against rolling 20-bar realized vol
    try:
        from backtest.run_tiger_brain_backtest import realized_vol_simple
        rv_history = []
        for lookback_i in range(max(20, i_15m - 20), i_15m + 1):
            if lookback_i >= 20:
                rv = realized_vol_simple(df_15m.iloc[:lookback_i + 1])
                rv_history.append(rv)
        if len(rv_history) >= 5:
            iv_percentile = sum(1 for rv in rv_history if rv < iv) / len(rv_history) * 100
        else:
            iv_percentile = 50.0
    except Exception:
        iv_percentile = 50.0

    # Gate 1: IV percentile check
    if iv_percentile > IV_EXPENSIVE_PERCENTILE:
        return False, 0.0, f"IV {iv_percentile:.0f}pct > {IV_EXPENSIVE_PERCENTILE:.0f} (overpriced)"

    # Gate 2: Delta band check
    contract = {"strike": strike, "delta": None}
    delta = estimate_delta(contract, underlying_price, "CE" if is_call else "PE")
    delta_abs = abs(delta)

    min_delta = DELTA_MIN_BUY if is_call else DELTA_MIN_SELL
    max_delta = DELTA_MAX_BUY if is_call else DELTA_MAX_SELL
    if delta_abs < min_delta:
        return False, 0.0, f"delta {delta:.2f} < {min_delta} (too OTM)"
    if delta_abs > max_delta:
        return False, 0.0, f"delta {delta:.2f} > {max_delta} (too ITM, expensive)"

    # Bonus for discount IV
    bonus = IV_DISCOUNT_BONUS if iv_percentile < 35.0 else 0.0
    return True, bonus, f"IV {iv_percentile:.0f}pct delta {delta:.2f} {'DISCOUNT' if bonus else 'fair'}"


def hunt_momentum(
    df_15m: pd.DataFrame,
    i_15m: int,
    df_1m: pd.DataFrame | None,
    seg: str,
    symbol: str,
    broker,
    pcr_cache: dict,
    vix_val: float,
    now: datetime | None = None,
) -> dict | None:
    """🐅 TIGER MOMENTUM HUNTER — full market scan for momentum.

    Runs ALL three detectors in parallel:
      1. ORB breakout (morning rocket catcher)
      2. Momentum spike (3-bar acceleration)
      3. VWAP reclaim (institutional re-entry)

    Then applies the OPTIONS MATH GATE — Tiger never enters without
    checking premium fairness.

    Returns the HIGHEST scoring signal that passes all gates, or None.
    """
    if now is None:
        now = datetime.now()
    if df_15m is None or len(df_15m) < 40:
        return None

    signals = []

    # Layer 1: ORB Breakout
    try:
        orb = detect_orb_breakout(df_15m, i_15m, df_1m, now)
        if orb is not None:
            signals.append(orb)
    except Exception as exc:
        logger.debug("ORB hunt fail %s: %s", symbol, exc)

    # Layer 2: Momentum Spike
    try:
        spike = detect_momentum_spike(df_15m, i_15m)
        if spike is not None:
            signals.append(spike)
    except Exception as exc:
        logger.debug("Spike hunt fail %s: %s", symbol, exc)

    # Layer 3: VWAP Reclaim
    try:
        vwap_sig = detect_vwap_reclaim(df_15m, i_15m)
        if vwap_sig is not None:
            signals.append(vwap_sig)
    except Exception as exc:
        logger.debug("VWAP hunt fail %s: %s", symbol, exc)

    if not signals:
        return None

    # Pick the highest scoring signal
    signals.sort(key=lambda s: s.get("setup_score", 0), reverse=True)
    best = signals[0]

    direction = best.get("direction", "BUY")
    underlying_price = best.get("entry_price", 0.0)
    strike = round(underlying_price)  # ATM

    # === OPTIONS MATH GATE — mandatory premium check ===
    allowed, bonus, math_detail = options_math_gate(
        df_15m, i_15m, direction, strike, underlying_price, vix_val, symbol)

    if not allowed:
        logger.info(
            f"🚫 MATH GATE BLOCK: {symbol} {direction} — {math_detail}")
        return None

    best["setup_score"] = min(100.0, best["setup_score"] + bonus)
    best["options_math"] = math_detail
    best["symbol"] = symbol
    best["is_scalper"] = False  # momentum hunter is NOT scalper
    best["is_momentum_hunter"] = True
    best["iv_bonus"] = bonus

    logger.info(
        f"🐅 MOMENTUM HUNTER SIGNAL: {symbol} {direction} "
        f"strat={best.get('strategy', '?')} score={best['setup_score']:.0f} "
        f"[{math_detail}]")

    return best
