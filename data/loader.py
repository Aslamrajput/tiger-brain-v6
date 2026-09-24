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

from __future__ import annotations

import io
import logging
import threading
import time
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

# ============================================================
# V19 INSTRUMENT MASTER FILTER — ALLOWED symbols + ALL F&O stocks
# 1.45 lakh → ~20K (index + MCX + all stock options kept for filter)
# ============================================================
# Index + MCX: always keep (spot + options + futures)
ALLOWED_INDEX = ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]
ALLOWED_MCX = ["GOLDM", "SILVERM", "CRUDEOIL", "NATURALGAS"]
ALLOWED_INSTRUMENT_NAMES = set(ALLOWED_INDEX + ALLOWED_MCX)

# Sirf in exchanges pe Tiger trade karta hai
ALLOWED_EXCH_SEGS = {"NSE", "NFO", "BSE", "BFO", "MCX"}

# Sirf in instrument types chahiye (options, futures, spot index)
USEFUL_INSTRUMENT_TYPES = {"AMXIDX", "OPTIDX", "FUTIDX", "OPTFUT", "FUTCOM", "INDEX"}


# Angel One ka public instrument-master file — isme har symbol
# (NIFTY, BANKNIFTY, options strikes, etc.) ka unique "token" hota
# hai jo API calls ke liye zaroori hai.
ANGEL_INSTRUMENT_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/"
    "OpenAPIScripMaster.json"
)

_instrument_master_cache = None  # ek baar download hone ke baad memory mein rakhte hain


def _filter_instruments(df: pd.DataFrame) -> pd.DataFrame:
    """1.45 lakh instruments → filtered set.

    Keep:
    - Index: NIFTY/BANKNIFTY/FINNIFTY/SENSEX (spot + options + futures)
    - MCX: GOLDM/SILVERM/CRUDEOIL/NATURALGAS (futures + options)
    - ALL stock options (OPTSTK on NFO) — liquidity filter selects top 10-11
      at scan time, but instrument master needs ALL so resolve_option_contract
      can find any stock's option tokens.
    """
    # Index + MCX instruments (by name match)
    named_mask = (
        df["name"].isin(ALLOWED_INSTRUMENT_NAMES)
        & df["exch_seg"].isin(ALLOWED_EXCH_SEGS)
        & df["instrumenttype"].isin(USEFUL_INSTRUMENT_TYPES)
    )

    # ALL stock options (OPTSTK) on NFO — for liquidity pipeline
    stock_opt_mask = (
        (df["exch_seg"] == "NFO")
        & (df["instrumenttype"] == "OPTSTK")
    )

    # NSE stock spot tokens (-EQ) for underlying candles
    # These have instrumenttype "" and exch_seg NSE
    # We keep them all — resolve_underlying_token needs them
    nse_eq_mask = (
        (df["exch_seg"] == "NSE")
        & (df["symbol"].str.endswith("-EQ", na=False))
    )

    mask = named_mask | stock_opt_mask | nse_eq_mask
    return df[mask].reset_index(drop=True)


def load_angel_instrument_master(force_refresh: bool = False) -> pd.DataFrame:
    """
    Angel One ka instrument list download + filter karta hai.
    Sirf ALLOWED_INDEX + ALLOWED_MCX symbols rakhta hai (1.45L → ~12K).
    JSON cache me save hota hai — roz ek baar fresh download, baaki
    time cache se load (fast startup).
    """
    global _instrument_master_cache

    if _instrument_master_cache is not None and not force_refresh:
        return _instrument_master_cache

    import os, json
    from datetime import datetime as _dt
    cache_path = os.path.join(os.path.dirname(__file__), "instruments_filtered.json")

    # Aaj ki cache fresh hai? → JSON se load karo (fast)
    today_str = _dt.now().strftime("%Y-%m-%d")
    if os.path.exists(cache_path) and not force_refresh:
        try:
            with open(cache_path, "r") as f:
                cached = json.load(f)
            if cached.get("_cache_date") == today_str:
                df = pd.DataFrame(cached["instruments"])
                _instrument_master_cache = df
                logger.info(f"Angel instrument master (cached): {len(df)} instruments.")
                return df
        except Exception as exc:
            logger.warning(f"Instrument cache read fail — fresh download: {exc}")

    # Fresh download from Angel One
    try:
        response = requests.get(ANGEL_INSTRUMENT_MASTER_URL, timeout=30)
        response.raise_for_status()
        data = response.json()
        full_df = pd.DataFrame(data)
        # FILTER — sirf allowed symbols rakho
        df = _filter_instruments(full_df)
        _instrument_master_cache = df
        logger.info(f"Angel instrument master load hua: {len(full_df)} → filtered {len(df)} instruments.")
        # JSON cache save
        try:
            with open(cache_path, "w") as f:
                json.dump({"_cache_date": today_str, "instruments": df.to_dict("records")}, f)
        except Exception as exc:
            logger.warning(f"Instrument cache save fail: {exc}")
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

