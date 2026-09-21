"""TIGER SNIPER ADVANCED V2 — MCX Commodity Scanner (the Brain).

Pure SMC + Supply/Demand zone detection across ALL MCX commodities. Input is
5m candles for each commodity. For every symbol the scanner detects:

  a) Break of Structure (BOS)          — trend continuation / reversal confirm
  b) Liquidity Sweep (buy/sell-side)   — institutional stop-hunt + snap-back
  c) Order Block (last opposite candle before impulse)
  d) Fair Value Gap (FVG)              — 3-bar institutional imbalance
  e) zone_strength (0-100)             — confluence score across the four above

Output: the single highest-quality zone with zone_strength > 80, or NO_TRADE.
A zone carries everything the sniper entry/exit engine needs: direction,
order-block edges (for the retest + SL), FVG size, ATR% volatility, and the
raw confluence components so the ML feature store can ingest them.

Design notes:
  - No fixed NIFTY / SENSEX / stock symbols — MCX commodities only.
  - All detectors are pure price-action (no VWAP / EMA / Black-Scholes).
  - ATR is the True Range average; used both for displacement thresholds and
    for the ATR * 2.5 trailing exit in tiger_live.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
# MCX universe — the ONLY symbols the sniper trades.
# yfinance proxies (US futures) are used for historical/backtest data;
# the live path maps these to MCX option contracts via data/loader.
# ─────────────────────────────────────────────────────────────────
MCX_SYMBOLS: dict[str, str] = {
    "GOLDM": "GC=F",
    "SILVERM": "SI=F",
    "CRUDEOIL": "CL=F",
    "NATURALGAS": "NG=F",
}

# Confluence weights — sum to 100.
_WEIGHTS = {"bos": 25, "sweep": 25, "ob": 25, "fvg": 25}
MIN_ZONE_STRENGTH = 80.0


# ─────────────────────────────────────────────────────────────────
# ATR
# ─────────────────────────────────────────────────────────────────
def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Average True Range over the last `period` bars (Wilder-style mean)."""
    if df is None or df.empty or len(df) < 2:
        return 0.0
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    tail = tr.tail(period).dropna()
    if tail.empty:
        return 0.0
    return float(tail.mean())


def calculate_atr_pct(df: pd.DataFrame, period: int = 14) -> float:
    """ATR as a percentage of the latest close — commodity_volatility feature."""
    atr = calculate_atr(df, period)
    if df is None or df.empty:
        return 0.0
    price = float(df["close"].iloc[-1])
    if atr <= 0 or price <= 0:
        return 0.0
    return round((atr / price) * 100.0, 4)


# ─────────────────────────────────────────────────────────────────
# SMC detectors — all operate on 5m candles, index i = last bar.
# ─────────────────────────────────────────────────────────────────
def detect_bos(df: pd.DataFrame, i: int, lookback: int = 20) -> Optional[dict]:
    """Break of Structure up to bar i.

    Bullish BOS: close breaks above the highest high of the prior `lookback`
    bars (structure shifts up). Bearish BOS: close breaks below the prior low.
    Returns {"direction": "bullish"|"bearish", "level": float, "bar": i} or None.
    """
    if df is None or df.empty or i < lookback:
        return None
    window = df.iloc[max(0, i - lookback):i]
    if len(window) < 5:
        return None
    cur = df.iloc[i]
    cur_close = float(cur["close"])
    swing_high = float(window["high"].max())
    swing_low = float(window["low"].min())
    atr = calculate_atr(df.iloc[: i + 1])
    min_break = max(atr * 0.25, 0.0)  # tiny ATR buffer to reject noise

    if cur_close > swing_high + min_break:
        return {"direction": "bullish", "level": swing_high, "bar": i}
    if cur_close < swing_low - min_break:
        return {"direction": "bearish", "level": swing_low, "bar": i}
    return None


