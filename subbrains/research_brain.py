"""Tiger Research Brain — 24/7 top gainers/losers + SMC trade planning.

User mandate:
  "Sub Brain sirf score nahi denge — kaam khatam research karenge.
   Same day ki bhi, or kal kisme trade kis jangha leni hai, iski bhi.
   Top losers, top gainers pe 24/7 research. No over research.
   Good and unique powerful research. Buying buying main pesa easy hai,
   isliye only buying. Real SMC and real supply zone, real demand zone,
   mathematical use karenge."

Flow:
  1. scan_top_movers() — fetch daily data for ALL NSE F&O stocks via
     yfinance (one batch, pre-market), compute daily % change.
  2. Top 10 gainers + top 10 losers → focus list.
  3. analyze_movers_smc() — run detect_zones_explosive on each mover's
     15m chart → find supply/demand zones with confluence.
  4. generate_trade_plan() — structured plan:
     - Today's watchlist (stocks with zones near price)
     - Direction bias (gainer → demand zone buy, loser → oversold bounce)
     - Tomorrow's candidates (zones forming, momentum building)
  5. Tiger's intraday scan uses this plan to prioritize symbols.

This is a RESEARCH brain — it informs, never blocks. Tiger decides.
Runs once pre-market (9:00 AM), cached for the day.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

from pipeline.intraday_strategies import detect_zones_explosive

logger = logging.getLogger(__name__)

# ============================================================
# Config
# ============================================================
TOP_N_MOVERS = 10          # top 10 gainers + top 10 losers
MIN_DAILY_VOLUME_CR = 50   # ₹50cr min volume (liquidity filter)
RESEARCH_CACHE_FILE = Path("data_cache/research_plan.json")
RESEARCH_CACHE_HOURS = 12  # cache valid for 12 hours (pre-market refresh)


# ============================================================
# Data Models
# ============================================================
@dataclass
class MoverAnalysis:
    """One stock's research result."""
    symbol: str
    name: str
    daily_change_pct: float
    direction: str            # "GAINER" or "LOSER"
    close: float
    volume_cr: float
    # SMC zones
    nearest_demand_zone: Optional[dict] = None
    nearest_supply_zone: Optional[dict] = None
    zone_strength: float = 0.0
    # Trade bias
    bias: str = "NEUTRAL"     # "BUY_CE", "BUY_PE", "NEUTRAL"
    bias_reason: str = ""
    # Score for prioritization
    research_score: float = 0.0


@dataclass
class TradePlan:
    """Full research plan for today + tomorrow."""
    generated_at: str
    trade_date: str
    top_gainers: list[dict] = field(default_factory=list)
    top_losers: list[dict] = field(default_factory=list)
    today_watchlist: list[dict] = field(default_factory=list)
    tomorrow_candidates: list[dict] = field(default_factory=list)
    market_bias: str = "NEUTRAL"
    notes: str = ""

    def summary(self) -> str:
        lines = [
            f"\n{'='*60}",
            f"  🧠 TIGER RESEARCH BRAIN — Trade Plan for {self.trade_date}",
            f"{'='*60}",
            f"  Generated: {self.generated_at}",
            f"  Market Bias: {self.market_bias}",
            f"",
            f"  📈 TOP GAINERS (buy CE on demand zone retest):",
        ]
        for m in self.top_gainers[:5]:
            lines.append(
                f"    {m['symbol']:15s} +{m['daily_change_pct']:.1f}%  "
                f"bias={m['bias']}  score={m['research_score']:.0f}")
        lines.append(f"  📉 TOP LOSERS (buy PE on supply zone / oversold bounce):")
        for m in self.top_losers[:5]:
            lines.append(
                f"    {m['symbol']:15s} {m['daily_change_pct']:.1f}%  "
                f"bias={m['bias']}  score={m['research_score']:.0f}")
        lines.append(f"  🎯 TODAY'S WATCHLIST ({len(self.today_watchlist)} stocks):")
        for w in self.today_watchlist[:5]:
            lines.append(
                f"    {w['symbol']:15s} {w['action']:8s}  "
                f"zone={w.get('zone_type','—')}  "
                f"entry_near={w.get('zone_price','—')}")
        lines.append(f"  🔮 TOMORROW'S CANDIDATES ({len(self.tomorrow_candidates)}):")
        for t in self.tomorrow_candidates[:3]:
            lines.append(f"    {t['symbol']:15s} building {t.get('pattern','—')}")
        lines.append(f"{'='*60}")
        return "\n".join(lines)


