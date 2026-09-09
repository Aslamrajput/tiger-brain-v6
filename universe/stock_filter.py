"""Tiger V19 — Stock Liquidity Filter Pipeline (Section 28)

All F&O Stocks → Option Volume → Premium Turnover → OI → Bid-Ask
Spread → OI+Volume Confirmation → Liquidity Score → Top 10-11 Stocks
→ Signal → CALL / PUT

Tiger options BUYING only — isliye liquidity sabse zaroori hai. Illiquid
stock options pe slippage khayega. Ye pipeline Bhavcopy (EOD) data se
top liquid stocks chunta hai, taaki live scan me sirf wahi stocks aaye.

Usage:
    from universe.stock_filter import filter_top_liquid_stocks
    top_stocks = filter_top_liquid_stocks()  # returns ["RELIANCE", "HDFCBANK", ...]
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd

logger = logging.getLogger(__name__)

# Sabse liquid F&O stocks — fallback jab Bhavcopy na mile
# (NSE se download fail ho ya holiday ho)
FALLBACK_TOP_STOCKS = [
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "SBIN",
    "AXISBANK", "LT", "BHARTIARTL", "ITC", "KOTAKBANK",
    "BAJFINANCE",
]

# Pipeline thresholds — har stage pe kitna filter karna hai
MIN_OPTION_VOLUME = 500_000        # contracts/day — very low volume hata
MIN_PREMIUM_TURNOVER_LAKH = 500   # ₹500 lakh/day premium turnover minimum
MIN_OPEN_INTEREST = 100_000       # 1 lakh OI minimum (deep market)
TOP_N_STOCKS = 11                 # final top 10-11 stocks


def _aggregate_stock_liquidity(bhavcopy: pd.DataFrame) -> pd.DataFrame:
    """Bhavcopy se per-stock liquidity metrics aggregate karo.

    Bhavcopy me har option contract ek row hai. Hum per STOCK total
    nikalte hain (CE+PE sab expiries ka sum).

    Returns:
        DataFrame: one row per stock, columns:
        [SYMBOL, total_contracts, total_value_lakh, total_oi, liquidity_score]
    """
    # Sirf stock options rakho (OPTSTK) — index options hata do
    # INSTRUMENT column: 'OPTSTK' ya 'OPTIDX'
    if "INSTRUMENT" not in bhavcopy.columns:
        logger.warning("Bhavcopy me INSTRUMENT column nahi — skip filter.")
        return pd.DataFrame()

    stock_opts = bhavcopy[bhavcopy["INSTRUMENT"] == "OPTSTK"].copy()
    if stock_opts.empty:
        # Some Bhavcopy files use different column names
        logger.warning("Bhavcopy me OPTSTK rows nahi mili.")
        return pd.DataFrame()

    # Aggregate per stock symbol
    agg = stock_opts.groupby("SYMBOL").agg(
        total_contracts=("CONTRACTS", "sum"),
        total_value_lakh=("VAL_INLAKH", "sum"),
        total_oi=("OPEN_INT", "sum"),
        contracts_count=("CONTRACTS", "count"),
    ).reset_index()

    return agg


def _liquidity_score(row: pd.Series) -> float:
    """Liquidity Score = 0.35*volume_rank + 0.25*turnover_rank +
    0.25*oi_rank + 0.15*spread_proxy_rank.

    Bid-ask spread real-time nahi milta Bhavcopy se, isliye spread ko
    contracts_count (kitne strikes/expiries active hain) se proxy karte
    hain — zyada active strikes = tighter spread (liquid market).
    """
    return (
        row.get("volume_rank", 0) * 0.35
        + row.get("turnover_rank", 0) * 0.25
        + row.get("oi_rank", 0) * 0.25
        + row.get("spread_rank", 0) * 0.15
    )


def filter_top_liquid_stocks(top_n: int = TOP_N_STOCKS) -> list[str]:
    """All F&O stocks → liquidity pipeline → top 10-11 stocks.

    Pipeline stages:
      1. Option Volume filter (MIN_OPTION_VOLUME)
      2. Premium Turnover filter (MIN_PREMIUM_TURNOVER_LAKH)
      3. Open Interest filter (MIN_OPEN_INTEREST)
      4. Liquidity Score (rank remaining stocks)
      5. Top N stocks

    Uses yesterday's NSE Bhavcopy (EOD data). Agar Bhavcopy na mile
    (holiday/weekend/NSE block), to FALLBACK_TOP_STOCKS use karta hai.

    Returns:
        list of stock symbol names (uppercase), max top_n items.
    """
    from data.loader import fetch_nse_bhavcopy

    # Try yesterday's Bhavcopy (most recent complete trading day)
    # Weekend pe Friday ka data
    for days_back in range(1, 5):
        trade_date = datetime.now() - timedelta(days=days_back)
        if trade_date.weekday() >= 5:  # skip Sat/Sun
            continue
        try:
            bhavcopy = fetch_nse_bhavcopy(trade_date)
            if bhavcopy is not None and not bhavcopy.empty:
                logger.info("Stock filter: Bhavcopy %s se (%d rows)",
                             trade_date.date(), len(bhavcopy))
                break
        except Exception as exc:
            logger.debug("Bhavcopy %s fail: %s", trade_date.date(), exc)
            bhavcopy = None
    else:
        logger.warning("Stock filter: Bhavcopy nahi mili — fallback top %d stocks.",
                       len(FALLBACK_TOP_STOCKS))
        return FALLBACK_TOP_STOCKS[:top_n]

    if bhavcopy is None:
        return FALLBACK_TOP_STOCKS[:top_n]

    # Stage 1: Aggregate per stock
    agg = _aggregate_stock_liquidity(bhavcopy)
    if agg.empty:
        logger.warning("Stock filter: aggregation empty — fallback.")
        return FALLBACK_TOP_STOCKS[:top_n]

    start_count = len(agg)

    # Stage 1: Option Volume filter
    agg = agg[agg["total_contracts"] >= MIN_OPTION_VOLUME]
    after_vol = len(agg)

    # Stage 2: Premium Turnover filter
    agg = agg[agg["total_value_lakh"] >= MIN_PREMIUM_TURNOVER_LAKH]
    after_turnover = len(agg)

    # Stage 3: Open Interest filter
    agg = agg[agg["total_oi"] >= MIN_OPEN_INTEREST]
    after_oi = len(agg)

    if agg.empty:
        logger.warning("Stock filter: sab filters ke baad 0 stocks — fallback.")
        return FALLBACK_TOP_STOCKS[:top_n]

    # Stage 4: Liquidity Score (rank each metric 0-100)
    agg["volume_rank"] = agg["total_contracts"].rank(pct=True) * 100
    agg["turnover_rank"] = agg["total_value_lakh"].rank(pct=True) * 100
    agg["oi_rank"] = agg["total_oi"].rank(pct=True) * 100
    agg["spread_rank"] = agg["contracts_count"].rank(pct=True) * 100
    agg["liquidity_score"] = agg.apply(_liquity_score_safe, axis=1)

    # Stage 5: Top N
    agg = agg.sort_values("liquidity_score", ascending=False)
    top = agg.head(top_n)["SYMBOL"].tolist()

    logger.info(
        "Stock liquidity pipeline: %d → vol %d → turnover %d → OI %d → top %d: %s",
        start_count, after_vol, after_turnover, after_oi, len(top), top
    )
    return top


def _liquity_score_safe(row: pd.Series) -> float:
    """_liquidity_score wrapper — exception safe."""
    try:
        return _liquidity_score(row)
    except Exception:
        return 0.0


if __name__ == "__main__":
    stocks = filter_top_liquid_stocks()
    print(f"\nTop {len(stocks)} liquid F&O stocks:")
    for i, s in enumerate(stocks, 1):
        print(f"  {i:2d}. {s}")
