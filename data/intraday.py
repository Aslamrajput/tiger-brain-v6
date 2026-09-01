"""
Tiger Brain V6+V7 — Intraday Data Layer (Phase 3, step 1)
============================================================
Ab tak poora system DAILY candles pe chalta tha — yani din mein zyada se
zyada 1 decision possible tha. Intraday (1/5/15-min) trading ke liye
sabse pehle ek bharosemand data layer chahiye, aur usme teen alag kaam
hain:

  1. FETCH — Angel One se intraday candles (chunk limits `data/loader.py`
     handle karta hai; 1-min ka max 30 din prati request hai).
  2. CACHE — har fetch API pe load daalta hai aur rate-limit khaata hai.
     Isliye data disk pe cache hota hai aur agli baar sirf MISSING tail
     hi download hoti hai.
  3. CLEAN + QUALITY CHECK — ye sabse important hissa hai. Intraday data
     mein duplicate candles, missing minutes, session ke bahar ke rows
     aur holiday rows aana normal baat hai. Inhe chup-chaap accept karna
     backtest ko jhootha bana deta hai, isliye yahan har cheez count
     hoti hai aur report mein dikhti hai.

⚠️ HONESTY NOTES:
  - Ye module sirf UNDERLYING (index/stock) candles laata hai. Option
    premium history isme nahi hai — wo alag kaam hai (real option-chain
    history), aur uske bina intraday options P&L ka number bharosemand
    nahi hoga.
  - Missing candles ko ye module BHARTA NAHI hai (koi forward-fill
    nahi). Fake candle banane se indicators jhoothe ho jaate hain —
    yahan gaps sirf report hote hain, chhupte nahi.
  - Sandbox mein internet nahi hai, isliye fetch wala hissa yahan test
    nahi hua; clean/resample/cache/quality sab offline tested hain.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, time, timedelta, timezone

import pandas as pd

try:
    from automation.holidays import has_holiday_calendar, is_market_holiday
    from config.thresholds import AUTOMATION
except ImportError:
    raise ImportError("Repo ROOT se chalao: python3 -m data.intraday")

logger = logging.getLogger("tiger_brain.data.intraday")
logging.basicConfig(level=logging.INFO)


# Angel One ke interval naam → minutes
INTRADAY_INTERVAL_MINUTES = {
    "ONE_MINUTE": 1,
    "THREE_MINUTE": 3,
    "FIVE_MINUTE": 5,
    "TEN_MINUTE": 10,
    "FIFTEEN_MINUTE": 15,
    "THIRTY_MINUTE": 30,
    "ONE_HOUR": 60,
}

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]
DEFAULT_CACHE_DIR = "data_cache"
NIFTY_SPOT_TOKEN = "99926000"

# Angel ke saare timestamps IST mein hote hain. Server UTC pe chal sakta
# hai (hamara EC2 UTC hi hai), isliye kabhi bhi seedha `datetime.now()`
# use mat karo — warna 09:15-15:30 IST wali window galat jagah gir jaati
# hai aur aaj ka session download hi nahi hota.
IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    """Abhi ka IST time, tz-naive (baaki data bhi tz-naive IST hai)."""
    return datetime.now(IST).replace(tzinfo=None)


def intraday_window(days: int) -> tuple:
    """
    (start, end) IST window jo maangi jaayegi. Start hamesha aadhi raat
    pe hota hai: Angel ka chunking din-dar-din chalta hai, isliye
    from_date ka TIME har chunk boundary pe repeat hota aur 14:32 jaisa
    start har boundary din ki subah ki candles kha jaata.
    """
    end = now_ist()
    start = (end - timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return start, end


def session_bounds() -> tuple:
    """Market open/close time config se (09:15 - 15:30 by default)."""
    open_h, open_m = map(int, AUTOMATION["MARKET_OPEN_TIME"].split(":"))
    close_h, close_m = map(int, AUTOMATION["MARKET_CLOSE_TIME"].split(":"))
    return time(open_h, open_m), time(close_h, close_m)


def candles_per_session(interval: str) -> int:
    """Ek poore trading din mein kitni candles honi CHAHIYE."""
    minutes = INTRADAY_INTERVAL_MINUTES[interval]
    open_t, close_t = session_bounds()
    session_minutes = (
        (close_t.hour * 60 + close_t.minute) - (open_t.hour * 60 + open_t.minute)
    )
    return session_minutes // minutes


# ============================================================
# 1. CLEANING — session filter, holidays, duplicates
# ============================================================

def clean_intraday(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    """
    Raw intraday candles ko trustworthy banata hai:

      * timestamp index (tz hata ke, taaki VIX/daily data ke saath
        compare karte waqt tz-mismatch crash na ho)
      * sirf market hours ki candles (09:15 se 15:30 se pehle tak)
      * weekend + NSE holiday rows hata deta hai
      * duplicate timestamps ka aakhri version rakhta hai
      * time ke hisaab se sort

    ⚠️ Missing candles ko ye BHARTA nahi — gaps `candle_quality_report()`
    mein report hote hain.
    """
    if df.empty:
        return df

    out = df.copy()
    out.columns = [c.lower() for c in out.columns]
    missing = [c for c in OHLCV_COLUMNS if c not in out.columns]
    if missing:
        raise ValueError(f"Intraday data mein ye columns nahi hain: {missing}")

    out = out[OHLCV_COLUMNS]
    out.index = pd.to_datetime(out.index)
    if out.index.tz is not None:
        out.index = out.index.tz_localize(None)

    open_t, close_t = session_bounds()
    times = out.index.time
    out = out[(times >= open_t) & (times < close_t)]

    out = out[out.index.dayofweek < 5]
    holiday_mask = [is_market_holiday(ts.date())[0] for ts in out.index]
    out = out[[not flag for flag in holiday_mask]]

    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out


def resample_candles(df: pd.DataFrame, target_minutes: int) -> pd.DataFrame:
    """
    Chhoti candles ko badi candles mein badalta hai (jaise 1-min → 5-min),
    taaki ek hi download se kai timeframes ban sakein.

    Bins session ke andar hi rehti hain (har din alag resample hota hai),
    warna 15:30 aur agle din 09:15 ek hi bin mein mil jaate.
    """
    if df.empty:
        return df
    if target_minutes <= 0:
        raise ValueError("target_minutes 0 se bada hona chahiye")

    agg = {
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }
    rule = f"{target_minutes}min"
    parts = [
        day_df.resample(rule, origin="start").agg(agg).dropna(subset=["open"])
        for _, day_df in df.groupby(df.index.normalize())
    ]
    return pd.concat(parts).sort_index()


# ============================================================
# 2. QUALITY REPORT — data pe bharosa karne se pehle
# ============================================================

def trading_days_between(start: pd.Timestamp, end: pd.Timestamp) -> list:
    """
    [start, end] ke beech ke saare NSE trading din (weekend/holiday chhod ke).

    Jis saal ki holiday list repo mein nahi hai, us saal ke din yahan se
    chhod diye jaate hain — warna har weekday holiday jhoota "gayab
    session" ban jaata.
    """
    days = pd.date_range(start.normalize(), end.normalize(), freq="D")
    return [
        day for day in days
        if day.dayofweek < 5
        and has_holiday_calendar(day.year)
        and not is_market_holiday(day.date())[0]
    ]


def candle_quality_report(
    df: pd.DataFrame, interval: str,
    expected_start: datetime | None = None, expected_end: datetime | None = None,
) -> dict:
    """
    Intraday dataset ki sachchai batata hai: kitne sessions hain, har
    session mein kitni candles honi chahiye thi, kitni missing hain,
    kaunse din adhoore hain, aur kitni candles ka volume 0 hai.

    expected_start/expected_end: jo window MAANGI gayi thi. Inke bina
    range sirf mile hue data se banti hai, isliye window ke shuru/aakhir
    ka poora fail hua chunk dikhta hi nahi.
    """
    expected = candles_per_session(interval)
    report = {
        "interval": interval,
        "rows": len(df),
        "sessions": 0,
        "expected_per_session": expected,
        "missing_candles": 0,
        "incomplete_sessions": [],
        "missing_sessions": [],
        "zero_volume_pct": 0.0,
        "first": None,
        "last": None,
    }
    if df.empty:
        return report

    by_day = df.groupby(df.index.normalize()).size()
    report["sessions"] = int(len(by_day))

    # Sirf maujood dino ko dekhna kaafi nahi — agar ek poora trading din
    # download hi na hua ho to wo yahan dikhna chahiye, warna adhoora
    # dataset "clean" lagta hai.
    present = set(by_day.index)
    range_start = pd.Timestamp(expected_start) if expected_start else df.index[0]
    range_end = pd.Timestamp(expected_end) if expected_end else df.index[-1]
    absent = [
        day for day in trading_days_between(range_start, range_end)
        if day not in present
    ]
    report["missing_sessions"] = [day.date().isoformat() for day in absent]

    incomplete_missing = int((expected - by_day).clip(lower=0).sum())
    report["missing_candles"] = incomplete_missing + expected * len(absent)
    report["incomplete_sessions"] = [
        (day.date().isoformat(), int(count))
        for day, count in by_day.items()
        if count < expected
    ]
    report["zero_volume_pct"] = round(
        float((df["volume"].fillna(0) <= 0).mean()) * 100, 2
    )
    report["first"] = df.index[0].isoformat()
    report["last"] = df.index[-1].isoformat()
    return report


def print_quality_report(report: dict) -> None:
    print("\n" + "=" * 66)
    print(f"INTRADAY DATA QUALITY — {report['interval']}")
    print("=" * 66)
    print(f"Candles           : {report['rows']}")
    print(f"Sessions          : {report['sessions']}")
    print(f"Range             : {report['first']} → {report['last']}")
    print(
        f"Missing candles   : {report['missing_candles']} "
        f"(expected {report['expected_per_session']} per session)"
    )
    print(f"Zero-volume share : {report['zero_volume_pct']}%")

    absent = report["missing_sessions"]
    if absent:
        preview = ", ".join(absent[:5])
        more = f" … +{len(absent) - 5} aur" if len(absent) > 5 else ""
        print(f"GAYAB sessions    : {len(absent)} — {preview}{more}")
        print(
            "⚠️ Ye trading din data mein hain hi nahi (fetch fail hua ya "
            "broker ne diya hi nahi) — inhe backtest mein 'quiet day' mat samjho."
        )

    incomplete = report["incomplete_sessions"]
    if incomplete:
        preview = ", ".join(f"{day} ({count})" for day, count in incomplete[:5])
        more = f" … +{len(incomplete) - 5} aur" if len(incomplete) > 5 else ""
        print(f"Adhoore sessions  : {len(incomplete)} — {preview}{more}")
        print(
            "⚠️ Adhoore sessions ko ye module BHARTA nahi — inpe indicators "
            "kam data se bante hain, isliye inke signals kamzor maano."
        )
    if report["zero_volume_pct"] > 50:
        print(
            "⚠️ Zyadatar candles ka volume 0 hai (index spot feed mein ye "
            "normal hai) — volume-based confirmations yahan kaam nahi karenge."
        )
    print("=" * 66)


# ============================================================
# 3. CACHE + FETCH
# ============================================================

def cache_path(
    symbol: str, interval: str, cache_dir: str = DEFAULT_CACHE_DIR,
    exchange: str = "NSE", symbol_token: str = NIFTY_SPOT_TOKEN,
) -> str:
    """
    Cache file ka naam. Exchange + token bhi naam mein hain, warna alag
    instrument (jaise BANKNIFTY ya koi option) wahi "NIFTY" naam use
    karke ek doosre ke bhaav se mix ho jaayein.
    """
    name = f"{symbol.upper()}_{exchange.upper()}_{symbol_token}_{interval}.csv.gz"
    return os.path.join(cache_dir, name)


def load_cached(
    symbol: str, interval: str, cache_dir: str = DEFAULT_CACHE_DIR,
    exchange: str = "NSE", symbol_token: str = NIFTY_SPOT_TOKEN,
) -> pd.DataFrame:
    """Cache se data padhta hai; na ho to khaali DataFrame."""
    path = cache_path(symbol, interval, cache_dir, exchange, symbol_token)
    if not os.path.exists(path):
        return pd.DataFrame(columns=OHLCV_COLUMNS)
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    return df[OHLCV_COLUMNS].sort_index()


def save_cache(
    df: pd.DataFrame, symbol: str, interval: str,
    cache_dir: str = DEFAULT_CACHE_DIR,
    exchange: str = "NSE", symbol_token: str = NIFTY_SPOT_TOKEN,
) -> str:
    path = cache_path(symbol, interval, cache_dir, exchange, symbol_token)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    df.to_csv(path)
    return path


def merge_candles(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """
    Purane cache aur naye fetch ko jodta hai — overlap mein NAYA data
    jeetta hai (broker kabhi-kabhi candle baad mein revise karta hai).
    """
    if old.empty:
        return new.sort_index()
    if new.empty:
        return old.sort_index()
    combined = pd.concat([old, new])
    return combined[~combined.index.duplicated(keep="last")].sort_index()


def load_intraday(
    symbol: str = "NIFTY",
    interval: str = "FIVE_MINUTE",
    days: int = 30,
    broker=None,
    symbol_token: str = NIFTY_SPOT_TOKEN,
    exchange: str = "NSE",
    cache_dir: str = DEFAULT_CACHE_DIR,
    offline: bool = False,
) -> pd.DataFrame:
    """
    Intraday candles ka main entry point — cache-first, incremental fetch.

    Pehle cache padhta hai, phir sirf MISSING tail (cache ke last candle
    se aaj tak) Angel se maangta hai. Isse rate limits bachte hain aur
    baar-baar chalane pe result reproducible rehta hai.

    Args:
        broker: logged-in AngelBroker. None = khud login karega
                (jab tak `offline=True` na ho).
        offline: sirf cache use karo, koi network call nahi.

    Returns:
        Cleaned OHLCV DataFrame (index=timestamp), maangi hui window ka.
    """
    if interval not in INTRADAY_INTERVAL_MINUTES:
        raise ValueError(
            f"Interval '{interval}' support nahi hai. "
            f"Chalega: {sorted(INTRADAY_INTERVAL_MINUTES)}"
        )
    if days <= 0:
        raise ValueError("days 0 se bada hona chahiye")

    start, end = intraday_window(days)
    cached = clean_intraday(
        load_cached(symbol, interval, cache_dir, exchange, symbol_token), interval
    )

    if offline:
        if cached.empty:
            logger.warning(
                f"Offline mode par {symbol}/{interval} ka cache khaali hai."
            )
        return cached[cached.index >= start]

    fetch_start = start
    if not cached.empty and cached.index[-1] > start:
        # Aakhri cached din dobara maangte hain — us din ka session
        # adhoora cache hua ho sakta hai
        fetch_start = cached.index[-1].normalize().to_pydatetime()

    fetched = _fetch_from_angel(
        broker, exchange, symbol_token, interval, fetch_start, end
    )
    merged = merge_candles(cached, clean_intraday(fetched, interval))

    if not merged.empty:
        save_cache(merged, symbol, interval, cache_dir, exchange, symbol_token)
    return merged[merged.index >= start]


def _fetch_from_angel(
    broker, exchange: str, symbol_token: str, interval: str,
    start: datetime, end: datetime,
) -> pd.DataFrame:
    from data.loader import fetch_angel_historical_candles

    if broker is None:
        from broker.angel_connect import AngelBroker

        broker = AngelBroker()
        broker.login()

    logger.info(
        f"Angel se {interval} candles: {start.date()} se {end.date()} tak"
    )
    return fetch_angel_historical_candles(
        broker, exchange, symbol_token, interval, start, end
    )


# ============================================================
# CLI — python3 -m data.intraday --interval FIVE_MINUTE --days 30
# ============================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m data.intraday",
        description="Intraday candles download + cache + quality check",
    )
    parser.add_argument(
        "--symbol", default="NIFTY", help="Cache ka naam (default: NIFTY)"
    )
    parser.add_argument(
        "--interval", default="FIVE_MINUTE", choices=sorted(INTRADAY_INTERVAL_MINUTES),
        help="Candle size (default: FIVE_MINUTE)",
    )
    parser.add_argument(
        "--days", type=int, default=30,
        help="Kitne din peeche tak (1-min ka Angel limit 30 din prati request hai)",
    )
    parser.add_argument(
        "--symbol-token", default=NIFTY_SPOT_TOKEN,
        help=f"Angel instrument token (default NIFTY spot: {NIFTY_SPOT_TOKEN})",
    )
    parser.add_argument("--exchange", default="NSE", help="NSE ya NFO (default: NSE)")
    parser.add_argument(
        "--cache-dir", default=DEFAULT_CACHE_DIR,
        help=f"Cache folder (default: {DEFAULT_CACHE_DIR})",
    )
    parser.add_argument(
        "--offline", action="store_true",
        help="Sirf cache se padho, koi broker login/network call nahi",
    )
    parser.add_argument(
        "--resample", type=int,
        help="Download ke baad in minutes ki candles bhi banao (jaise 15)",
    )
    parser.add_argument("--save-csv", help="Final data yahan CSV mein save karo")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    df = load_intraday(
        symbol=args.symbol, interval=args.interval, days=args.days,
        symbol_token=args.symbol_token, exchange=args.exchange,
        cache_dir=args.cache_dir, offline=args.offline,
    )
    if df.empty:
        print("ERROR: Koi intraday candle nahi mili.")
        return 1

    window_start, window_end = intraday_window(args.days)
    print_quality_report(
        candle_quality_report(df, args.interval, window_start, window_end)
    )

    if args.resample:
        df = resample_candles(df, args.resample)
        print(f"\n{args.resample}-min candles banayi: {len(df)} rows")

    if args.save_csv:
        df.to_csv(args.save_csv)
        print(f"Save ho gaya: {args.save_csv}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