def detect_liquidity_sweep(df: pd.DataFrame, i: int,
                           lookback: int = 20) -> Optional[dict]:
    """Liquidity sweep (stop-hunt + snap-back) at bar i.

    Bullish sweep: low pierces below the prior swing low but close snaps back
    above it (sell-side liquidity grabbed). Bearish: high pierces above prior
    swing high, close snaps back below (buy-side grabbed).
    """
    if df is None or df.empty or i < lookback + 1:
        return None
    prior = df.iloc[max(0, i - lookback):i]
    if len(prior) < 5:
        return None
    cur = df.iloc[i]
    cur_high = float(cur["high"])
    cur_low = float(cur["low"])
    cur_close = float(cur["close"])
    swing_low = float(prior["low"].min())
    swing_high = float(prior["high"].max())
    atr = calculate_atr(df.iloc[: i + 1])
    min_pierce = atr * 0.15 if atr > 0 else 0.0

    if cur_low < swing_low - min_pierce and cur_close > swing_low:
        return {"direction": "bullish", "swept_level": swing_low,
                "wick_extreme": cur_low, "bar": i}
    if cur_high > swing_high + min_pierce and cur_close < swing_high:
        return {"direction": "bearish", "swept_level": swing_high,
                "wick_extreme": cur_high, "bar": i}
    return None


def detect_order_block(df: pd.DataFrame, i: int,
                       lookback: int = 20) -> Optional[dict]:
    """Order Block — last opposite candle before an impulsive displacement.

    Bullish OB: the last bearish candle immediately before a bullish
    displacement that breaks structure. Bearish OB: mirror. The OB's high/low
    define the retest zone and the SL edge.
    """
    if df is None or df.empty or i < lookback:
        return None
    window = df.iloc[max(0, i - lookback):i + 1].reset_index(drop=True)
    if len(window) < 6:
        return None
    atr = calculate_atr(df.iloc[: i + 1])
    if atr <= 0:
        return None
    min_disp = atr * 0.8

    # scan the window for the displacement candle, then grab the prior opposite
    for k in range(2, len(window)):
        bar = window.iloc[k]
        body = abs(float(bar["close"]) - float(bar["open"]))
        if body < min_disp:
            continue
        prev = window.iloc[k - 1]
        if bar["close"] > bar["open"]:  # displacement up → bullish OB
            swing_high = float(window.iloc[:k]["high"].max())
            if float(bar["close"]) > swing_high and prev["close"] < prev["open"]:
                return {"direction": "bullish",
                        "top": float(prev["high"]),
                        "bottom": float(prev["low"]),
                        "bar": i - (len(window) - 1 - k) + 1}
        else:  # displacement down → bearish OB
            swing_low = float(window.iloc[:k]["low"].min())
            if float(bar["close"]) < swing_low and prev["close"] > prev["open"]:
                return {"direction": "bearish",
                        "top": float(prev["high"]),
                        "bottom": float(prev["low"]),
                        "bar": i - (len(window) - 1 - k) + 1}
    return None


def detect_fvg(df: pd.DataFrame, i: int, lookback: int = 20) -> Optional[dict]:
    """Latest unfilled Fair Value Gap near bar i.

    Bullish FVG: bar[k-2].high < bar[k].low (gap up, unfilled). Bearish: mirror.
    Returns {"direction", "top", "bottom", "size", "bar"} or None. `size` is
    the gap width as a fraction of ATR (scale-invariant).
    """
    if df is None or df.empty or i < 2:
        return None
    atr = calculate_atr(df.iloc[: i + 1])
    start = max(2, i - lookback + 1)
    # walk backwards from i to find the most recent UNFILLED gap
    for idx in range(i, start - 1, -1):
        h1 = float(df.iloc[idx - 2]["high"])
        l1 = float(df.iloc[idx - 2]["low"])
        h3 = float(df.iloc[idx]["high"])
        l3 = float(df.iloc[idx]["low"])
        if l3 > h1:  # bullish gap
            filled = any(float(df.iloc[k]["low"]) <= h1
                         for k in range(idx + 1, i + 1))
            if not filled:
                gap = l3 - h1
                return {"direction": "bullish", "top": l3, "bottom": h1,
                        "size": round(gap / atr, 4) if atr > 0 else 0.0,
                        "bar": idx}
        elif h3 < l1:  # bearish gap
            filled = any(float(df.iloc[k]["high"]) >= l1
                         for k in range(idx + 1, i + 1))
            if not filled:
                gap = l1 - h3
                return {"direction": "bearish", "top": l1, "bottom": h3,
                        "size": round(gap / atr, 4) if atr > 0 else 0.0,
                        "bar": idx}
    return None


