"""
Tiger Brain V6.1 — BRAIN 2: Setup Trigger & Entry Engine (SMC)
================================================================
Dusra brain. Brain 1 ne momentum confirm kiya; ab ye brain SMART MONEY
CONCEPTS (SMC) se asli setup trigger banata hai:

  1. ORDER BLOCK — structure break se theek pehle ki last opposite
     candle. Institutional footprint jahan se displacement nikla.
  2. LIQUIDITY SWEEP — swing high/low ke paar wick ghuskar wapas
     andar close hona (stop-hunt / false breakout rejection).
  3. RS SCORE (0-100) — Brain 1 ke RS divergence ko setup confidence
     mein convert karna.

Output: ek setup dict — direction (BUY=call side / SELL=put side), entry,
stop-loss level, aur confidence score. Ye Brain 3 (option selector) ko
jaata hai.

Note: is repo ke pichhle requirements (SMC Order Blocks, Liquidity Sweeps,
RS scores) isi file mein live hain.
"""

from __future__ import annotations

import logging

try:
    from config.thresholds import BRAIN1, BRAIN2
except ImportError:
    raise ImportError("Repo ROOT se chalao, 'pipeline/' ke andar se nahi.")

logger = logging.getLogger("tiger_brain.smart_money_scanner")


def _atr(df, period: int = 14) -> float:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = (
        (high - low)
        .combine((high - prev_close).abs(), max)
        .combine((low - prev_close).abs(), max)
    )
    return float(tr.tail(period).mean())


def find_order_blocks(df) -> dict:
    """
    SMC Order Block detection.

    Bullish OB: displacement-up se theek pehle ki last bearish candle,
    jahan displacement ATR-multiple se badi ho aur market structure
    (recent swing high) break kare.
    Bearish OB: iska mirror image.

    Returns dict with bullish/bearish OB zones (price ranges) or None.
    """
    if len(df) < BRAIN2["ORDER_BLOCK_LOOKBACK_BARS"] + 5:
        return {"bullish": None, "bearish": None, "note": "kaafi bars nahi hain"}

    atr = _atr(df)
    if atr <= 0:
        return {"bullish": None, "bearish": None, "note": "ATR 0"}

    lookback = BRAIN2["ORDER_BLOCK_LOOKBACK_BARS"]
    recent = df.tail(lookback).reset_index(drop=True)
    min_disp = atr * BRAIN2["ORDER_BLOCK_MIN_DISPLACEMENT_ATR"]

    bullish_ob = None
    bearish_ob = None

    for i in range(2, len(recent)):
        # Displacement candle(s): current bar ka body
        bar = recent.iloc[i]
        body = abs(float(bar["close"]) - float(bar["open"]))
        if body < min_disp:
            continue

        prev = recent.iloc[i - 1]

        if bar["close"] > bar["open"]:  # displacement UP
            # structure break check: pichhle kuch bars ka swing high todna
            swing_high = float(recent.iloc[: i]["high"].max())
            if bar["close"] > swing_high and prev["close"] < prev["open"]:
                bullish_ob = {
                    "index": i - 1,
                    "top": float(prev["high"]),
                    "bottom": float(prev["low"]),
                }

        elif bar["close"] < bar["open"]:  # displacement DOWN
            swing_low = float(recent.iloc[: i]["low"].min())
            if bar["close"] < swing_low and prev["close"] > prev["open"]:
                bearish_ob = {
                    "index": i - 1,
                    "top": float(prev["high"]),
                    "bottom": float(prev["low"]),
                }

    return {"bullish": bullish_ob, "bearish": bearish_ob, "note": None}


def find_liquidity_sweep(df) -> dict:
    """
    SMC Liquidity Sweep detection.

    Bullish sweep: kisi recent swing LOW ke neeche wick ghusi, par close
    us level ke upar wapas aa gayi (sellers ke stops hunt karke rejection).
    Bearish sweep: swing HIGH ke upar wick, close neeche wapas.

    Returns dict with sweep direction, swept level, wick extreme — or None.
    """
    if len(df) < BRAIN2["SWING_LOOKBACK_BARS"] + 2:
        return {"sweep": None, "note": "kaafi bars nahi hain"}

    atr = _atr(df)
    if atr <= 0:
        return {"sweep": None, "note": "ATR 0"}

    lookback = BRAIN2["SWING_LOOKBACK_BARS"]
    window = df.tail(lookback + 1).reset_index(drop=True)
    last = window.iloc[-1]
    prior = window.iloc[:-1]  # last bar se pehle ke swings

    min_pierce = atr * BRAIN2["LIQUIDITY_SWEEP_MIN_PIERCE_ATR"]
    result = {"sweep": None, "note": None}

    # --- Bullish sweep: low wick neeche ghusi, close upar ---
    swing_low = float(prior["low"].min())
    if (
        float(last["low"]) < swing_low - min_pierce
        and float(last["close"]) > swing_low
    ):
        result["sweep"] = {
            "direction": "bullish",
            "swept_level": swing_low,
            "wick_extreme": float(last["low"]),
            "pierce_atr": round((swing_low - float(last["low"])) / atr, 2),
        }

    # --- Bearish sweep: high wick upar ghusi, close neeche ---
    swing_high = float(prior["high"].max())
    if (
        float(last["high"]) > swing_high + min_pierce
        and float(last["close"]) < swing_high
    ):
        result["sweep"] = {
            "direction": "bearish",
            "swept_level": swing_high,
            "wick_extreme": float(last["high"]),
            "pierce_atr": round((float(last["high"]) - swing_high) / atr, 2),
        }

    return result