# ============================================================
# Core Functions
# ============================================================
def scan_top_movers(symbols: dict[str, str], top_n: int = TOP_N_MOVERS) -> tuple[list, list]:
    """Fetch daily data for all symbols, return top gainers + losers.

    Args:
        symbols: {symbol: yfinance_ticker} dict
        top_n: number of top gainers/losers to return

    Returns:
        (gainers, losers) — each is a list of dicts with
        {symbol, daily_change_pct, close, volume_cr}
    """
    logger.info(f"🧠 Research Brain: scanning {len(symbols)} symbols for top movers...")

    movers = []
    batch_tickers = list(symbols.values())
    batch_symbols = list(symbols.keys())

    # Fetch 5-day daily data for all symbols (batch, one call)
    try:
        raw = yf.download(
            batch_tickers,
            period="5d",
            interval="1d",
            progress=False,
            group_by="ticker",
            threads=True,
        )
    except Exception as exc:
        logger.error(f"Research Brain: yfinance batch fail: {exc}")
        return [], []

    for sym, ticker in symbols.items():
        try:
            if batch_tickers and len(batch_tickers) > 1:
                col_key = ticker
                if col_key not in raw.columns.get_level_values(0):
                    continue
                df = raw[col_key].dropna()
            else:
                df = raw.dropna()

            if df is None or len(df) < 2:
                continue

            today_close = float(df["Close"].iloc[-1])
            prev_close = float(df["Close"].iloc[-2])
            if prev_close <= 0:
                continue

            change_pct = ((today_close - prev_close) / prev_close) * 100

            # Volume in ₹ crore (approx: close * volume / 1e7)
            if "Volume" in df.columns:
                vol = float(df["Volume"].iloc[-1])
                volume_cr = (today_close * vol) / 1e7
            else:
                volume_cr = 0.0

            # Liquidity filter
            if volume_cr < MIN_DAILY_VOLUME_CR:
                continue

            movers.append({
                "symbol": sym,
                "name": sym,
                "daily_change_pct": round(change_pct, 2),
                "close": round(today_close, 2),
                "volume_cr": round(volume_cr, 1),
            })
        except Exception:
            continue

    if not movers:
        logger.warning("Research Brain: no movers found (data empty)")
        return [], []

    # Sort: gainers (descending), losers (ascending)
    gainers = sorted(movers, key=lambda x: x["daily_change_pct"], reverse=True)[:top_n]
    losers = sorted(movers, key=lambda x: x["daily_change_pct"])[:top_n]

    logger.info(
        f"🧠 Research Brain: {len(movers)} stocks scanned | "
        f"Top gainer: {gainers[0]['symbol']} +{gainers[0]['daily_change_pct']}% | "
        f"Top loser: {losers[0]['symbol']} {losers[0]['daily_change_pct']}%")

    return gainers, losers


