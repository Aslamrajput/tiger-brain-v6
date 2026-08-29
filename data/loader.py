"""
Tiger Brain V6+V7 — Data Loader (Free Sources)
=================================================
Ye module free data sources se historical + cross-asset data fetch karta hai:
  1. Yahoo Finance (yfinance) — cross-asset data (SGX/GIFT Nifty proxy, US
     market, crude, USD-INR) + India VIX
  2. NSE Bhavcopy — daily options OI/volume (end-of-day only)

⚠️ IMPORTANT LIMITATIONS (ye jhooth nahi bolna hai):
  - Yahan sirf DAILY-level data milega. Intraday OI/IV history free mein
    kahin nahi milti — jaisa humne discuss kiya, uske liye paid provider
    (TrueData/Global Datafeeds) chahiye hoga.
  - NSE Bhavcopy se seedha IV nahi milta — options price se IV calculate
    karni padegi (Black-Scholes reverse). Ye is file mein abhi nahi hai,
    ek alag `iv_calculator.py` module mein karenge jab options pricing
    logic banayenge.
  - NSE website scraping-friendly nahi hai — headers/session handling
    zaroori hai, aur website structure kabhi bhi change ho sakti hai.
    Agar ye function fail ho, sabse pehle check karo ki NSE ne URL/format
    to nahi badla.
  - Ye sandbox environment mein internet access disabled hai, isliye ye
    code yahan test NAHI hua hai. Apne server pe pehli baar chalane ke
    baad zaroor manually verify karna ki data sahi aa raha hai.
"""

import io
import logging
from datetime import datetime, timedelta

import pandas as pd
import requests

logger = logging.getLogger("tiger_brain.data_loader")
logging.basicConfig(level=logging.INFO)


# ============================================================
# 1. CROSS-ASSET DATA (Yahoo Finance) — Section 10, 32
# ============================================================

# Yahoo Finance tickers jo hume chahiye (Section 10 ke "Wake-Up Sequence" ke liye)
CROSS_ASSET_TICKERS = {
    "us_market": "^GSPC",        # S&P 500 (US market cue)
    "us_market_nasdaq": "^IXIC", # Nasdaq (extra confirmation)
    "crude_oil": "CL=F",         # WTI Crude futures
    "usd_inr": "USDINR=X",       # USD-INR exchange rate
    "india_vix": "^INDIAVIX",    # India VIX
    "nifty_spot": "^NSEI",       # Nifty 50 spot (for cross-check with SGX/GIFT proxy)
}
# NOTE: SGX Nifty officially band ho chuka hai (GIFT Nifty ne replace kiya).
# GIFT Nifty ka direct free ticker Yahoo pe reliably available nahi hai —
# iske liye NSE IX (India International Exchange) ka data source dhundhna
# padega, ya broker API se real-time lena hoga. Abhi ke liye US market +
# crude + USD-INR se hi "overnight sentiment" proxy banayenge.


def fetch_cross_asset_history(days_back: int = 400) -> dict:
    """
    Yahoo Finance se cross-asset historical daily data fetch karta hai.

    Returns:
        dict of {name: pandas.DataFrame} — har asset ka OHLCV dataframe
        Agar koi ticker fail ho jaye, us key ki value None hogi (poora
        function crash nahi hoga — ek asset fail hone se baaki data loss
        nahi hona chahiye).
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.error(
            "yfinance install nahi hai. Chalao: pip install yfinance"
        )
        raise

    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)

    results = {}
    for name, ticker in CROSS_ASSET_TICKERS.items():
        try:
            df = yf.download(
                ticker,
                start=start_date.strftime("%Y-%m-%d"),
                end=end_date.strftime("%Y-%m-%d"),
                progress=False,
            )
            if df.empty:
                logger.warning(f"'{name}' ({ticker}) ke liye khali data aaya.")
                results[name] = None
            else:
                results[name] = df
                logger.info(f"'{name}' ({ticker}): {len(df)} din ka data mila.")
        except Exception as exc:
            logger.error(f"'{name}' ({ticker}) fetch karne mein error: {exc}")
            results[name] = None

    return results


def get_latest_overnight_snapshot() -> dict:
    """
    Section 32 ke "Pre-Market Wake-Up" step ke liye — sirf latest values
    chahiye (poori history nahi), taaki roz subah quick check ho sake.

    Returns:
        dict — har asset ka latest close price + previous close se %change
    """
    data = fetch_cross_asset_history(days_back=10)
    snapshot = {}

    for name, df in data.items():
        if df is None or len(df) < 2:
            snapshot[name] = {"latest": None, "pct_change": None}
            continue

        latest_close = float(df["Close"].iloc[-1])
        prev_close = float(df["Close"].iloc[-2])
        pct_change = ((latest_close - prev_close) / prev_close) * 100

        snapshot[name] = {
            "latest": round(latest_close, 4),
            "pct_change": round(pct_change, 2),
        }

    return snapshot


# ============================================================
# 2. INDIA VIX HISTORICAL — Section 17 (Regime Classification)
# ============================================================

def fetch_india_vix_history(days_back: int = 400) -> pd.DataFrame:
    """
    India VIX ka historical daily data — Regime Classification (High Vol /
    Low Vol / Vol Shock, Section 17) ke liye zaroori.

    Yahoo Finance ka ^INDIAVIX ticker use karta hai. Agar ye kabhi fail ho,
    backup source: niftyindices.com ya nseindia.com se CSV download.
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance install nahi hai. Chalao: pip install yfinance")
        raise

    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)

    df = yf.download(
        "^INDIAVIX",
        start=start_date.strftime("%Y-%m-%d"),
        end=end_date.strftime("%Y-%m-%d"),
        progress=False,
    )

    if df.empty:
        logger.warning(
            "India VIX data khali aaya. Yahoo ka ticker fail ho sakta hai — "
            "niftyindices.com se manual CSV download backup plan hai."
        )

    return df