def compute_rs_score(rs_divergence_pct) -> float:
    """
    RS divergence ko 0-100 score mein map karna.
    ±1pp (BRAIN1 threshold) pe ~50; har additional pp 10 points.
    """
    if rs_divergence_pct is None:
        return 50.0  # neutral — benchmark data nahi tha
    # 50 + 10*divergence, clamped 0-100
    score = 50 + 10 * rs_divergence_pct
    return float(max(0.0, min(100.0, score)))


def generate_setup(brain1_result: dict, df) -> dict:
    """
    Brain 2 ka main entry point. Brain 1 ke output + OHLCV se SMC setup
    banata hai (ya reject karta hai).

    Args:
        brain1_result: pipeline.brain1_scanner.scan() ka output
        df: symbol OHLCV DataFrame

    Returns:
        dict:
            'setup_found': bool
            'direction': 'BUY' | 'SELL' | None   (BUY = call, SELL = put)
            'entry_price': float | None
            'stop_loss': float | None   (underlying pe, OB/sweep-based)
            'setup_score': float (0-100)
            'order_block': detected OB details
            'liquidity_sweep': detected sweep details
            'rs_score': float
            'brain2_notes': list
    """
    notes = []

    if not brain1_result.get("passed_brain1", False):
        return {
            "setup_found": False, "direction": None, "entry_price": None,
            "stop_loss": None, "setup_score": 0, "order_block": None,
            "liquidity_sweep": None, "rs_score": 0, "brain2_notes":
            ["Brain 1 pass nahi hua — Brain 2 run hi nahi hoga"],
        }

    obs = find_order_blocks(df)
    sweep = find_liquidity_sweep(df)

    rs_pct = None
    if brain1_result.get("rs_divergence"):
        rs_pct = brain1_result["rs_divergence"].get("divergence_pct")
    rs_score = compute_rs_score(rs_pct)

    # --- Direction resolve karna: OB + sweep + RS ek hon mein ---
    votes = []

    if obs["bullish"]:
        votes.append("BUY")
    if obs["bearish"]:
        votes.append("SELL")
    if sweep.get("sweep"):
        votes.append("BUY" if sweep["sweep"]["direction"] == "bullish" else "SELL")
    if rs_score >= BRAIN2["RS_SCORE_SIGNIFICANT_MIN"]:
        votes.append("BUY")
    elif rs_score <= 100 - BRAIN2["RS_SCORE_SIGNIFICANT_MIN"]:
        votes.append("SELL")

    if not votes:
        return {
            "setup_found": False, "direction": None, "entry_price": None,
            "stop_loss": None, "setup_score": 0, "order_block": obs,
            "liquidity_sweep": sweep, "rs_score": rs_score,
            "brain2_notes": ["koi SMC structure nahi mila (OB/sweep/RS) — koi setup nahi"],
        }

    buy_votes = votes.count("BUY")
    sell_votes = votes.count("SELL")

    if buy_votes == sell_votes:
        return {
            "setup_found": False, "direction": None, "entry_price": None,
            "stop_loss": None, "setup_score": 0, "order_block": obs,
            "liquidity_sweep": sweep, "rs_score": rs_score,
            "brain2_notes": [
                f"conflicting signals ({buy_votes} BUY vs {sell_votes} SELL votes) — koi setup nahi"
            ],
        }

    direction = "BUY" if buy_votes > sell_votes else "SELL"

    # --- Entry & stop-loss ---
    last_close = float(df["close"].iloc[-1])
    entry_price = last_close
    stop_loss = None

    if direction == "BUY":
        ob = obs["bullish"]
        swp = (sweep.get("sweep") or {}).get("direction") == "bullish" and sweep["sweep"] or None
        # Stop: OB bottom ya sweep wick extreme, jo zyada conservative ho
        candidates = []
        if ob:
            candidates.append(ob["bottom"])
        if swp:
            candidates.append(swp["wick_extreme"])
        if candidates:
            stop_loss = min(candidates)
            notes.append(f"Bullish setup — stop OB-bottom/sweep-wick se: {stop_loss}")
    else:
        ob = obs["bearish"]
        swp = (sweep.get("sweep") or {}).get("direction") == "bearish" and sweep["sweep"] or None
        candidates = []
        if ob:
            candidates.append(ob["top"])
        if swp:
            candidates.append(swp["wick_extreme"])
        if candidates:
            stop_loss = max(candidates)
            notes.append(f"Bearish setup — stop OB-top/sweep-wick se: {stop_loss}")

    if stop_loss is not None and direction == "BUY" and stop_loss >= entry_price:
        stop_loss = None
        notes.append("stop-loss entry ke upar aa gaya (structure invalid) — stop hata diya, setup reject")
    if stop_loss is not None and direction == "SELL" and stop_loss <= entry_price:
        stop_loss = None
        notes.append("stop-loss entry ke neeche aa gaya (structure invalid) — stop hata diya, setup reject")
    if stop_loss is None:
        return {
            "setup_found": False, "direction": None, "entry_price": None,
            "stop_loss": None, "setup_score": 0, "order_block": obs,
            "liquidity_sweep": sweep, "rs_score": rs_score,
            "brain2_notes": notes + ["valid stop-loss nahi bana — setup reject"],
        }

    # --- Setup score (0-100): OB + sweep + RS weighted ---
    weights = BRAIN2["SETUP_SCORE_WEIGHTS"]

    ob_present = 1.0 if (direction == "BUY" and obs["bullish"]) or (
        direction == "SELL" and obs["bearish"]
    ) else 0.0
    sweep_aligned = 0.0
    if sweep.get("sweep"):
        aligned = (
            (direction == "BUY" and sweep["sweep"]["direction"] == "bullish")
            or (direction == "SELL" and sweep["sweep"]["direction"] == "bearish")
        )
        # pierce depth se thoda bonus (deeper sweep = stronger signal)
        sweep_aligned = min(1.0, (sweep["sweep"]["pierce_atr"] or 0.5) / 1.0) if aligned else 0.0
    # RS score ko direction ke hisaab se: BUY ke liye high RS, SELL ke liye low
    rs_component = rs_score / 100.0 if direction == "BUY" else (100 - rs_score) / 100.0

    setup_score = 100 * (
        weights["order_block"] * ob_present
        + weights["liquidity_sweep"] * sweep_aligned
        + weights["rs_score"] * rs_component
    )

    setup_found = setup_score >= BRAIN2["MIN_SETUP_SCORE"]
    if not setup_found:
        notes.append(f"setup score {setup_score:.1f} < minimum {BRAIN2['MIN_SETUP_SCORE']} — reject")
    else:
        notes.append(
            f"{direction} setup — score {setup_score:.1f} "
            f"(OB: {ob_present}, sweep: {sweep_aligned:.2f}, RS: {rs_component:.2f})"
        )

    return {
        "setup_found": setup_found,
        "direction": direction if setup_found else None,
        "entry_price": entry_price if setup_found else None,
        "stop_loss": stop_loss if setup_found else None,
        "setup_score": round(setup_score, 1),
        "order_block": obs,
        "liquidity_sweep": sweep,
        "rs_score": round(rs_score, 1),
        "brain2_notes": notes,
    }


