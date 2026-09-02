"""
Tiger Brain V6.2 — Expanded F&O Universe (Intraday)
=====================================================
Pure intraday options-buying machine ke liye full high-liquidity F&O
universe. Daily 5-10 trades per segment target ke liye 50+ stocks +
index + commodities.

⚠️ HONESTY: yfinance 15-minute data sirf ~60 din ka history deta hai.
Isliye ye universe sirf un symbols tak limited hai jinka 15m data
reliably mil raha hai. Real deployment pe Angel One / broker se full
F&O list aayegi.

Symbol naming convention:
  - NSE stocks: "<SYMBOL>.NS"
  - Index: "^NSEI" (NIFTY), "^NSEBANK" (BANKNIFTY)
  - Commodities (proxy): "CL=F" (CRUDE), "GC=F" (GOLD) — US futures,
    timezone alag hai, session IST se align hota hai approx.
"""

from __future__ import annotations

# Exchange-standard lot sizes (approximate — real lots change quarterly)
LOT_SIZES = {
    "NIFTY": 75,
    "BANKNIFTY": 35,
    "FINNIFTY": 65,
    # High-liquidity F&O stocks (lot size approximation)
    "RELIANCE": 250,
    "SBIN": 300,
    "HDFCBANK": 550,
    "ICICIBANK": 700,
    "AXISBANK": 625,
    "KOTAKBANK": 400,
    "TCS": 175,
    "INFY": 400,
    "WIPRO": 6000,
    "HCLTECH": 700,
    "LT": 175,
    "MARUTI": 50,
    "ITC": 3200,
    "BHARTIARTL": 475,
    "TATASTEEL": 2100,
    "SUNPHARMA": 700,
    "ADANIENT": 200,
    "TATACONSUM": 800,
    "BAJFINANCE": 125,
    "ASIANPAINT": 400,
    "ULTRACEMCO": 150,
    "TITAN": 175,
    "POWERGRID": 3850,
    "NTPC": 1925,
    "ONGC": 3850,
    "COALINDIA": 3200,
    "TECHM": 600,
    "DIVISLAB": 150,
    "CIPLA": 850,
    "DRREDDY": 125,
    "GRASIM": 300,
    "JSWSTEEL": 260,
    "HINDALCO": 1075,
    "BAJAJFINSV": 175,
    "NESTLEIND": 125,
    "DABUR": 1300,
    "BRITANNIA": 200,
    # Commodities (MCX proxy via US futures)
    "CRUDEOIL": 100,
    "NATURALGAS": 1250,
    "GOLD": 100,
}

# F&O universe — high-liquidity stocks + index + commodities
# Segmented for Brain 4's separate commodity counter
INDEX_SYMBOLS = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
}

STOCK_SYMBOLS = {
    "RELIANCE": "RELIANCE.NS",
    "SBIN": "SBIN.NS",
    "HDFCBANK": "HDFCBANK.NS",
    "ICICIBANK": "ICICIBANK.NS",
    "AXISBANK": "AXISBANK.NS",
    "KOTAKBANK": "KOTAKBANK.NS",
    "TCS": "TCS.NS",
    "INFY": "INFY.NS",
    "WIPRO": "WIPRO.NS",
    "HCLTECH": "HCLTECH.NS",
    "LT": "LT.NS",
    "MARUTI": "MARUTI.NS",
    "ITC": "ITC.NS",
    "BHARTIARTL": "BHARTIARTL.NS",
    "TATASTEEL": "TATASTEEL.NS",
    "SUNPHARMA": "SUNPHARMA.NS",
    "ADANIENT": "ADANIENT.NS",
    "TATACONSUM": "TATACONSUM.NS",
    "BAJFINANCE": "BAJFINANCE.NS",
    "ASIANPAINT": "ASIANPAINT.NS",
    "ULTRACEMCO": "ULTRACEMCO.NS",
    "TITAN": "TITAN.NS",
    "POWERGRID": "POWERGRID.NS",
    "NTPC": "NTPC.NS",
    "ONGC": "ONGC.NS",
    "COALINDIA": "COALINDIA.NS",
    "TECHM": "TECHM.NS",
    "DIVISLAB": "DIVISLAB.NS",
    "CIPLA": "CIPLA.NS",
    "DRREDDY": "DRREDDY.NS",
    "GRASIM": "GRASIM.NS",
    "HINDALCO": "HINDALCO.NS",
    "BAJAJFINSV": "BAJAJFINSV.NS",
    "NESTLEIND": "NESTLEIND.NS",
    "BRITANNIA": "BRITANNIA.NS",
}

COMMODITY_SYMBOLS = {
    "CRUDEOIL": "CL=F",
    "NATURALGAS": "NG=F",
    "GOLD": "GC=F",
}

# Combined universe grouped by segment
UNIVERSE = {
    "index": {"label": "Index Options (NIFTY/BANKNIFTY)", "symbols": INDEX_SYMBOLS, "category": "equity"},
    "stock": {"label": "Stock Options (Top F&O)", "symbols": STOCK_SYMBOLS, "category": "equity"},
    "commodity": {"label": "Commodity Options (CRUDE/GOLD)", "symbols": COMMODITY_SYMBOLS, "category": "commodity"},
}


def all_symbols() -> dict:
    """Return flat {symbol: ticker} map across all segments."""
    out = {}
    for seg in UNIVERSE.values():
        out.update(seg["symbols"])
    return out


def segment_of(symbol: str) -> str:
    """Return segment key for a symbol."""
    for seg_key, seg in UNIVERSE.items():
        if symbol in seg["symbols"]:
            return seg_key
    return "stock"


def lot_size(symbol: str) -> int:
    """Return exchange-standard lot size for a symbol."""
    return LOT_SIZES.get(symbol, 1)


# Intraday execution windows (IST) — Brain 1 time filter
# High-momentum hours only; no entries in the dead 11:00-13:30 zone
ENTRY_WINDOWS = [
    ("09:15", "11:00"),   # Morning momentum
    ("13:30", "15:15"),   # Afternoon momentum
]

# Hard square-off time — Brain 5 MUST exit everything here
SQUARE_OFF_TIME = "15:15"