# ============================================================
# 3. NSE BHAVCOPY — Daily Options OI/Volume (Section 19, 24, 28)
# ============================================================

NSE_BHAVCOPY_BASE_URL = (
    "https://archives.nseindia.com/content/historical/DERIVATIVES/"
    "{year}/{month}/fo{day}{month_str}{year}bhav.csv.zip"
)

MONTH_ABBR = {
    1: "JAN", 2: "FEB", 3: "MAR", 4: "APR", 5: "MAY", 6: "JUN",
    7: "JUL", 8: "AUG", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC",
}

# NSE requests block karta hai agar proper browser-jaisa header na ho
NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def fetch_nse_bhavcopy(trade_date: datetime) -> pd.DataFrame | None:
    """
    Ek specific trading day ka NSE F&O Bhavcopy fetch karta hai — options ka
    daily OI, volume, OHLC price milta hai (end-of-day snapshot).

    ⚠️ Ye function INTERNET ACCESS chahega jo is sandbox mein disabled hai —
    isliye ye untested hai. Apne server pe pehli baar chalane ke baad
    zaroor verify karna.

    Args:
        trade_date: kis din ka data chahiye (weekday hona chahiye, market
                    holiday pe file nahi milegi)

    Returns:
        DataFrame with columns like SYMBOL, EXPIRY_DT, STRIKE_PR, OPTION_TYP,
        OPEN, HIGH, LOW, CLOSE, SETTLE_PR, CONTRACTS, VAL_INLAKH, OPEN_INT,
        CHG_IN_OI — ya None agar fetch fail ho (holiday/weekend/network issue)
    """
    year = trade_date.strftime("%Y")
    month = trade_date.strftime("%m")
    day = trade_date.strftime("%d")
    month_str = MONTH_ABBR[trade_date.month]

    url = NSE_BHAVCOPY_BASE_URL.format(
        year=year, month=month, day=day, month_str=month_str
    )

    try:
        response = requests.get(url, headers=NSE_HEADERS, timeout=15)
        response.raise_for_status()

        import zipfile
        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            csv_filename = z.namelist()[0]
            with z.open(csv_filename) as f:
                df = pd.read_csv(f)

        logger.info(f"Bhavcopy {trade_date.date()}: {len(df)} rows mile.")
        return df

    except requests.exceptions.HTTPError:
        logger.warning(
            f"Bhavcopy {trade_date.date()} nahi mili — shayad holiday/weekend "
            "hai, ya NSE ne URL format badal diya hai."
        )
        return None
    except Exception as exc:
        logger.error(f"Bhavcopy {trade_date.date()} fetch mein error: {exc}")
        return None


