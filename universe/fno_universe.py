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
    "SILVER": 30,
}

# F&O universe — high-liquidity stocks + index + commodities
# Segmented for Brain 4's separate commodity counter
INDEX_SYMBOLS = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "^CNXFIN",  # FinNifty (financial sector index)
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
}

# ============================================================
# EXPANDED SCAN UNIVERSE — 150+ liquid NSE F&O stocks (Brain 1 scanner)
# ============================================================
# Broader high-liquidity F&O list for the pre-market gun-powder scanner.
# These are real, actively-traded NSE F&O names. Lot sizes approximated
# for sizing; the scanner uses daily/4H zones (lot size not critical there).
SCAN_STOCK_SYMBOLS = {
    # --- Financials ---
    "RELIANCE": "RELIANCE.NS", "SBIN": "SBIN.NS", "HDFCBANK": "HDFCBANK.NS",
    "ICICIBANK": "ICICIBANK.NS", "AXISBANK": "AXISBANK.NS", "KOTAKBANK": "KOTAKBANK.NS",
    "BAJFINANCE": "BAJFINANCE.NS", "BAJAJFINSV": "BAJAJFINSV.NS", "INDUSINDBK": "INDUSINDBK.NS",
    "AUBANK": "AUBANK.NS", "FEDERALBNK": "FEDERALBNK.NS", "IDFCFIRSTB": "IDFCFIRSTB.NS",
    "PNB": "PNB.NS", "BANKBARODA": "BANKBARODA.NS", "CANBK": "CANBK.NS",
    "SBILIFE": "SBILIFE.NS", "HDFCLIFE": "HDFCLIFE.NS", "ICICIPRULI": "ICICIPRULI.NS",
    "SBICARD": "SBICARD.NS", "CHOLAFIN": "CHOLAFIN.NS", "MUTHOOTFIN": "MUTHOOTFIN.NS",
    "PFC": "PFC.NS", "RECLTD": "RECLTD.NS", "M&MFIN": "MMFIN.NS",
    # --- IT ---
    "TCS": "TCS.NS", "INFY": "INFY.NS", "WIPRO": "WIPRO.NS", "HCLTECH": "HCLTECH.NS",
    "TECHM": "TECHM.NS", "LTIM": "LTIM.NS", "COFORGE": "COFORGE.NS",
    # --- Energy / Oil / Metals ---
    "ONGC": "ONGC.NS", "COALINDIA": "COALINDIA.NS", "NTPC": "NTPC.NS", "POWERGRID": "POWERGRID.NS",
    "TATASTEEL": "TATASTEEL.NS", "JSWSTEEL": "JSWSTEEL.NS", "HINDALCO": "HINDALCO.NS",
    "JINDALSTEL": "JINDALSTEL.NS", "SAIL": "SAIL.NS", "VEDL": "VEDL.NS",
    "NATIONALUM": "NATIONALUM.NS", "HINDZINC": "HINDZINC.NS",
    "GAIL": "GAIL.NS", "IGL": "IGL.NS", "PETRONET": "PETRONET.NS", "OIL": "OIL.NS",
    # --- Auto ---
    "MARUTI": "MARUTI.NS", "TATAMOTORS": "TATAMOTORS.NS", "M&M": "M&M.NS",
    "EICHERMOT": "EICHERMOT.NS", "BOSCHLTD": "BOSCHLTD.NS", "HEROMOTOCO": "HEROMOTOCO.NS",
    "BAJAJ-AUTO": "BAJAJ-AUTO.NS", "TVSMOTOR": "TVSMOTOR.NS", "ASHOKLEY": "ASHOKLEY.NS",
    "MOTHERSON": "MOTHERSON.NS", "BALKRISIND": "BALKRISIND.NS", "TIINDIA": "TIINDIA.NS",
    # --- FMCG / Consumer ---
    "ITC": "ITC.NS", "HINDUNILVR": "HINDUNILVR.NS", "NESTLEIND": "NESTLEIND.NS",
    "BRITANNIA": "BRITANNIA.NS", "DABUR": "DABUR.NS", "TATACONSUM": "TATACONSUM.NS",
    "VARUNBEVER": "VARUNBEVER.NS", "GODREJCP": "GODREJCP.NS", "MARICO": "MARICO.NS",
    "COLPAL": "COLPAL.NS", "UNITDSPR": "UNITDSPR.NS",
    # --- Pharma / Healthcare ---
    "SUNPHARMA": "SUNPHARMA.NS", "DRREDDY": "DRREDDY.NS", "CIPLA": "CIPLA.NS",
    "DIVISLAB": "DIVISLAB.NS", "APOLLOHOSP": "APOLLOHOSP.NS", "LUPIN": "LUPIN.NS",
    "AUROPHARMA": "AUROPHARMA.NS", "BIOCON": "BIOCON.NS", "TORNTPHARM": "TORNTPHARM.NS",
    "CADILAHC": "CADILAHC.NS", "ZYDUSLIFE": "ZYDUSLIFE.NS", "LAURUSLABS": "LAURUSLABS.NS",
    # --- Telecom / Media ---
    "BHARTIARTL": "BHARTIARTL.NS", "IDEA": "IDEA.NS", "SUNTV": "SUNTV.NS",
    "ZEE": "ZEE.NS", "PVR": "PVR.NS", "INOXLEISUR": "INOXLEISUR.NS",
    # --- Infra / Cement / Realty ---
    "LT": "LT.NS", "ULTRACEMCO": "ULTRACEMCO.NS", "GRASIM": "GRASIM.NS",
    "SHREECEM": "SHREECEM.NS", "AMBUJACEM": "AMBUJACEM.NS", "ACC": "ACC.NS",
    "DLF": "DLF.NS", "GODREJPROP": "GODREJPROP.NS", "OBEROIRLTY": "OBEROIRLTY.NS",
    "BRIGADE": "BRIGADE.NS", "PHOENIXTD": "PHOENIXTD.NS",
    "ABB": "ABB.NS", "SIEMENS": "SIEMENS.NS", "BEL": "BEL.NS", "BHEL": "BHEL.NS",
    # --- Retail / Paints / Misc ---
    "ASIANPAINT": "ASIANPAINT.NS", "BERGEPAINT": "BERGEPAINT.NS", "TITAN": "TITAN.NS",
    "TRENT": "TRENT.NS", "DMART": "DMART.NS", "NYKAA": "NYKAA.NS",
    "ADANIENT": "ADANIENT.NS", "ADANIPORTS": "ADANIPORTS.NS", "ADANIPOWER": "ADANIPOWER.NS",
    "JSWENERGY": "JSWENERGY.NS", "TATAPOWER": "TATAPOWER.NS", "IDEATECH": "IDEATECH.NS",
    # --- Chemicals / Fertilizers ---
    "UPL": "UPL.NS", "PIIND": "PIIND.NS", "SRF": "SRF.NS", "TATACHEM": "TATACHEM.NS",
    "COROMANDEL": "COROMANDEL.NS", "CHAMBLFERT": "CHAMBLFERT.NS", "GNFC": "GNFC.NS",
    # --- Others ---
    "DIXON": "DIXON.NS", "AMBER": "AMBER.NS", "POLYCAB": "POLYCAB.NS",
    "HAVELLS": "HAVELLS.NS", "VOLTAS": "VOLTAS.NS", "BLUESTAR": "BLUESTAR.NS",
    "PAGEIND": "PAGEIND.NS", "BATAINDIA": "BATAINDIA.NS", "WHIRLPOOL": "WHIRLPOOL.NS",
    "INDIGO": "INDIGO.NS", "JBL": "JBL.NS", "CONCOR": "CONCOR.NS",
    "AARTIIND": "AARTIIND.NS", "ATUL": "ATUL.NS", "DEEPAKNTR": "DEEPAKNTR.NS",
    "ALKEM": "ALKEM.NS", "GLENMARK": "GLENMARK.NS", "IPCAIND": "IPCAIND.NS",
    "RAMCOCEM": "RAMCOCEM.NS", "INDUSTOWER": "INDUSTOWER.NS", "GMRINFRA": "GMRINFRA.NS",
    "JPASSOCIAT": "JPASSOCIAT.NS", "IRCTC": "IRCTC.NS", "INDHOTEL": "INDHOTEL.NS",
    "JINDALPOLY": "JINDALPOLY.NS", "MAZDOCK": "MAZDOCK.NS", "RBLBANK": "RBLBANK.NS",
    "BANKINDIA": "BANKINDIA.NS", "IDBI": "IDBI.NS", "MCDOWELL-N": "MCDOWELL-N.NS",
    "PEL": "PEL.NS", "TATAELXSI": "TATAELXSI.NS", "BANDHANBNK": "BANDHANBNK.NS",
}

# MCX commodities expanded for scanner (Silver added)
SCAN_COMMODITY_SYMBOLS = {
    "CRUDEOIL": "CL=F", "NATURALGAS": "NG=F",
    "GOLD": "GC=F", "SILVER": "SI=F",
}


def scan_universe() -> dict:
    """Flat {symbol: ticker} map of the full 150+ scan universe (NSE + MCX)."""
    out = dict(SCAN_STOCK_SYMBOLS)
    out.update(SCAN_COMMODITY_SYMBOLS)
    out.update(INDEX_SYMBOLS)
    return out


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