# ─────────────────────────────────────────────────────────────────
# Zone confluence scoring + scan
# ─────────────────────────────────────────────────────────────────
@dataclass
class SniperZone:
    """A scored SMC confluence zone for one commodity."""
    symbol: str
    direction: str                      # "BUY" | "SELL"
    option_type: str                    # "CE" | "PE"
    zone_type: str                      # "demand" | "supply"
    zone_strength: float                # 0-100
    order_block: dict
    fvg: Optional[dict] = None
    fvg_size: float = 0.0
    commodity_volatility: float = 0.0   # ATR %
    bos: Optional[dict] = None
    liquidity_sweep: Optional[dict] = None
    entry_ts: Optional[str] = None
    setup_score: float = 0.0
    is_sniper: bool = True
    extra: dict = field(default_factory=dict)

    def to_signal(self) -> dict:
        """Serialise to a tiger_live-compatible signal dict."""
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "option_type": self.option_type,
            "zone_type": self.zone_type,
            "zone_strength": self.zone_strength,
            "sniper_zone_strength": self.zone_strength,   # raw 0-100 for ML
            "order_block": self.order_block,
            "fvg": self.fvg,
            "fvg_size": self.fvg_size,
            "commodity_volatility": self.commodity_volatility,
            "bos": self.bos,
            "liquidity_sweep": self.liquidity_sweep,
            "setup_score": self.setup_score,
            "is_sniper": True,
            "is_scalper": False,
            "is_momentum_hunter": False,
            "entry_ts": self.entry_ts,
            "brain_alignment": 7,   # sniper = full conviction by construction
        }


def _score_zone(bos, sweep, ob, fvg) -> tuple[float, str, str]:
    """Confluence score (0-100) + zone_type + dominant direction.

    Direction is derived from whichever SMC components agree. If components
    conflict (e.g. bullish BOS but bearish sweep), the zone is weakened.
    """
    score = 0.0
    directions = []
    if bos:
        score += _WEIGHTS["bos"]
        directions.append(bos["direction"])
    if sweep:
        score += _WEIGHTS["sweep"]
        directions.append(sweep["direction"])
    if ob:
        score += _WEIGHTS["ob"]
        directions.append(ob["direction"])
    if fvg:
        score += _WEIGHTS["fvg"]
        directions.append(fvg["direction"])

    if not directions:
        return 0.0, "neutral", "CE", "BUY"

    # Majority direction
    bull = sum(1 for d in directions if d == "bullish")
    bear = sum(1 for d in directions if d == "bearish")
    if bull > bear:
        direction, zone_type, option_type = "BUY", "demand", "CE"
    elif bear > bull:
        direction, zone_type, option_type = "SELL", "supply", "PE"
    else:
        # tie → no clean direction, penalise
        score -= 10
        direction, zone_type, option_type = "BUY", "demand", "CE"

    # Confluence bonus: 3+ agreeing components → +5 (max 100)
    agreeing = max(bull, bear)
    if agreeing >= 3:
        score = min(score + 5, 100.0)
    # Conflict penalty: opposing components present
    if bull > 0 and bear > 0:
        score -= min(bull, bear) * 5
    return max(0.0, min(score, 100.0)), zone_type, option_type, direction