def analyze_movers_smc(movers: list[dict]) -> list[MoverAnalysis]:
    """Run SMC zone detection on each mover's 15m chart.

    For each mover, fetch 15m data (1 month), run detect_zones_explosive,
    find nearest supply + demand zones to current price.
    """
    results = []
    for m in movers:
        try:
            sym = m["symbol"]
            ticker = m.get("ticker", f"{sym}.NS")

            # Fetch 15m data (30 days = enough for zone formation)
            df = yf.download(
                ticker, period="30d", interval="15m",
                progress=False, threads=False,
            )
            if df is None or len(df) < 40:
                continue

            df = df.dropna()
            if len(df) < 40:
                continue

            # Standardize columns
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            # Run zone detection at the latest bar
            zones = detect_zones_explosive(df, len(df) - 1, lookback=40)
            if not zones:
                continue

            current_price = float(df["Close"].iloc[-1])

            # Find nearest demand (below price) and supply (above price)
            nearest_demand = None
            nearest_supply = None
            best_strength = 0.0

            for z in zones:
                z_top = float(z.get("zone_top", 0))
                z_bot = float(z.get("zone_bottom", 0))
                z_type = z.get("type", "")
                z_strength = float(z.get("score", 0))

                if z_strength > best_strength:
                    best_strength = z_strength

                if z_type == "demand" and z_top <= current_price:
                    dist = (current_price - z_top) / current_price * 100
                    if nearest_demand is None or dist < nearest_demand.get("_dist", 999):
                        z["_dist"] = dist
                        nearest_demand = z
                elif z_type == "supply" and z_bot >= current_price:
                    dist = (z_bot - current_price) / current_price * 100
                    if nearest_supply is None or dist < nearest_supply.get("_dist", 999):
                        z["_dist"] = dist
                        nearest_supply = z

            # Determine bias
            direction = "GAINER" if m["daily_change_pct"] > 0 else "LOSER"
            bias = "NEUTRAL"
            bias_reason = ""

            if direction == "GAINER" and nearest_demand:
                bias = "BUY_CE"
                bias_reason = (
                    f"Gainer + demand zone at {nearest_demand.get('zone_top', 0):.0f} "
                    f"({nearest_demand.get('_dist', 0):.1f}% below) — "
                    f"buy CE on retest")
            elif direction == "LOSER" and nearest_supply:
                bias = "BUY_PE"
                bias_reason = (
                    f"Loser + supply zone at {nearest_supply.get('zone_top', 0):.0f} "
                    f"({nearest_supply.get('_dist', 0):.1f}% above) — "
                    f"buy PE on rejection")
            elif direction == "LOSER" and nearest_demand:
                # Oversold loser bouncing off demand → buy CE
                dist = nearest_demand.get("_dist", 999)
                if dist < 2.0:  # within 2% of demand zone
                    bias = "BUY_CE"
                    bias_reason = (
                        f"Oversold loser near demand zone "
                        f"({dist:.1f}% below) — bounce buy CE")

            # Research score: strength + proximity + momentum
            momentum_score = min(abs(m["daily_change_pct"]), 10.0)
            zone_proximity = 0
            if nearest_demand:
                zone_proximity += max(0, 10 - nearest_demand.get("_dist", 10))
            if nearest_supply:
                zone_proximity += max(0, 10 - nearest_supply.get("_dist", 10))
            research_score = best_strength + momentum_score + zone_proximity

            results.append(MoverAnalysis(
                symbol=sym,
                name=m["name"],
                daily_change_pct=m["daily_change_pct"],
                direction=direction,
                close=m["close"],
                volume_cr=m["volume_cr"],
                nearest_demand_zone={
                    "top": float(nearest_demand.get("zone_top", 0)),
                    "bottom": float(nearest_demand.get("zone_bottom", 0)),
                    "dist_pct": nearest_demand.get("_dist", 0),
                } if nearest_demand else None,
                nearest_supply_zone={
                    "top": float(nearest_supply.get("zone_top", 0)),
                    "bottom": float(nearest_supply.get("zone_bottom", 0)),
                    "dist_pct": nearest_supply.get("_dist", 0),
                } if nearest_supply else None,
                zone_strength=best_strength,
                bias=bias,
                bias_reason=bias_reason,
                research_score=round(research_score, 1),
            ))
        except Exception as exc:
            logger.debug(f"Research Brain: SMC analysis skip {m['symbol']}: {exc}")
            continue

    return results