# ============================================================
# QUICK MANUAL TEST — repo ROOT se: python3 -m pipeline.smart_money_scanner
# ============================================================
if __name__ == "__main__":
    import numpy as np
    import pandas as pd

    np.random.seed(11)
    n = 80
    dates = pd.date_range("2025-01-01", periods=n, freq="D")

    # Uptrend with a sweep-and-reclaim at the end
    closes = 100 + np.linspace(0, 14, n)
    d = pd.DataFrame(index=dates)
    d["close"] = closes
    d["open"] = d["close"].shift(1).fillna(100)
    d["high"] = d[["open", "close"]].max(axis=1) + 0.2
    d["low"] = d[["open", "close"]].min(axis=1) - 0.2
    vols = np.full(n, 200000)
    vols[-1] = 700000
    d["volume"] = vols

    # Last bar: bullish sweep (deep wick below recent swing low, close above)
    swing_low = float(d["low"].iloc[-15:-1].min())
    d.iloc[-1, d.columns.get_loc("low")] = swing_low - 4.0
    d.iloc[-1, d.columns.get_loc("open")] = swing_low - 1.0
    d.iloc[-1, d.columns.get_loc("close")] = swing_low + 3.0
    d.iloc[-1, d.columns.get_loc("high")] = swing_low + 3.4

    b1 = {
        "passed_brain1": True,
        "rs_divergence": {"divergence_pct": 2.5},
    }
    result = generate_setup(b1, d)
    print("=== Brain 2: SMC setup test ===")
    print(f"setup_found: {result['setup_found']}")
    print(f"direction: {result['direction']}")
    print(f"entry: {result['entry_price']}, stop: {result['stop_loss']}")
    print(f"setup_score: {result['setup_score']}, rs_score: {result['rs_score']}")
    for note in result["brain2_notes"]:
        print(f"  - {note}")