def scan_mcx(data_map_5m: dict[str, pd.DataFrame],
             now_ts: Optional[str] = None,
             min_components: int = 3,
             allowed: Optional[set] = None,
             market: str = "MCX") -> Optional[SniperZone]:
    """Scan a symbol universe, return the single best zone (>80) or None.

    Generalized for BOTH MCX commodities and NSE index/stock options — the
    pure-SMC detectors are price-action only (no volume), so the same engine
    works on any universe.

    Args:
        data_map_5m: {symbol: DataFrame of 5m candles} for the universe.
        now_ts: ISO timestamp to stamp on the signal (defaults to now).
        min_components: minimum agreeing SMC components for a rocket setup.
            Default 3 (BOS+sweep+OB+FVG — true confluence, not noise).
            NSE path passes 2 (NSE_MIN_CONFLUENCE_COMPONENTS).
        allowed: optional set of symbols to scan. If None, defaults to
            MCX_SYMBOLS (back-compat). NSE path passes its own universe set.
        market: "MCX" → count-based confluence gate (3+ of BOS/sweep/OB/FVG).
            "NSE" → OB-ANCHORED gate: Order Block is MANDATORY + (FVG or
            liquidity sweep). This is a true institutional supply/demand zone
            (OB = last opposite candle before impulse = the institution's
            reversion level; FVG/sweep = the imbalance that confirms it).

    Returns:
        SniperZone (best zone_strength > 80) or None (NO_TRADE).
    """
    from datetime import datetime
    from config.thresholds import SNIPER
    if now_ts is None:
        now_ts = datetime.now().isoformat()
    if allowed is None:
        allowed = set(MCX_SYMBOLS.keys())
    if market == "MCX":
        min_components = SNIPER.get("MIN_CONFLUENCE_COMPONENTS", min_components)
    else:
        min_components = SNIPER.get("NSE_MIN_CONFLUENCE_COMPONENTS", 2)

    best: Optional[SniperZone] = None
    for symbol, df in data_map_5m.items():
        if symbol not in allowed:
            continue
        if df is None or df.empty or len(df) < 25:
            logger.debug("mcx_scanner: %s skipped (insufficient 5m data)", symbol)
            continue
        i = len(df) - 1
        bos = detect_bos(df, i)
        sweep = detect_liquidity_sweep(df, i)
        ob = detect_order_block(df, i)
        fvg = detect_fvg(df, i)

        score, zone_type, option_type, direction = _score_zone(bos, sweep, ob, fvg)

        # Score gate scales with the required component count. Each SMC
        # component is worth 25 points, so a 2-component setup tops out at 50:
        # a fixed 80 threshold would make min_components=2 unreachable and the
        # confluence gate dead config. Required score = full strength * (n/4).
        components = sum(1 for x in (bos, sweep, ob, fvg) if x is not None)
        required_score = MIN_ZONE_STRENGTH * min_components / 4.0
        if score < required_score:
            logger.debug("mcx_scanner: %s score %.0f < %.0f — skip",
                         symbol, score, required_score)
            continue

        # === DUAL-MARKET CONFLUENCE GATE ===
        if market == "NSE":
            # OB-ANCHORED: Order Block mandatory + (FVG or liquidity sweep).
            # A true institutional supply/demand zone is the OB (last opposite
            # candle before the impulse) confirmed by an imbalance (FVG) or a
            # stop-hunt reversal (sweep). BOS alone or FVG-alone are rejected
            # — they are displacement, not a tradeable zone.
            ob_anchored = ob is not None and (fvg is not None or sweep is not None)
            if not ob_anchored:
                logger.debug(
                    "mcx_scanner[NSE]: %s score %.0f but NOT OB-anchored "
                    "(OB=%s FVG=%s sweep=%s) — skip (no institutional zone)",
                    symbol, score,
                    "Y" if ob else "N", "Y" if fvg else "N", "Y" if sweep else "N")
                continue
        else:
            # MCX: count-based — min_components of {BOS, sweep, OB, FVG} agreeing.
            # A true rocket setup needs confluence, not a weak coincidence.
            if components < min_components:
                logger.debug("mcx_scanner[MCX]: %s score %.0f but only %d/%d components — skip (no rocket)",
                             symbol, score, components, min_components)
                continue

        fvg_size = float(fvg["size"]) if fvg else 0.0
        vol = calculate_atr_pct(df)
        ob_edge = ob or {"top": 0.0, "bottom": 0.0}

        zone = SniperZone(
            symbol=symbol, direction=direction, option_type=option_type,
            zone_type=zone_type, zone_strength=score,
            order_block=ob_edge, fvg=fvg, fvg_size=fvg_size,
            commodity_volatility=vol, bos=bos, liquidity_sweep=sweep,
            entry_ts=now_ts, setup_score=score,
        )
        logger.info(
            "🎯 MCX SNIPER ZONE %s %s score=%.0f dir=%s OB=[%.2f,%.2f] "
            "FVG=%s vol=%.2f%% BOS=%s sweep=%s",
            symbol, option_type, score, direction,
            ob_edge["bottom"], ob_edge["top"],
            "Y" if fvg else "N", vol,
            bos["direction"] if bos else "-",
            sweep["direction"] if sweep else "-",
        )
        if best is None or score > best.zone_strength:
            best = zone

    if best is None:
        logger.info("mcx_scanner: NO_TRADE — no MCX zone > %.0f", MIN_ZONE_STRENGTH)
    return best


def no_trade() -> dict:
    """Sentinel signal for the entry engine when no zone qualifies."""
    return {"status": "NO_TRADE", "is_sniper": True}