def generate_trade_plan(
    gainers_analysis: list[MoverAnalysis],
    losers_analysis: list[MoverAnalysis],
) -> TradePlan:
    """Combine gainer + loser analysis into a structured trade plan."""
    now = datetime.now()

    # Market bias: more strong gainers → bullish, more strong losers → bearish
    strong_gainers = sum(1 for g in gainers_analysis if g.research_score > 15)
    strong_losers = sum(1 for l in losers_analysis if l.research_score > 15)
    if strong_gainers > strong_losers * 1.5:
        market_bias = "BULLISH"
    elif strong_losers > strong_gainers * 1.5:
        market_bias = "BEARISH"
    else:
        market_bias = "NEUTRAL"

    # Today's watchlist: stocks with zones near price + clear bias
    today_watch = []
    for a in sorted(gainers_analysis + losers_analysis,
                    key=lambda x: x.research_score, reverse=True):
        if a.bias == "NEUTRAL":
            continue
        zone_type = "demand" if a.nearest_demand_zone else "supply" if a.nearest_supply_zone else "—"
        zone_price = (a.nearest_demand_zone or a.nearest_supply_zone or {}).get("top", 0)
        today_watch.append({
            "symbol": a.symbol,
            "action": "BUY_CE" if a.bias == "BUY_CE" else "BUY_PE",
            "zone_type": zone_type,
            "zone_price": round(zone_price, 1),
            "current_price": a.close,
            "research_score": a.research_score,
            "reason": a.bias_reason,
        })

    # Tomorrow's candidates: movers with zones forming but not yet at price
    tomorrow = []
    for a in gainers_analysis + losers_analysis:
        if a.research_score > 5 and a.bias != "NEUTRAL":
            tomorrow.append({
                "symbol": a.symbol,
                "pattern": f"{a.direction} with {a.zone_strength:.0f} zone",
                "bias": a.bias,
                "daily_change_pct": a.daily_change_pct,
            })

    plan = TradePlan(
        generated_at=now.strftime("%Y-%m-%d %H:%M:%S"),
        trade_date=now.date().isoformat(),
        top_gainers=[asdict(a) for a in gainers_analysis[:TOP_N_MOVERS]],
        top_losers=[asdict(a) for a in losers_analysis[:TOP_N_MOVERS]],
        today_watchlist=today_watch[:10],
        tomorrow_candidates=tomorrow[:5],
        market_bias=market_bias,
        notes=(
            f"Only buying. Real SMC zones. "
            f"Buy CE on demand retest (gainers), "
            f"buy PE on supply rejection (losers)."
        ),
    )

    return plan


def save_plan(plan: TradePlan, filepath: Path = RESEARCH_CACHE_FILE) -> None:
    """Save trade plan to JSON cache (restart-safe)."""
    try:
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w") as f:
            json.dump(asdict(plan), f, indent=2, default=str)
        logger.info(f"🧠 Research plan saved: {filepath}")
    except Exception as exc:
        logger.warning(f"Research plan save fail: {exc}")


def load_plan(filepath: Path = RESEARCH_CACHE_FILE) -> Optional[TradePlan]:
    """Load cached plan if still valid (within RESEARCH_CACHE_HOURS)."""
    try:
        if not filepath.exists():
            return None
        with open(filepath) as f:
            data = json.load(f)
        generated = datetime.fromisoformat(data["generated_at"])
        age_hours = (datetime.now() - generated).total_seconds() / 3600
        if age_hours > RESEARCH_CACHE_HOURS:
            logger.info(f"🧠 Research plan stale ({age_hours:.1f}h old), refreshing...")
            return None
        return TradePlan(**data)
    except Exception:
        return None


def run_research(symbols: dict[str, str]) -> Optional[TradePlan]:
    """Main entry point — run full research and return trade plan.

    Called pre-market (9:00 AM). Cached for the day.
    Use yfinance daily data for movers, 15m for SMC zones.
    """
    # Check cache first
    cached = load_plan()
    if cached:
        logger.info(f"🧠 Research plan cached (generated {cached.generated_at})")
        return cached

    logger.info("🧠 Research Brain: starting full scan...")

    # Step 1: scan top movers
    gainers, losers = scan_top_movers(symbols)
    if not gainers and not losers:
        logger.warning("Research Brain: no movers — data unavailable")
        return None

    # Step 2: SMC analysis on each mover
    gainers_analysis = analyze_movers_smc(gainers)
    losers_analysis = analyze_movers_smc(losers)

    # Step 3: generate trade plan
    plan = generate_trade_plan(gainers_analysis, losers_analysis)

    # Step 4: save cache
    save_plan(plan)

    logger.info(plan.summary())
    return plan


def get_today_watchlist() -> list[str]:
    """Return today's priority symbols from the research plan.

    Tiger's intraday scan checks these first — research-informed hunting.
    """
    plan = load_plan()
    if plan is None:
        return []
    return [w["symbol"] for w in plan.today_watchlist if w.get("symbol")]
