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
    "BAJFINANCE": 750,
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
    "SILVER": 30,
    # MCX MINI contracts — smaller lot sizes for small capital accounts
    "GOLDM": 100,           # Gold Mini (100g vs 1kg full)
    "SILVERM": 1,           # Silver Mini (1kg vs 30kg full)
    "CRUDEOILM": 10,        # Crude Oil Mini (10 bbl vs 100 full)
    "NATGASMINI": 250,      # Natural Gas Mini (250 vs 1250 full)
}

# F&O universe — high-liquidity stocks + index + commodities
# Segmented for Brain 4's separate commodity counter
INDEX_SYMBOLS = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "^CNXFIN",  # FinNifty (financial sector index)
    "SENSEX": "^BSESN",     # BSE Sensex (BSE index)
}

STOCK_SYMBOLS = {
    # Top 10 highly liquid F&O stocks (user priority)
    "RELIANCE": "RELIANCE.NS",
    "TCS": "TCS.NS",
    "ICICIBANK": "ICICIBANK.NS",
    "HDFCBANK": "HDFCBANK.NS",
    "INFY": "INFY.NS",
    "SBIN": "SBIN.NS",
    "AXISBANK": "AXISBANK.NS",
    "LT": "LT.NS",
    "BHARTIARTL": "BHARTIARTL.NS",
    "ITC": "ITC.NS",
    # Additional high-liquidity F&O stocks
    "KOTAKBANK": "KOTAKBANK.NS",
    "WIPRO": "WIPRO.NS",
    "HCLTECH": "HCLTECH.NS",
    "MARUTI": "MARUTI.NS",
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
    "SILVER": "SI=F",  # MCX Silver (US futures proxy)
    # MCX Mini contracts — used for live MCX session (small capital friendly)
    "GOLDM": "GC=F",       # Gold Mini (yfinance proxy = Gold futures)
    "SILVERM": "SI=F",     # Silver Mini (yfinance proxy = Silver futures)
}

# ============================================================
# EXPANDED SCAN UNIVERSE — 150+ liquid NSE F&O stocks (Brain 1 scanner)
# ============================================================
# Broader high-liquidity F&O list for the pre-market gun-powder scanner.
# These are real, actively-traded NSE F&O names. Lot sizes approximated
# for sizing; the scanner uses daily/4H zones (lot size not critical there).
SCAN_STOCK_SYMBOLS = {
    # Top 20 most liquid F&O stocks — Angel One rate-limit ke liye compact
    "RELIANCE": "RELIANCE.NS", "TCS": "TCS.NS", "HDFCBANK": "HDFCBANK.NS",
    "ICICIBANK": "ICICIBANK.NS", "INFY": "INFY.NS", "SBIN": "SBIN.NS",
    "AXISBANK": "AXISBANK.NS", "LT": "LT.NS", "BHARTIARTL": "BHARTIARTL.NS",
    "ITC": "ITC.NS", "KOTAKBANK": "KOTAKBANK.NS", "BAJFINANCE": "BAJFINANCE.NS",
    "HCLTECH": "HCLTECH.NS", "MARUTI": "MARUTI.NS", "ASIANPAINT": "ASIANPAINT.NS",
    "TITAN": "TITAN.NS", "TATASTEEL": "TATASTEEL.NS", "SUNPHARMA": "SUNPHARMA.NS",
    "ADANIENT": "ADANIENT.NS", "ULTRACEMCO": "ULTRACEMCO.NS",
}

# MCX commodities expanded for scanner (Silver added)
SCAN_COMMODITY_SYMBOLS = {
    "CRUDEOIL": "CL=F", "NATURALGAS": "NG=F",
    "GOLD": "GC=F", "SILVER": "SI=F",
}


def scan_universe() -> dict:
    """Flat {symbol: ticker} map of the full scan universe (NSE + MCX).

    Used by backtest. Live path uses nse_scan_symbols() / mcx_scan_symbols()
    to fetch only the active market's symbols (rate-limit optimization).
    """
    out = dict(SCAN_STOCK_SYMBOLS)
    out.update(SCAN_COMMODITY_SYMBOLS)
    out.update(INDEX_SYMBOLS)
    return out