def fetch_bhavcopy_range(start_date: datetime, end_date: datetime) -> pd.DataFrame:
    """
    Ek date range ke saare trading-day Bhavcopy files fetch karke ek hi
    combined DataFrame banata hai. Backtest (Phase 2) ke liye historical
    OI/volume dataset banane ka main entry point.

    Weekends automatically skip ho jaate hain. Market holidays (jinke liye
    file nahi milegi) bhi silently skip ho jaate hain — warning log hoti hai.
    """
    all_data = []
    current = start_date

    while current <= end_date:
        if current.weekday() < 5:  # 0=Mon .. 4=Fri, weekend skip
            df = fetch_nse_bhavcopy(current)
            if df is not None:
                df["TRADE_DATE"] = current.date()
                all_data.append(df)
        current += timedelta(days=1)

    if not all_data:
        logger.warning("Is date range mein koi bhavcopy data nahi mila.")
        return pd.DataFrame()

    combined = pd.concat(all_data, ignore_index=True)
    logger.info(
        f"Total {len(all_data)} trading din ka data combine hua, "
        f"{len(combined)} total rows."
    )
    return combined


# ============================================================
# 4. ANGEL ONE — REAL HISTORICAL + LIVE DATA (Section 19, 24, 28)
# ============================================================
# Ye Angel One ke asli account se REAL data laata hai (free NSE
# sources ke bajaye) — intraday bhi milega, jo free sources se
# nahi milta tha.
#
# ⚠️ Ye functions ek already-logged-in `AngelBroker` instance
# (broker/angel_connect.py se) maangte hain — pehle broker.login()
# call karna zaroori hai.

# Angel One ka public instrument-master file — isme har symbol
# (NIFTY, BANKNIFTY, options strikes, etc.) ka unique "token" hota
# hai jo API calls ke liye zaroori hai.
ANGEL_INSTRUMENT_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/"
    "OpenAPIScripMaster.json"
)

_instrument_master_cache = None  # ek baar download hone ke baad memory mein rakhte hain


def load_angel_instrument_master(force_refresh: bool = False) -> pd.DataFrame:
    """
    Angel One ka poora instrument list download karta hai (hazaaron
    symbols, saare tokens ke saath). Ye file rozana thodi badalti hai
    (naye expiries add hote hain), isliye din mein ek baar refresh
    karna sahi practice hai.
    """
    global _instrument_master_cache

    if _instrument_master_cache is not None and not force_refresh:
        return _instrument_master_cache

    try:
        response = requests.get(ANGEL_INSTRUMENT_MASTER_URL, timeout=30)
        response.raise_for_status()
        data = response.json()
        df = pd.DataFrame(data)
        _instrument_master_cache = df
        logger.info(f"Angel instrument master load hua: {len(df)} instruments.")
        return df
    except Exception as exc:
        logger.error(f"Instrument master fetch mein error: {exc}")
        raise


def find_symbol_token(exchange: str, search_text: str) -> pd.DataFrame:
    """
    Symbol ka token dhundta hai naam se search karke.

    Args:
        exchange: 'NSE' (index/stock spot) ya 'NFO' (futures/options)
        search_text: jaise 'NIFTY' ya 'BANKNIFTY' ya kisi option ka
                     naam (jaise 'NIFTY28AUG25000CE')

    Returns:
        Matching rows ka DataFrame — caller ko isme se exact match
        chunna hai (kai symbols match ho sakte hain, jaise saari
        expiries ek naam se).
    """
    df = load_angel_instrument_master()
    filtered = df[df["exch_seg"] == exchange]
    mask = filtered["symbol"].str.contains(search_text, case=False, na=False)
    return filtered[mask]


# Section 18-25 documentation ke hisaab se Angel One ke interval limits
ANGEL_INTERVAL_MAX_DAYS = {
    "ONE_MINUTE": 30, "THREE_MINUTE": 60, "FIVE_MINUTE": 100,
    "TEN_MINUTE": 100, "FIFTEEN_MINUTE": 200, "THIRTY_MINUTE": 200,
    "ONE_HOUR": 400, "ONE_DAY": 2000,
}