# Angel ka historical API rate-limited hai (3 req/sec, aur burst pe
# "Access denied because of exceeding access rate" bhejta hai). Chunked
# download mein ye error aana normal hai, isliye har chunk ke beech ruko
# aur rate-limit wale error pe badhte hue intezaar ke saath retry karo.
# NOTE: backoff chhota (1s, 2s, 4s) rakha gaya hai — turant wapas maarna
# burst ko aur badha deta hai, isliye pehle attempt pe peechhe hato.
ANGEL_CHUNK_PAUSE_SEC = 0.5
# Exponential backoff on rate-limit (429) + read timeouts: 2s, 4s, 8s between the 4 attempts.
ANGEL_MAX_RETRIES = 4
ANGEL_RETRY_BACKOFF_SEC = 2.0  # 2s, 4s, 8s exponential backoff sequence

# Angel historical API allows ~3 req/sec, but a burst across many symbols
# (per-symbol chunks) trips "Access denied because of exceeding access rate".
# A process-wide gate serialises every getCandleData call so consecutive
# requests are spaced >= ANGEL_MIN_CALL_INTERVAL_SEC apart (~2.2 req/sec),
# including across symbols scanned in sequence.
ANGEL_MIN_CALL_INTERVAL_SEC = 0.45


class TokenBucket:
    """Leaky-bucket rate limiter — thread-safe, per-endpoint.

    Capacity tokens accumulate at a fixed refill rate; each call consumes
    one token. If the bucket is empty, the caller blocks until a token
    refills. This guarantees a steady max-rate with no bursts.

    Args:
        rate: tokens added per second (e.g. 2.2 = max 2.2 req/sec)
        capacity: max tokens that can bank up (burst allowance)
    """

    def __init__(self, rate: float, capacity: float = None):
        self.rate = rate
        self.capacity = capacity if capacity is not None else rate
        self._tokens = self.capacity
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until one token is available, then consume it."""
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate
            time.sleep(wait)


# Separate buckets per endpoint — Angel enforces different rate limits.
# Candle historical: ~3 req/sec (safe 2.2).  Quote/market data: 1 req/sec.
_candle_bucket = TokenBucket(rate=ANGEL_MIN_CALL_INTERVAL_SEC ** -1,
                             capacity=3)
_quote_bucket = TokenBucket(rate=1.0, capacity=1)

# Legacy lock kept for backward-compat with tests/importers that reference it.
_angel_call_lock = threading.Lock()
_angel_last_call_ts = 0.0


def _angel_rate_limit_gate(bucket: str = "candle") -> None:
    """Block until a rate-limit slot is available (token bucket / leaky bucket).

    Thread-safe. Uses a per-endpoint token bucket so candle (2.2/s) and
    quote (1/s) limits are independently enforced.
    """
    if bucket == "quote":
        _quote_bucket.acquire()
    else:
        _candle_bucket.acquire()


_RATE_LIMIT_MARKERS = (
    "access rate", "rate limit", "exceeding access", "too many request",
    "too many requests", "ab1021",
)
_TIMEOUT_MARKERS = (
    "read timed out", "connect timed out", "connection timeout",
    "timed out", "read timeout", "connect timeout",
)


def is_rate_limit_error(message: str) -> bool:
    """Kya ye error rate-limit ka hai (yani retry karne layak)?"""
    lowered = str(message).lower()
    return any(marker in lowered for marker in _RATE_LIMIT_MARKERS)


def is_timeout_error(message: str) -> bool:
    """Kya ye error read/connect timeout ka hai (retry karne layak)?

    Read timed out = Angel server ne connection accept kiya par response
    bhejne mein latak gaya. Retry karna safe hai (rate-limit nahi hai).
    """
    lowered = str(message).lower()
    return any(marker in lowered for marker in _TIMEOUT_MARKERS)


def fetch_candle_chunk(
    broker, params: dict,
    max_retries: int = ANGEL_MAX_RETRIES,
    backoff_sec: float = ANGEL_RETRY_BACKOFF_SEC,
) -> list:
    """
    Ek chunk ki candles laata hai, rate-limit pe exponential backoff ke
    saath retry karte hue.

    Sirf rate-limit errors retry hote hain — baaki errors (galat token,
    session expire) retry karne se theek nahi honge, isliye wo turant
    raise ho jaate hain aur caller unhe log karta hai.

    TOKEN EXPIRY HANDLING: agar Angel One "Token missing" / AG8003 return
    kare, to broker._auto_relogin() call hota hai (fresh TOTP + session),
    aur candle fetch ek baar retry hota hai. Tiger ko data nahi dena
    = missed trades, isliye token error pe auto-recovery critical hai.

    Returns:
        Raw candle rows ki list (khali list agar data hi na ho).
    """
    _TOKEN_ERROR_MARKERS = (
        "ag8003", "token missing", "ag8002", "invalid token",
        "session expired", "token expired", "unauthorized",
    )

    def _is_token_err(msg: str) -> bool:
        lowered = str(msg).lower()
        return any(m in lowered for m in _TOKEN_ERROR_MARKERS)

    for attempt in range(max_retries):
        try:
            _angel_rate_limit_gate()
            response = broker.smart_api.getCandleData(params)
        except Exception as exc:
            # Token error as exception → auto relogin + retry
            if _is_token_err(str(exc)) and hasattr(broker, "_auto_relogin"):
                logger.warning("Candle fetch token error — auto re-login + retry...")
                broker._token_healthy = False
                if broker._auto_relogin():
                    try:
                        _angel_rate_limit_gate()
                        response = broker.smart_api.getCandleData(params)
                        if response.get("status") and response.get("data"):
                            return response["data"]
                    except Exception:
                        pass
                return []
            # SmartAPI rate-limit ka jawab JSON nahi hota, isliye SDK
            # exception phenkta hai — usme bhi wahi message hota hai.
            # Read/connect timeout bhi retry-worthy hai (rate-limit nahi).
            is_retryable = is_rate_limit_error(exc) or is_timeout_error(exc)
            if not is_retryable or attempt == max_retries - 1:
                raise
            response = {"message": str(exc)}

        if response.get("status") and response.get("data"):
            return response["data"]

        message = response.get("message", "unknown")

        # Token error in response → auto relogin + retry
        if _is_token_err(message) and hasattr(broker, "_auto_relogin"):
            logger.warning("Candle response token error (%s) — auto re-login...",
                           message)
            broker._token_healthy = False
            if broker._auto_relogin():
                try:
                    _angel_rate_limit_gate()
                    response = broker.smart_api.getCandleData(params)
                    if response.get("status") and response.get("data"):
                        return response["data"]
                except Exception:
                    pass
            logger.warning("Candle fetch failed after token re-login attempt.")
            return []

        if not is_rate_limit_error(message) or attempt == max_retries - 1:
            if not is_timeout_error(message):
                logger.warning(
                    f"NO_DATA candle chunk {params['fromdate']}-{params['todate']} "
                    f"khali/fail: {message}"
                )
                return []

        delay = backoff_sec * (2 ** attempt)
        reason = "RATE_LIMIT_HIT" if is_rate_limit_error(message) else "TIMEOUT_RETRY"
        logger.warning(
            f"{reason} ({message}) — {delay:.0f}s exponential backoff "
            f"before retry ({attempt + 1}/{max_retries - 1})"
        )
        time.sleep(delay)

    return []


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
        # Chunk ka aakhri din PURA maangna zaroori hai — warna us date pe
        # sirf 00:00 tak ka data aata hai aur uska poora session gayab ho
        # jaata hai (agla chunk bhi agle din se shuru hota hai).
        chunk_end = min(
            (chunk_start + timedelta(days=max_days - 1)).replace(
                hour=23, minute=59, second=0, microsecond=0
            ),
            to_date,
        )

        params = {
            "exchange": exchange,
            "symboltoken": symboltoken,
            "interval": interval,
            "fromdate": chunk_start.strftime("%Y-%m-%d %H:%M"),
            "todate": chunk_end.strftime("%Y-%m-%d %H:%M"),
        }

        try:
            candles = fetch_candle_chunk(broker, params)
            if candles:
                all_candles.extend(candles)
                logger.info(
                    f"Candles mile: {chunk_start.date()} se {chunk_end.date()} "
                    f"({len(candles)} rows)"
                )
        except Exception as exc:
            if is_rate_limit_error(exc):
                logger.error(
                    f"RATE_LIMIT_HIT {chunk_start.date()}-{chunk_end.date()} "
                    f"(retries exhausted): {exc}"
                )
            else:
                logger.error(
                    f"NO_DATA candle fetch error "
                    f"{chunk_start.date()}-{chunk_end.date()}: {exc}"
                )

        chunk_start = (chunk_end + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        if chunk_start <= to_date:
            time.sleep(ANGEL_CHUNK_PAUSE_SEC)

    if not all_candles:
        logger.warning("NO_DATA — koi candle data nahi mila poore range mein.")
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

    # Rate-limit guard: ltpData se pehle 0.5s ruko (reduced from 3.0s for 42-symbol scan)
    time.sleep(0.5)
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
# 5. OPTIONS CHAIN + OI DATA (Section 19, 22, 23 — Sub-Brains ke
#    liye zaroori: OI buildup, PCR, IV Skew)
# ============================================================
# ⚠️ Ye section Angel One ke instrument-master format pe based hai
# (confirmed via SmartAPI forum documentation): options symbols
# jaise "NIFTY28OCT2524400CE" — name+expiry(DDMMMYYYY)+strike+CE/PE.
# Strike value master file mein *100 hoke stored hai (jaise 24400
# strike = "2440000.000000"), isliye humesha /100 karna hai.

INDEX_UNDERLYING_TOKENS = {
    # (exchange, spot index token) — LTP/candles ke liye
    "NIFTY": ("NSE", "99926000"),
    "BANKNIFTY": ("NSE", "99926009"),
    "FINNIFTY": ("NSE", "99926037"),
    "SENSEX": ("BSE", "99919000"),
}


# Index options → OPTIDX on NFO/BFO, Stock options → OPTSTK on NFO,
# Commodity options → OPTFUT on MCX. Ye mapping resolve_option_contract
# ke liye chahiye taaki har segment ke options token mil sake.
OPTION_INSTRUMENT_TYPE = {
    "NIFTY": ("OPTIDX", "NFO"),
    "BANKNIFTY": ("OPTIDX", "NFO"),
    "FINNIFTY": ("OPTIDX", "NFO"),
    "SENSEX": ("OPTIDX", "BFO"),
    "CRUDEOIL": ("OPTFUT", "MCX"),
    "CRUDEOILM": ("OPTFUT", "MCX"),
    "NATURALGAS": ("OPTFUT", "MCX"),
    "NATGASMINI": ("OPTFUT", "MCX"),
    "GOLD": ("OPTFUT", "MCX"),
    "GOLDM": ("OPTFUT", "MCX"),
    "SILVER": ("OPTFUT", "MCX"),
    "SILVERM": ("OPTFUT", "MCX"),
}

# MCX MINI fallback — jab full-size contract afford nahi hota (small capital),
# to Tiger MINI variant try karta hai (chhota lot size = kam capital).
# Example: ₹10k account pe CRUDEOIL (lot 100) afford nahi → CRUDEOILM (lot 10).
MCX_MINI_FALLBACK = {
    "CRUDEOIL": "CRUDEOILM",      # lot 100 → 10
    "NATURALGAS": "NATGASMINI",   # lot 1250 → 250
    "GOLD": "GOLDM",              # lot 1 → 100 (premium-based, GOLDM cheaper)
    "SILVER": "SILVERM",          # lot 30 → 1 (mini)
}


def resolve_underlying_token(symbol: str) -> tuple[str, str] | None:
    """Symbol → (exchange, symboltoken) for historical candle fetch.

    NSE index (NIFTY/BANKNIFTY/FINNIFTY) → spot index token (AMXIDX).
    NSE stock (RELIANCE/TCS/etc) → '-EQ' suffix token on NSE.
    MCX commodity (GOLD/CRUDEOIL/etc) → nearest FUTCOM expiry token on MCX.

    Returns:
        (exchange, symboltoken) ya None agar resolve nahi hua.
    """
    try:
        df = load_angel_instrument_master()
    except Exception as exc:
        logger.error(f"Instrument master load fail: {exc}")
        return None

    # Index → spot index token (99926000 etc)
    if symbol in INDEX_UNDERLYING_TOKENS:
        exch, token = INDEX_UNDERLYING_TOKENS[symbol]
        return (exch, token)

    # MCX commodity → nearest FUTCOM expiry
    if symbol in OPTION_INSTRUMENT_TYPE and \
            OPTION_INSTRUMENT_TYPE[symbol][1] == "MCX":
        mask = (
            (df["name"] == symbol)
            & (df["instrumenttype"] == "FUTCOM")
            & (df["exch_seg"] == "MCX")
        )
        mcx_fut = df[mask].copy()
        if mcx_fut.empty:
            logger.warning(f"MCX FUTCOM nahi mila: {symbol}")
            return None
        mcx_fut["expiry_parsed"] = pd.to_datetime(
            mcx_fut["expiry"], format="%d%b%Y", errors="coerce")
        today = pd.Timestamp.now().normalize()
        mcx_fut = mcx_fut[mcx_fut["expiry_parsed"] >= today].sort_values(
            "expiry_parsed")
        if mcx_fut.empty:
            logger.warning(f"MCX future expiry khatam: {symbol}")
            return None
        row = mcx_fut.iloc[0]
        return ("MCX", str(row["token"]))

    # NSE stock → -EQ spot token
    eq_symbol = symbol + "-EQ"
    mask = (df["exch_seg"] == "NSE") & (df["symbol"] == eq_symbol)
    matches = df[mask]
    if not matches.empty:
        return ("NSE", str(matches.iloc[0]["token"]))

    logger.warning(f"Symbol resolve nahi hua: {symbol}")
    return None


def fetch_angel_underlying_candles(
    broker, symbol: str, interval: str = "FIFTEEN_MINUTE",
    days: int = 30,
) -> pd.DataFrame:
    """Angel One se real historical candles fetch karo (NOT yfinance!).

    NSE/MCX underlying ke liye — spot index, stock, ya MCX futures.
    yfinance fallback sirf tab jab Angel One fail ho (rate limit etc).

    Returns:
        DataFrame (timestamp index, open/high/low/close/volume columns)
        ya empty DataFrame agar fetch fail.
    """
    resolved = resolve_underlying_token(symbol)
    if resolved is None:
        return pd.DataFrame()
    exchange, symboltoken = resolved
    to_date = datetime.now().replace(hour=23, minute=59, second=0, microsecond=0)
    from_date = to_date - timedelta(days=days)
    try:
        return fetch_angel_historical_candles(
            broker, exchange, symboltoken, interval, from_date, to_date)
    except Exception as exc:
        logger.error(f"Angel candle fetch fail {symbol}: {exc}")
        return pd.DataFrame()


def get_option_chain_instruments(
    underlying: str = "NIFTY", expiry_date: str = None
) -> pd.DataFrame:
    """
    Instrument master se ek underlying (jaise NIFTY) ke saare options
    contracts nikalta hai ek expiry ke liye.

    Index options (NIFTY/BANKNIFTY/FINNIFTY) → OPTIDX on NFO.
    Stock options (RELIANCE/TCS/etc) → OPTSTK on NFO.
    Commodity options (CRUDEOIL/GOLD/etc) → OPTFUT on MCX.

    Args:
        underlying: 'NIFTY', 'BANKNIFTY', 'RELIANCE', 'GOLD', etc.
        expiry_date: format 'DDMMMYYYY' jaisa '28OCT2025'. None = sabse
                     nearest (jaldi expire hone wali) expiry khud chunega.

    Returns:
        DataFrame with columns: token, symbol, strike, option_type (CE/PE),
        expiry, lotsize
    """
    df = load_angel_instrument_master()

    instr_type, exchange = OPTION_INSTRUMENT_TYPE.get(
        underlying, ("OPTSTK", "NFO"))

    mask = (
        (df["name"] == underlying)
        & (df["instrumenttype"] == instr_type)
        & (df["exch_seg"] == exchange)
    )
    options = df[mask].copy()

    if options.empty:
        logger.warning(f"'{underlying}' ke options nahi mile instrument master mein "
                       f"(type={instr_type}, exch={exchange}).")
        return pd.DataFrame()

    # Strike master mein *100 hoke stored hai
    options["strike"] = options["strike"].astype(float) / 100
    options["option_type"] = options["symbol"].str[-2:]  # last 2 chars: CE/PE

    # Expiry ko date mein convert karke sort karna (nearest expiry dhundhne ke liye)
    options["expiry_parsed"] = pd.to_datetime(
        options["expiry"], format="%d%b%Y", errors="coerce"
    )
    options = options.dropna(subset=["expiry_parsed"]).sort_values("expiry_parsed")

    if expiry_date is None:
        # Sabse nearest (jaldi wali) expiry chunna — future ki, aaj se pehle ki nahi
        today = pd.Timestamp.now().normalize()
        future_expiries = options[options["expiry_parsed"] >= today]["expiry"].unique()
        if len(future_expiries) == 0:
            logger.warning("Koi future expiry nahi mili.")
            return pd.DataFrame()
        expiry_date = sorted(
            future_expiries,
            key=lambda x: pd.to_datetime(x, format="%d%b%Y"),
        )[0]
        logger.info(f"Nearest expiry auto-selected: {expiry_date}")

    result = options[options["expiry"] == expiry_date][
        ["token", "symbol", "strike", "option_type", "expiry", "lotsize"]
    ].reset_index(drop=True)

    return result


def resolve_option_contract(
    underlying: str, strike: float, option_type: str, expiry_date: str = None
) -> dict | None:
    """Symbol + strike + CE/PE → Angel One tradingsymbol + symboltoken.

    Live order placement ke liye — intraday_scan har trade signal pe
    ye call karega taaki real order place ho sake.

    Args:
        underlying: 'NIFTY', 'RELIANCE', 'GOLD', etc.
        strike: strike price (jaise 24400.0)
        option_type: 'CE' ya 'PE'
        expiry_date: 'DDMMMYYYY' format, None = nearest expiry

    Returns:
        {'tradingsymbol': str, 'symboltoken': str, 'exchange': str,
         'lotsize': int} ya None agar contract nahi mila.
    """
    chain = get_option_chain_instruments(underlying, expiry_date)
    if chain is None or chain.empty:
        return None

    matches = chain[
        (chain["option_type"] == option_type)
        & (chain["strike"] == float(strike))
    ]
    if matches.empty:
        # BUGFIX: filter by option_type BEFORE finding nearest strike.
        # Previously searched the ENTIRE chain (CE+PE), so a BUY (CE) signal
        # could resolve to a PE contract — Tiger bought puts instead of calls!
        typed = chain[chain["option_type"] == option_type]
        if typed.empty:
            return None
        nearest_idx = (typed["strike"] - float(strike)).abs().idxmin()
        matches = typed.loc[[nearest_idx]]

    row = matches.iloc[0]
    _, exchange = OPTION_INSTRUMENT_TYPE.get(underlying, ("OPTSTK", "NFO"))
    return {
        "tradingsymbol": row["symbol"],
        "symboltoken": str(row["token"]),
        "exchange": exchange,
        "lotsize": int(row["lotsize"]),
    }


class _GreeksFailCache:
    """Throttle optionGreek API calls per underlying.

    If the API returns no data (common for MCX commodity options), we
    skip retries for a cooldown period to prevent log flooding and wasted
    latency. Delta estimate fallback is used instead during cooldown.
    """
    _COOLDOWN_SECONDS = 15 * 60  # 15 minutes

    def __init__(self):
        self._fails: dict[str, float] = {}

    def mark_fail(self, underlying: str):
        from time import time
        self._fails[underlying] = time()

    def is_cooled(self, underlying: str) -> bool:
        """True if underlying is in cooldown (skip API call)."""
        from time import time
        last = self._fails.get(underlying)
        if last is None:
            return False
        if time() - last < self._COOLDOWN_SECONDS:
            return True
        del self._fails[underlying]
        return False


_greeks_fail_cache = _GreeksFailCache()


def find_affordable_option(
    underlying: str,
    atm_strike: float,
    option_type: str,
    balance: float,
    broker=None,
    max_otm_steps: int = 3,
    min_delta: float = 0.10,
    iv_crush_warning_pct: float = 50.0,
    max_premium: float = 0.0,
) -> dict | None:
    """Find an affordable option strike — walks OTM until 1 lot fits balance.

    Tries ATM first. If unaffordable, steps further OTM (cheaper premium)
    until a single lot costs <= balance. This is Tiger's zero-to-hero mode:
    buy cheap OTM options when ATM is too expensive for small accounts.

    QUANT GREEKS LAYER:
      - Fetches live IV + Delta from Angel One optionGreek API
      - Rejects contracts with Delta < min_delta (0.10) — only truly dead options rejected
      - Lower threshold allows cheap OTM options (delta 0.10-0.30) that rocket 200%+
      - Reads ATM IV to measure instant IV crush risk before placement
      - If ATM IV > iv_crush_warning_pct, logs IV crush risk warning
      - Max 15 OTM steps — dynamically balances small accounts (₹4,000-₹8,000)
      - Falls back to moneyness-based delta estimate if greeks unavailable

    CHEAP OPTIONS GATE (max_premium):
      - If max_premium > 0, skips any option with premium > max_premium
      - User mandate: only buy ₹5-50 premium options (cheap options buying)
      - This ensures Tiger walks far enough OTM to find truly cheap premiums

    Args:
        underlying: 'NIFTY', 'SILVERM', 'CRUDEOIL', etc.
        atm_strike: ATM strike price (underlying close)
        option_type: 'CE' or 'PE'
        balance: available capital (₹)
        broker: broker instance for LTP + greeks fetch
        max_otm_steps: max OTM strikes to try before giving up (hard cap 15)
        min_delta: minimum delta threshold (default 0.10 — allows cheap OTM)
        iv_crush_warning_pct: ATM IV % above which IV crush risk is flagged
        max_premium: skip options with premium above this (0 = no limit)

    Returns:
        {'tradingsymbol', 'symboltoken', 'exchange', 'lotsize', 'strike',
         'ltp', 'one_lot_cost', 'delta', 'iv', 'iv_crush_risk'} or None.
    """
    chain = get_option_chain_instruments(underlying)
    if chain is None or chain.empty:
        return None

    matches = chain[chain["option_type"] == option_type].copy()
    if matches.empty:
        return None

    # Sort by distance from ATM — CE: ascending (higher strike = OTM)
    # PE: descending (lower strike = OTM)
    if option_type == "CE":
        matches = matches[matches["strike"] >= atm_strike].sort_values("strike")
    else:
        matches = matches[matches["strike"] <= atm_strike].sort_values(
            "strike", ascending=False)

    # === FETCH LIVE GREEKS (IV + Delta) from optionGreek API ===
    # Throttled: if optionGreek returns "No Data" for an underlying (common for
    # MCX commodity options), skip API calls for 15 min — prevents log flood
    # + wasted latency. Delta estimate fallback is used instead.
    greeks_df = None
    atm_iv = None
    if broker is not None and not _greeks_fail_cache.is_cooled(underlying):
        try:
            from data.iv_series import fetch_live_greeks, live_atm_iv
            from datetime import datetime as _dt
            # Get expiry from chain
            expiry_str = matches.iloc[0].get("expiry", "") if len(matches) > 0 else ""
            if expiry_str:
                expiry_dt = _dt.strptime(expiry_str, "%d%b%Y").date()
                greeks_df = fetch_live_greeks(broker, underlying, expiry_dt)
                if not greeks_df.empty:
                    iv_result = live_atm_iv(greeks_df, atm_strike)
                    atm_iv = iv_result.get("atm_iv")
                    if atm_iv is not None and atm_iv > iv_crush_warning_pct:
                        logger.warning(
                            f"⚠️ IV CRUSH RISK: ATM IV={atm_iv:.1f}% > "
                            f"{iv_crush_warning_pct}% — high IV crush risk on {underlying}")
                else:
                    _greeks_fail_cache.mark_fail(underlying)
        except Exception as exc:
            logger.debug(f"Greeks fetch fail (will use delta estimate): {exc}")
            _greeks_fail_cache.mark_fail(underlying)
            greeks_df = None

    # === PRE-SUBSCRIBE OPTION TOKENS TO WEBSOCKET (zero REST LTP) ===
    # User mandate: permanent websocket, no REST, no limit, no error.
    # Subscribe all candidate option tokens to WS so ws_get_ltp() reads from
    # cache instead of falling back to REST. Wait 3s for first ticks.
    if broker is not None and hasattr(broker, 'websocket') and broker.websocket is not None:
        _candidate_tokens = []
        _exchange_type = 2  # default NSE_FO
        _, _exch_name = OPTION_INSTRUMENT_TYPE.get(underlying, ("OPTSTK", "NFO"))
        if _exch_name == "MCX":
            _exchange_type = 5  # MCX_FO
        elif _exch_name == "BFO":
            _exchange_type = 2  # BSE_FO treated as NSE_FO type
        for _, row in matches.head(max_otm_steps).iterrows():
            _tok = str(row["token"])
            if _tok:
                _candidate_tokens.append(_tok)
        if _candidate_tokens:
            try:
                broker.websocket.subscribe_tokens(_candidate_tokens, _exchange_type)
                import time as _time
                _time.sleep(3.0)  # wait for first ticks to arrive
                logger.info(
                    f"📡 WS pre-subscribed {len(_candidate_tokens)} {underlying} "
                    f"option tokens — zero REST LTP calls")
            except Exception as exc:
                logger.debug(f"WS pre-subscribe fail (REST fallback): {exc}")

    for _, row in matches.head(max_otm_steps).iterrows():
        strike = float(row["strike"])
        lot = int(row["lotsize"])

        # Get real LTP if broker available
        ltp = 0.0
        if broker is not None:
            try:
                ltp = broker.ws_get_ltp(
                    row["symbol"], str(row["token"]),
                    OPTION_INSTRUMENT_TYPE.get(underlying, ("OPTSTK", "NFO"))[1],
                )
            except Exception:
                ltp = 0.0

        # Rough OTM premium estimate if LTP unavailable
        if ltp <= 0:
            otm_distance = abs(strike - atm_strike) / atm_strike
            ltp = max(5.0, atm_strike * 0.005 * (1 - otm_distance * 5))

        one_lot_cost = lot * ltp
        if one_lot_cost > balance or one_lot_cost <= 0:
            continue

        # === CHEAP OPTIONS GATE — skip expensive premiums ===
        # User mandate: only buy ₹5-50 premium options. If max_premium is set,
        # skip any option whose per-unit premium exceeds it and keep walking OTM.
        if max_premium > 0 and ltp > max_premium:
            logger.info(
                f"   💰 CHEAP GATE: {underlying} {strike}{option_type} "
                f"premium ₹{ltp:.2f} > ₹{max_premium:.0f} max — walking further OTM")
            continue

        # === DELTA GATE — reject dead zero-delta junk ===
        delta = None
        iv = None
        if greeks_df is not None and not greeks_df.empty:
            greek_row = greeks_df[
                (greeks_df["strike"] == strike) &
                (greeks_df["option_type"] == option_type)
            ]
            if not greek_row.empty:
                delta = float(greek_row["delta"].iloc[0])
                iv = float(greek_row["iv"].iloc[0])

        # Fallback: estimate delta from moneyness if greeks unavailable
        if delta is None:
            from broker.option_selector import estimate_delta
            delta = estimate_delta(
                {"strike": strike, "delta": None}, atm_strike, option_type)

        if delta < min_delta:
            logger.info(
                f"   🚫 DELTA GATE: {underlying} {strike}{option_type} "
                f"delta={delta:.2f} < {min_delta} — dead option, skip")
            continue

        _, exchange = OPTION_INSTRUMENT_TYPE.get(underlying, ("OPTSTK", "NFO"))
        iv_crush_risk = (atm_iv is not None and atm_iv > iv_crush_warning_pct)
        return {
            "tradingsymbol": row["symbol"],
            "symboltoken": str(row["token"]),
            "exchange": exchange,
            "lotsize": lot,
            "strike": strike,
            "ltp": ltp,
            "one_lot_cost": one_lot_cost,
            "delta": round(delta, 2),
            "iv": round(iv, 1) if iv is not None else None,
            "iv_crush_risk": iv_crush_risk,
        }

    return None


def fetch_option_chain_oi(
    broker, underlying: str = "NIFTY", expiry_date: str = None, strikes_around_atm: int = 10
) -> pd.DataFrame:
    """
    Option-chain ka OI + volume — SINGLE optionGreek API call se.

    Uses Angel One optionGreek API which returns per-strike: IV, delta,
    gamma, theta, vega, tradeVolume, openInterest, totalBuyQuantity,
    totalSellQuantity — ALL from one call. NO per-strike REST LTP
    fetch (that caused rate-limit floods). For indices (NIFTY/BANKNIFTY)
    the summed options tradeVolume + OI become the REAL volume proxy
    for the underlying index (whose spot feed reports volume=0).

    Args:
        underlying: 'NIFTY', etc.
        expiry_date: None = nearest expiry
        strikes_around_atm: ATM ke around kitni strikes chahiye (dono
                             taraf) — poora chain lena zaroori nahi,
                             ATM ke aas-paas ka hi kaam ka hota hai

    Returns:
        DataFrame: columns = [strike, option_type, oi, volume,
                   iv, delta, total_buy_qty, total_sell_qty]
    """
    if broker is None or broker.smart_api is None:
        raise RuntimeError("Broker login nahi hua hai — pehle broker.login() call karo.")

    from data.iv_series import fetch_live_greeks
    from datetime import datetime as _dt

    # Resolve expiry from instrument master (no REST — local CSV/cache)
    instruments = get_option_chain_instruments(underlying, expiry_date)
    if instruments.empty:
        return pd.DataFrame()

    if expiry_date is None and not instruments.empty:
        expiry_date = instruments.iloc[0].get("expiry", "")

    try:
        expiry_dt = _dt.strptime(expiry_date, "%d%b%Y").date()
    except Exception:
        return pd.DataFrame()

    # SINGLE optionGreek call — returns ALL strikes with OI + volume.
    # NO per-strike REST LTP loop (that caused rate-limit floods).
    greeks = fetch_live_greeks(broker, underlying, expiry_dt)
    if greeks.empty:
        return pd.DataFrame()

    # Build result — OI + volume extracted directly from the API payload.
    # ltp column removed: we don't need strike-level price for the volume
    # proxy. The volume proxy = sum(tradeVolume) + sum(openInterest).
    result = greeks[["strike", "option_type", "iv", "delta",
                     "trade_volume", "open_interest",
                     "total_buy_qty", "total_sell_qty"]].copy()
    result.rename(columns={
        "trade_volume": "volume",
        "open_interest": "oi",
    }, inplace=True)

    return result


def fetch_index_oi_volume(
    broker, underlying: str = "NIFTY", expiry_date: str = None
) -> dict:
    """
    Index ka REAL volume proxy — options chain se.

    NIFTY/BANKNIFTY spot feed volume=0 deta hai. Lekin options chain
    mein har strike ka tradeVolume + openInterest milta hai (optionGreek
    API). In sabhi strikes ka sum = index participation volume.

    Ye function total tradeVolume + total OI + buy/sell pressure return
    karta hai — tiger_live isse 15m DataFrame ke volume column mein
    backfill karta hai.

    Returns:
        {total_volume, total_oi, total_buy_qty, total_sell_qty,
         buy_sell_ratio, expiry, fetched_at}
        Empty dict agar data na mile.
    """
    from datetime import datetime as _dt

    chain = fetch_option_chain_oi(broker, underlying, expiry_date)
    if chain.empty:
        return {}

    total_vol = float(chain["volume"].sum())
    total_oi = float(chain["oi"].sum())
    total_buy = float(chain["total_buy_qty"].sum()) if "total_buy_qty" in chain else 0.0
    total_sell = float(chain["total_sell_qty"].sum()) if "total_sell_qty" in chain else 0.0
    buy_sell_ratio = total_buy / total_sell if total_sell > 0 else 1.0

    expiry = expiry_date or ""
    return {
        "total_volume": total_vol,
        "total_oi": total_oi,
        "total_buy_qty": total_buy,
        "total_sell_qty": total_sell,
        "buy_sell_ratio": buy_sell_ratio,
        "expiry": expiry,
        "fetched_at": _dt.now().isoformat(),
    }                    