# ============================================================
# TWO-MARKET SESSION UNIVERSE (NSE + MCX split)
# ============================================================
# NSE:  09:15 - 15:15  →  20 stocks + 3 indices = 23 symbols
# MCX:  15:30 - 23:15  →  4 commodities (GOLDM, SILVERM, CRUDEOIL, NATURALGAS)
# Two markets NEVER overlap — Tiger fetches only the active market per scan.

def nse_scan_symbols() -> dict:
    """NSE session symbols — INDEX only (NIFTY, BANKNIFTY, FINNIFTY, SENSEX).

    STOCK options BLOCKED — ALLOWED_SYMBOLS filter in entry function
    bhi block karta hai, par yahan se hi sirf 4 index scan hote hain
    (27 → 4 symbols = 7x kam API calls, fast scan).
    """
    return dict(INDEX_SYMBOLS)


def mcx_scan_symbols() -> dict:
    """MCX session symbols — 4 commodities (mini contracts for small capital)."""
    return {
        "GOLDM": "GC=F",
        "SILVERM": "SI=F",
        "CRUDEOIL": "CL=F",
        "NATURALGAS": "NG=F",
    }


def get_active_scan_symbols(now=None) -> tuple[dict, str]:
    """Return (symbols_dict, market_label) for the currently active session.

    Returns:
        (dict, "NSE")   during 09:15-15:15
        (dict, "MCX")   during 15:30-23:15
        ({}, "CLOSED")  otherwise
    """
    from datetime import datetime, time
    from config.thresholds import AUTOMATION

    if now is None:
        now = datetime.now()
    current = now.time()

    nse_open_h, nse_open_m = map(int, AUTOMATION["MARKET_OPEN_TIME"].split(":"))
    nse_off_h, nse_off_m = map(int, AUTOMATION["NSE_SQUARE_OFF_TIME"].split(":"))
    mcx_open_h, mcx_open_m = map(int, AUTOMATION["MCX_OPEN_TIME"].split(":"))
    mcx_close_h, mcx_close_m = map(int, AUTOMATION["MCX_CLOSE_TIME"].split(":"))

    nse_open = time(nse_open_h, nse_open_m)
    nse_close = time(nse_off_h, nse_off_m)  # 15:15 square-off
    mcx_open = time(mcx_open_h, mcx_open_m)  # 15:30
    mcx_close = time(mcx_close_h, mcx_close_m)  # 23:15

    if nse_open <= current <= nse_close:
        return nse_scan_symbols(), "NSE"
    if mcx_open <= current <= mcx_close:
        return mcx_scan_symbols(), "MCX"
    return {}, "CLOSED"


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

# ============================================================
# INDIAN MARKET STRUCTURAL TIMING (NSE vs MCX)
# ============================================================
# NSE (equity F&O — index + stocks): 09:15-15:30 IST, square-off 15:15
NSE_ENTRY_WINDOWS = [
    ("09:15", "11:00"),   # Morning momentum
    ("13:30", "15:15"),   # Afternoon momentum
]
NSE_SQUARE_OFF = "15:15"

# MCX (commodities — CRUDE/GOLD/NATGAS): 09:00-23:30 IST, square-off 23:15.
# MCX has a morning session (09:00-11:30) and a long evening session
# (17:00-23:30). We allow entries across the MCX day, square-off at 23:15.
MCX_ENTRY_WINDOWS = [
    ("09:00", "11:30"),   # MCX morning session
    ("17:00", "23:00"),   # MCX evening session (high liquidity for intl commodities)
]
MCX_SQUARE_OFF = "23:15"


def entry_windows_for(segment: str) -> list:
    """Return the entry windows for a segment ('index'|'stock'|'commodity')."""
    if segment == "commodity":
        return MCX_ENTRY_WINDOWS
    return NSE_ENTRY_WINDOWS