def fetch_angel_historical_candles(
    broker, exchange: str, symboltoken: str, interval: str,
    from_date: datetime, to_date: datetime,
) -> pd.DataFrame:
    """
    Angel One se real historical OHLCV candles fetch karta hai —
    Phase 2 backtest ke liye ye MAIN function hoga.

    Interval limits (Angel One ke rules) automatically handle hoti
    hain — agar tumne 2 saal ka 1-minute data manga, ye khud usko
    30-30 din ke chunks mein todke saari requests karega aur combine
    karke ek DataFrame dega.

    Args:
        broker: AngelBroker instance jisme login() already ho chuka ho
        exchange: 'NSE' ya 'NFO'
        symboltoken: instrument ka token (find_symbol_token se milega)
        interval: 'ONE_MINUTE', 'FIVE_MINUTE', 'ONE_DAY', etc.
        from_date, to_date: datetime objects

    Returns:
        DataFrame with index=timestamp, columns=[open, high, low, close, volume]
        Khali DataFrame agar kuch na mile.
    """
    if broker.smart_api is None:
        raise RuntimeError(
            "Broker login nahi hua hai — pehle broker.login() call karo."
        )

    max_days = ANGEL_INTERVAL_MAX_DAYS.get(interval, 30)
    all_candles = []

    chunk_start = from_date
    while chunk_start <= to_date:
        chunk_end = min(chunk_start + timedelta(days=max_days - 1), to_date)

        params = {
            "exchange": exchange,
            "symboltoken": symboltoken,
            "interval": interval,
            "fromdate": chunk_start.strftime("%Y-%m-%d %H:%M"),
            "todate": chunk_end.strftime("%Y-%m-%d %H:%M"),
        }

        try:
            response = broker.smart_api.getCandleData(params)
            if response.get("status") and response.get("data"):
                all_candles.extend(response["data"])
                logger.info(
                    f"Candles mile: {chunk_start.date()} se {chunk_end.date()} "
                    f"({len(response['data'])} rows)"
                )
            else:
                logger.warning(
                    f"Candle chunk {chunk_start.date()}-{chunk_end.date()} "
                    f"khali/fail: {response.get('message', 'unknown')}"
                )
        except Exception as exc:
            logger.error(
                f"Candle fetch error {chunk_start.date()}-{chunk_end.date()}: {exc}"
            )

        chunk_start = chunk_end + timedelta(days=1)

    if not all_candles:
        logger.warning("Koi candle data nahi mila poore range mein.")
        return pd.DataFrame()

    df = pd.DataFrame(
        all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.set_index("timestamp")
    return df


def fetch_angel_ltp(broker, exchange: str, tradingsymbol: str, symboltoken: str) -> dict | None:
    """
    Live/Last-Traded-Price snapshot — Market Feeds ke liye. Intraday
    scanning (Section 7, 28 — Dynamic Universe Selection) ke waqt ye
    use hoga real-time price/volume check karne ke liye.

    Returns:
        dict with price/OI info, ya None agar fetch fail ho
    """
    if broker.smart_api is None:
        raise RuntimeError("Broker login nahi hua hai — pehle broker.login() call karo.")

    try:
        response = broker.smart_api.ltpData(exchange, tradingsymbol, symboltoken)
        if response.get("status"):
            return response["data"]
        else:
            logger.warning(f"LTP fetch fail: {response.get('message', 'unknown')}")
            return None
    except Exception as exc:
        logger.error(f"LTP fetch error: {exc}")
        return None


# ============================================================
# QUICK MANUAL TEST (isko apne server pe chalao, sandbox mein nahi chalega)
# ============================================================
if __name__ == "__main__":
    print("=== Cross-Asset Snapshot Test ===")
    snapshot = get_latest_overnight_snapshot()
    for name, values in snapshot.items():
        print(f"{name}: {values}")

    print("\n=== India VIX History Test (last 10 rows) ===")
    vix_df = fetch_india_vix_history(days_back=30)
    print(vix_df.tail(10))

    print("\n=== NSE Bhavcopy Test (yesterday) ===")
    yesterday = datetime.now() - timedelta(days=1)
    bhav_df = fetch_nse_bhavcopy(yesterday)
    if bhav_df is not None:
        print(bhav_df.head())
    else:
        print("Bhavcopy nahi mili — upar ka warning check karo.")

    print("\n=== Angel One Real Data Test ===")
    print("⚠️ Ye REAL Angel One login + real API calls karega.")
    try:
        from broker.angel_connect import AngelBroker

        broker = AngelBroker()
        broker.login()
        print("✅ Broker login successful, ab data fetch test karte hain...\n")

        # NIFTY 50 index ka well-known token (NSE) — Angel One
        # documentation mein diya gaya standard token
        nifty_token = "99926000"

        print("--- NIFTY Live LTP Test ---")
        ltp_data = fetch_angel_ltp(broker, "NSE", "NIFTY", nifty_token)
        print(ltp_data)

        print("\n--- NIFTY Historical Daily Candles (last 10 days) ---")
        end = datetime.now()
        start = end - timedelta(days=10)
        candles = fetch_angel_historical_candles(
            broker, "NSE", nifty_token, "ONE_DAY", start, end
        )
        print(candles)

    except Exception as exc:
        print(f"❌ Angel One data test fail hua: {exc}")