def square_off_for(segment: str) -> str:
    """Return the hard square-off time (HH:MM) for a segment."""
    if segment == "commodity":
        return MCX_SQUARE_OFF
    return NSE_SQUARE_OFF


# ============================================================
# LIQUIDITY TIERS (Brain 3 spread safety)
# ============================================================
# Tier 1 = most liquid (index, mega-cap) → tightest spreads.
# Used to model a realistic bid-ask spread % per contract so the 0.5%
# spread gate disqualifies illiquid contracts while letting liquid ones
# through. Real NSE ATM index option spreads ~0.3-0.5%; large-cap stocks
# ~0.4-0.8%; mid-caps ~0.8-1.5%; MCX commodities ~0.6-1.2%.
LIQUIDITY_TIER = {
    "NIFTY": 1, "BANKNIFTY": 1, "FINNIFTY": 1,
    "RELIANCE": 1, "HDFCBANK": 1, "ICICIBANK": 1, "SBIN": 1, "TCS": 1,
    "INFY": 1, "AXISBANK": 1, "KOTAKBANK": 1, "BAJFINANCE": 1, "LT": 1,
    "BHARTIARTL": 2, "ITC": 2, "TATASTEEL": 2, "HINDALCO": 2, "WIPRO": 2,
    "MARUTI": 2, "SUNPHARMA": 2, "TITAN": 2, "ADANIENT": 2, "GRASIM": 2,
    "HCLTECH": 2, "TECHM": 2, "ASIANPAINT": 2, "ULTRACEMCO": 2,
    "POWERGRID": 3, "NTPC": 3, "ONGC": 3, "COALINDIA": 3, "DIVISLAB": 3,
    "CIPLA": 3, "DRREDDY": 3, "BAJAJFINSV": 3, "NESTLEIND": 3, "BRITANNIA": 3,
    "TATACONSUM": 3,
    # MCX commodities — separate session, different spread regime
    "CRUDEOIL": 2, "GOLD": 2, "SILVER": 2, "NATURALGAS": 3,
}


def liquidity_tier(symbol: str) -> int:
    """Return 1 (most liquid), 2, or 3 (least liquid)."""
    return LIQUIDITY_TIER.get(symbol, 3)


# ============================================================
# V6.5 — EXPIRY DAY SCHEDULE (Brain 3 zero-to-hero engine)
# ============================================================
# NSE weekly expiry schedule (default conventions, IST):
#   NIFTY      → Thursday
#   BANKNIFTY  → Wednesday  (post-Nov 2023; was Friday earlier)
#   Stocks (monthly) → last Thursday of the month
# MCX commodity expiry → last business day of the month (approx).
EXPIRY_DAY_OF_WEEK = {
    "NIFTY": 3,        # Thursday (0=Mon)
    "BANKNIFTY": 2,    # Wednesday
    "FINNIFTY": 1,     # Tuesday
}


def is_expiry_day(symbol: str, dt) -> bool:
    """True if `dt` is the weekly/monthly expiry day for the symbol."""
    import pandas as pd
    d = pd.Timestamp(dt)
    if d.tz is not None:
        d = d.tz_convert("Asia/Kolkata")
    dow = d.weekday()
    # Index: weekly expiry on the assigned weekday
    if symbol in EXPIRY_DAY_OF_WEEK:
        return dow == EXPIRY_DAY_OF_WEEK[symbol]
    # Stocks: monthly expiry = last Thursday of the month
    if symbol in SCAN_STOCK_SYMBOLS or symbol in STOCK_SYMBOLS:
        # last Thursday of the month
        last_day = d + pd.offsets.MonthEnd(0)
        # walk back to the last Thursday (weekday 3)
        t = last_day
        while t.weekday() != 3:
            t = t - pd.Timedelta(days=1)
        return d.date() == t.date()
    # MCX commodities: last business day of month (approx)
    if symbol in SCAN_COMMODITY_SYMBOLS or symbol in COMMODITY_SYMBOLS:
        last_day = (d + pd.offsets.MonthEnd(0)).date()
        return d.date() == last_day
    return False
