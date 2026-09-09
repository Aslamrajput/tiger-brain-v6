"""
Tiger Brain V6+V7 — Derivative Context Feed (KADAM 3)
========================================================
Ab tak poori pipeline NIFTY **spot** candles pe chalti thi, aur usi wajah
se teen sub-brain factors zinda hi nahi the:

  * volume confirmation — index spot candles ka volume hamesha 0 aata
    hai (verify kiya: 289/289 candles zero), isliye `has_volume_data()`
    har baar factor skip karta tha.
  * OI buildup — `oi_buildup_confirmed` / `oi_new_buildup_confirmed`
    hamesha `None` jaate the, yani wo factor bhi skip.
  * IV — Vol-Arb ko 20+ points ka IV series chahiye; wo kabhi tha hi
    nahi, isliye 5 mein se 5 brain kabhi vote nahi kar paaye.

Ye module wahi teen gaps asli Angel data se bharta hai:

  1. VOLUME — NIFTY **futures** candles (FUTIDX). Inme har bar pe asli
     traded volume hota hai. Price spot ka hi rehta hai (options spot pe
     settle hote hain); sirf `volume` column futures se aata hai.
  2. OI — futures ka `getOIData` (5-min OI history). OI ka rise/fall
     buildup vs unwinding batata hai.
  3. IV — ye `data/iv_series.py` mein hai (asli option candles se
     Black-Scholes reverse), aur yahan context mein jud jaata hai.

⚠️ HONESTY NOTES:
  - Jo feed nahi mila, wo **missing** rehta hai — `None`. Missing ko
    "confirmation mil gaya" ya "confirmation fail" maan lena dono galat
    hain; sub-brains khud missing par weight redistribute karte hain.
  - NO LOOKAHEAD: `DerivativeContext.scanner_kwargs(ts)` sirf `ts` tak
    ka data deta hai — aage ka OI/IV us bar ke decision mein nahi jaata.
  - Futures ≠ spot: basis (premium/discount) hota hai, isliye sirf
    volume/OI uthaya jaata hai, price nahi.
  - Angel ke master mein sirf ZINDA contracts hote hain. Futures ke liye
    bhi wahi registry pattern hai jo options ke liye hai — roz
    `python3 -m data.option_chain --refresh` chalne se history banti
    jaati hai; usse pehle ke expire ho chuke contracts ka data ab kabhi
    nahi milega.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta

import pandas as pd

try:
    from data.intraday import (
        DEFAULT_CACHE_DIR,
        INTRADAY_INTERVAL_MINUTES,
        load_intraday,
        now_ist,
        trading_days_between,
    )
    from data.option_chain import (
        DEFAULT_EXCHANGE,
        DEFAULT_UNDERLYING,
        MAX_CONTRACT_HISTORY_DAYS,
        fetch_scrip_master,
        load_registry,
        parse_expiry,
        save_registry,
    )
except ImportError:
    raise ImportError("Repo ROOT se chalao: python3 -m data.derivatives")

logger = logging.getLogger("tiger_brain.data.derivatives")
logging.basicConfig(level=logging.INFO)


FUTURES_INSTRUMENT_TYPE = "FUTIDX"
# Front contract expiry se itne din pehle hi agle contract pe roll kar
# lete hain — expiry week mein liquidity waise bhi next month mein shift
# ho jaati hai
FUTURES_ROLL_DAYS = 1
# Angel ek request mein itne din se zyada OI nahi deta (candles jaisa hi)
OI_MAX_DAYS_PER_REQUEST = 30
OI_MAX_RETRIES = 4
OI_RETRY_BACKOFF_SEC = 10.0
OI_CHUNK_PAUSE_SEC = 12.0

# OI buildup ka matlab: naye positions ban rahe hain. Horizon BARS mein
# nahi, MINUTES mein define hota hai — warna 1-min run pe "12 bars" 12
# minute ka hota aur 1-hour run pe 12 ghante ka, yani same market move
# alag-alag confirmation deta.
TREND_OI_LOOKBACK_MINUTES = 60
TREND_OI_MIN_CHANGE_PCT = 0.5
BREAKOUT_OI_LOOKBACK_MINUTES = 30
BREAKOUT_OI_MIN_CHANGE_PCT = 1.0
DEFAULT_INTERVAL = "FIVE_MINUTE"


def lookback_bars_for_interval(minutes: int, interval: str) -> int:
    """Minute-horizon → us interval ke bars (kam se kam 1 bar)."""
    if interval not in INTRADAY_INTERVAL_MINUTES:
        raise ValueError(f"Interval '{interval}' support nahi hai")
    return max(1, round(minutes / INTRADAY_INTERVAL_MINUTES[interval]))


# ============================================================
# 1. FUTURES CONTRACTS — master + registry
# ============================================================

def futures_registry_path(
    underlying: str = DEFAULT_UNDERLYING,
    exchange: str = DEFAULT_EXCHANGE,
    cache_dir: str = DEFAULT_CACHE_DIR,
) -> str:
    name = f"futures_registry_{underlying.upper()}_{exchange.upper()}.json"
    return os.path.join(cache_dir, name)


def extract_futures_contracts(
    master: list,
    underlying: str = DEFAULT_UNDERLYING,
    exchange: str = DEFAULT_EXCHANGE,
) -> dict:
    """Scrip master ke FUTIDX rows → {symbol: contract-dict}."""
    contracts = {}
    for row in master:
        if (
            row.get("name") != underlying.upper()
            or row.get("exch_seg") != exchange.upper()
            or row.get("instrumenttype") != FUTURES_INSTRUMENT_TYPE
        ):
            continue
        symbol = row.get("symbol", "")
        token = str(row.get("token", "")).strip()
        # Angel ke feeds mein kabhi-kabhi joda hua token aata hai
        # ('48704 61471') — aisa token har candle/OI request fail karta
        # hai, isliye use registry mein ghusne hi nahi dete
        if not symbol or not token or not token.isdigit():
            logger.warning(f"Futures row skip (kharab token): {symbol!r} {token!r}")
            continue
        contracts[symbol] = {
            "symbol": symbol,
            "token": token,
            "expiry": parse_expiry(row["expiry"]).isoformat(),
            "lot_size": int(float(row.get("lotsize", 0) or 0)),
        }
    return contracts


def refresh_futures_registry(
    underlying: str = DEFAULT_UNDERLYING,
    exchange: str = DEFAULT_EXCHANGE,
    cache_dir: str = DEFAULT_CACHE_DIR,
    master: list | None = None,
) -> dict:
    """
    Aaj ke zinda futures contracts registry mein jodta hai. Purane kabhi
    hataye nahi jaate — expire hone ke baad unka token master mein nahi
    milega, sirf yahin bachega.
    """
    path = futures_registry_path(underlying, exchange, cache_dir)
    registry = load_registry(path)
    live = extract_futures_contracts(
        master if master is not None else fetch_scrip_master(),
        underlying, exchange,
    )

    today = now_ist().date().isoformat()
    added = 0
    for symbol, contract in live.items():
        if symbol not in registry:
            contract["first_seen"] = today
            registry[symbol] = contract
            added += 1
        else:
            registry[symbol].update(contract)
        registry[symbol]["last_seen"] = today

    save_registry(registry, path)
    logger.info(
        f"Futures registry: {added} naye contracts, total {len(registry)} ({path})"
    )
    return registry


def futures_coverage_start(registry: dict) -> date | None:
    """
    Pehla din jis se registry par bharosa kiya ja sakta hai (pehla
    refresh). Usse pehle ke din ka asli front contract expire ho kar
    master se gayab ho chuka tha — us din ke liye jo contract registry
    mein bacha hai wo ek ALAG (aage ka) contract hai.

    Purani registries mein `first_seen` nahi hota — tab None, yani koi
    guard nahi (backward compatible).
    """
    seen = [
        date.fromisoformat(c["first_seen"])
        for c in registry.values()
        if c.get("first_seen")
    ]
    return min(seen) if seen else None


def select_futures_contract(
    registry: dict, day: date, roll_days: int = FUTURES_ROLL_DAYS,
    coverage_start: date | None = None,
) -> dict | None:
    """
    Us din ka FRONT contract — pehla contract jiski expiry `roll_days`
    door ya usse zyada ho. Expiry ke ekdum kareeb wala contract chhod
    dete hain kyunki tab volume/OI agle contract mein chala jaata hai.

    Registry coverage se pehle ke din pe kuch nahi lautata — galat
    contract ka volume/OI dena us din ke market ko jhoothla dena hai.
    """
    if coverage_start is not None and day < coverage_start:
        return None
    candidates = sorted(
        registry.values(), key=lambda c: date.fromisoformat(c["expiry"])
    )
    for contract in candidates:
        if (date.fromisoformat(contract["expiry"]) - day).days >= roll_days:
            return contract
    return None


def _contract_day_map(registry: dict, days: list, roll_days: int) -> dict:
    """{trading day: contract} — kis din kaunsa front contract chalega."""
    coverage_start = futures_coverage_start(registry)
    mapping = {}
    for day in days:
        contract = select_futures_contract(
            registry, day, roll_days, coverage_start
        )
        if contract is not None:
            mapping[day] = contract
    return mapping


# ============================================================
# 2. FUTURES CANDLES (asli volume yahin se aata hai)
# ============================================================

def load_futures_candles(
    index: pd.DatetimeIndex,
    interval: str = "FIVE_MINUTE",
    broker=None,
    underlying: str = DEFAULT_UNDERLYING,
    exchange: str = DEFAULT_EXCHANGE,
    cache_dir: str = DEFAULT_CACHE_DIR,
    offline: bool = False,
    registry: dict | None = None,
    roll_days: int = FUTURES_ROLL_DAYS,
) -> pd.DataFrame:
    """
    `index` (spot bars) ke har din ke liye front futures contract ki
    candles laata hai aur unhe ek continuous series mein jodta hai.

    Ek hi contract se poora saal nahi milta (monthly expiry), isliye
    stitching zaroori hai — par har din ka data USI din ke front contract
    se aata hai, mix nahi hota.
    """
    if interval not in INTRADAY_INTERVAL_MINUTES:
        raise ValueError(f"Interval '{interval}' support nahi hai")
    if registry is None:
        registry = load_registry(
            futures_registry_path(underlying, exchange, cache_dir)
        )
    if not registry or len(index) == 0:
        return pd.DataFrame()

    days = sorted({pd.Timestamp(ts).date() for ts in index})
    day_map = _contract_day_map(registry, days, roll_days)
    if not day_map:
        return pd.DataFrame()

    frames = []
    for token in sorted({c["token"] for c in day_map.values()}):
        contract_days = sorted(
            day for day, c in day_map.items() if c["token"] == token
        )
        contract = day_map[contract_days[0]]
        expiry = date.fromisoformat(contract["expiry"])
        history_days = max(
            (contract_days[-1] - contract_days[0]).days + 2,
            (min(expiry, contract_days[-1]) - contract_days[0]).days + 2,
        )
        candles = load_intraday(
            symbol=contract["symbol"],
            interval=interval,
            days=min(history_days, MAX_CONTRACT_HISTORY_DAYS),
            broker=broker,
            symbol_token=token,
            exchange=exchange,
            cache_dir=cache_dir,
            offline=offline,
            end=datetime.combine(min(expiry, days[-1]), datetime.max.time()),
        )
        if candles.empty:
            logger.warning(f"{contract['symbol']}: koi futures candle nahi mili")
            continue
        keep = pd.Index([pd.Timestamp(d) for d in contract_days])
        frames.append(candles[candles.index.normalize().isin(keep)])

    if not frames:
        return pd.DataFrame()
    stitched = pd.concat(frames)
    return stitched[~stitched.index.duplicated(keep="last")].sort_index()


def attach_futures_volume(
    spot_df: pd.DataFrame, futures_df: pd.DataFrame
) -> tuple:
    """
    Spot OHLC rakhta hai par `volume` futures se leta hai.

    Price spot ka hi rehna chahiye (options spot pe settle hote hain aur
    futures mein basis hota hai) — sirf volume wahan se aata hai jahan wo
    asli mein exist karta hai.

    Returns: (naya df, coverage dict)
    """
    out = spot_df.copy()
    coverage = {
        "bars": int(len(spot_df)),
        "matched": 0,
        "matched_pct": 0.0,
        "zero_volume_pct": 100.0,
    }
    if spot_df.empty or futures_df.empty or "volume" not in futures_df.columns:
        return out, coverage

    aligned = futures_df["volume"].reindex(spot_df.index)
    # Jis bar pe futures candle nahi mili wahan volume MISSING rahega
    # (0 likhna "koi trade nahi hua" ka jhoota dawa hota)
    out["volume"] = aligned
    matched = int(aligned.notna().sum())
    coverage["matched"] = matched
    coverage["matched_pct"] = round(matched / len(spot_df) * 100, 1)
    if matched:
        coverage["zero_volume_pct"] = round(
            float((aligned.dropna() <= 0).mean()) * 100, 2
        )
    return out, coverage


# ============================================================
# 3. OPEN INTEREST — getOIData (cache-first, rate-limit safe)
# ============================================================

def oi_cache_path(
    token: str, interval: str, exchange: str = DEFAULT_EXCHANGE,
    cache_dir: str = DEFAULT_CACHE_DIR,
) -> str:
    name = f"OI_{exchange.upper()}_{token}_{interval}.csv.gz"
    return os.path.join(cache_dir, name)


def load_cached_oi(
    token: str, interval: str, exchange: str = DEFAULT_EXCHANGE,
    cache_dir: str = DEFAULT_CACHE_DIR,
) -> pd.Series:
    path = oi_cache_path(token, interval, exchange, cache_dir)
    if not os.path.exists(path):
        return pd.Series(dtype="float64", name="open_interest")
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    return df["open_interest"].sort_index()


def save_cached_oi(
    series: pd.Series, token: str, interval: str,
    exchange: str = DEFAULT_EXCHANGE, cache_dir: str = DEFAULT_CACHE_DIR,
) -> str:
    path = oi_cache_path(token, interval, exchange, cache_dir)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    series.rename("open_interest").to_frame().to_csv(path)
    return path


def parse_oi_rows(rows: list) -> pd.Series:
    """Angel ke OI rows ({'time': ..., 'oi': ...}) → tz-naive IST Series."""
    if not rows:
        return pd.Series(dtype="float64", name="open_interest")
    frame = pd.DataFrame(rows)
    if "time" not in frame.columns or "oi" not in frame.columns:
        raise ValueError(f"getOIData ke rows mein time/oi nahi hain: {list(frame)}")
    index = pd.to_datetime(frame["time"], utc=True).dt.tz_convert(
        "Asia/Kolkata"
    ).dt.tz_localize(None)
    series = pd.Series(
        frame["oi"].astype(float).values, index=index, name="open_interest"
    )
    return series[~series.index.duplicated(keep="last")].sort_index()


def fetch_oi_chunk(
    broker, params: dict,
    max_retries: int = OI_MAX_RETRIES,
    backoff_sec: float = OI_RETRY_BACKOFF_SEC,
) -> list:
    """Ek OI chunk — rate-limit pe backoff ke saath retry (candles jaisa hi)."""
    from data.loader import is_rate_limit_error

    for attempt in range(max_retries):
        try:
            response = broker.smart_api.getOIData(params)
        except Exception as exc:
            if not is_rate_limit_error(exc) or attempt == max_retries - 1:
                raise
            response = {"message": str(exc)}

        if response.get("status") and response.get("data"):
            return response["data"]

        message = response.get("message", "unknown")
        if not is_rate_limit_error(message) or attempt == max_retries - 1:
            logger.warning(
                f"OI chunk {params['fromdate']}-{params['todate']} khali/fail: "
                f"{message}"
            )
            return []
        delay = backoff_sec * (2 ** attempt)
        logger.warning(f"Angel rate limit (OI) — {delay:.0f}s baad retry")
        time.sleep(delay)
    return []


def fetch_oi_history(
    broker, exchange: str, token: str, interval: str,
    start: datetime, end: datetime,
) -> pd.Series:
    """Poori window ka OI — 30-30 din ke chunks mein (Angel ki limit)."""
    rows = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(
            (chunk_start + timedelta(days=OI_MAX_DAYS_PER_REQUEST - 1)).replace(
                hour=23, minute=59, second=0, microsecond=0
            ),
            end,
        )
        rows.extend(fetch_oi_chunk(broker, {
            "exchange": exchange,
            "symboltoken": token,
            "interval": interval,
            "fromdate": chunk_start.strftime("%Y-%m-%d %H:%M"),
            "todate": chunk_end.strftime("%Y-%m-%d %H:%M"),
        }))
        chunk_start = (chunk_end + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        if chunk_start <= end:
            time.sleep(OI_CHUNK_PAUSE_SEC)
    return parse_oi_rows(rows)


def missing_oi_ranges(cached: pd.Series, start: datetime, end: datetime) -> list:
    """
    Window ke jo trading DIN cache mein nahi hain, unke fetch-ranges.

    Candles wala `missing_ranges()` sirf cache ke aage/peeche dekhta hai.
    OI chunk beech mein fail ho jaye (rate limit/error) to uske dono taraf
    ka data cache ho jaata hai aur wo hole phir kabhi maanga hi nahi jaata
    — us daur ke OI confirmations hamesha ke liye gayab. Isliye yahan
    din-dar-din dekhte hain, aur chalu din hamesha dobara maangte hain
    (uska session abhi adhoora hai).
    """
    days = trading_days_between(pd.Timestamp(start), pd.Timestamp(end))
    if not days:
        return []
    have = (
        set(pd.DatetimeIndex(cached.index).normalize())
        if len(cached) else set()
    )
    today = pd.Timestamp(now_ist().date())
    missing = [d for d in days if d not in have or d == today]
    if not missing:
        return []

    ranges = []
    run_start = run_end = missing[0]
    for day in missing[1:]:
        if (day - run_end).days <= 4:      # weekend/holiday gap chhodo
            run_end = day
            continue
        ranges.append((run_start, run_end))
        run_start = run_end = day
    ranges.append((run_start, run_end))

    return [
        (
            max(first.to_pydatetime(), start),
            min(last.replace(hour=23, minute=59).to_pydatetime(), end),
        )
        for first, last in ranges
    ]


def load_oi_series(
    token: str,
    interval: str = "FIVE_MINUTE",
    start: datetime | None = None,
    end: datetime | None = None,
    broker=None,
    exchange: str = DEFAULT_EXCHANGE,
    cache_dir: str = DEFAULT_CACHE_DIR,
    offline: bool = False,
) -> pd.Series:
    """
    Ek contract ki OI history — cache-first, sirf missing hissa Angel se.
    (Wahi pattern jo `data/intraday.load_intraday()` candles ke liye
    use karta hai.)
    """
    cached = load_cached_oi(token, interval, exchange, cache_dir)
    if offline or start is None or end is None:
        if offline and cached.empty:
            logger.warning(f"Offline: token {token} ka OI cache khaali hai")
        return cached

    merged = cached
    for fetch_start, fetch_end in missing_oi_ranges(cached, start, end):
        fetched = fetch_oi_history(
            broker, exchange, token, interval, fetch_start, fetch_end
        )
        if fetched.empty:
            continue
        merged = pd.concat([merged, fetched])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()

    if not merged.empty:
        save_cached_oi(merged, token, interval, exchange, cache_dir)
    return merged[(merged.index >= start) & (merged.index <= end)]


def load_futures_oi(
    index: pd.DatetimeIndex,
    interval: str = "FIVE_MINUTE",
    broker=None,
    underlying: str = DEFAULT_UNDERLYING,
    exchange: str = DEFAULT_EXCHANGE,
    cache_dir: str = DEFAULT_CACHE_DIR,
    offline: bool = False,
    registry: dict | None = None,
    roll_days: int = FUTURES_ROLL_DAYS,
) -> pd.Series:
    """
    Front futures ka stitched OI series — har din usi din ke contract se.

    ⚠️ Contract badalne wale din OI ka LEVEL jump karta hai (naya contract
    = alag OI base). Isliye buildup flags har contract ke andar hi
    calculate hote hain (`oi_buildup_flags` ko `contract_ids` milta hai).
    """
    if registry is None:
        registry = load_registry(
            futures_registry_path(underlying, exchange, cache_dir)
        )
    if not registry or len(index) == 0:
        return pd.Series(dtype="float64", name="open_interest")

    days = sorted({pd.Timestamp(ts).date() for ts in index})
    day_map = _contract_day_map(registry, days, roll_days)
    if not day_map:
        return pd.Series(dtype="float64", name="open_interest")

    parts = []
    for token in sorted({c["token"] for c in day_map.values()}):
        contract_days = sorted(
            day for day, c in day_map.items() if c["token"] == token
        )
        series = load_oi_series(
            token=token, interval=interval,
            start=datetime.combine(contract_days[0], datetime.min.time()),
            end=datetime.combine(contract_days[-1], datetime.max.time()),
            broker=broker, exchange=exchange, cache_dir=cache_dir,
            offline=offline,
        )
        if series.empty:
            continue
        keep = pd.Index([pd.Timestamp(d) for d in contract_days])
        parts.append(series[series.index.normalize().isin(keep)])

    if not parts:
        return pd.Series(dtype="float64", name="open_interest")
    combined = pd.concat(parts)
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    return combined.rename("open_interest")


def contract_ids_for_index(
    index: pd.DatetimeIndex,
    registry: dict,
    roll_days: int = FUTURES_ROLL_DAYS,
) -> pd.Series:
    """Har bar pe kaunsa futures contract front tha (token) — series."""
    days = sorted({pd.Timestamp(ts).date() for ts in index})
    day_map = _contract_day_map(registry, days, roll_days)
    tokens = [
        day_map[pd.Timestamp(ts).date()]["token"]
        if pd.Timestamp(ts).date() in day_map else None
        for ts in index
    ]
    return pd.Series(tokens, index=index, name="contract")


def oi_buildup_flags(
    oi_series: pd.Series,
    lookback_bars: int,
    min_change_pct: float = TREND_OI_MIN_CHANGE_PCT,
    contract_ids: pd.Series | None = None,
    same_session_only: bool = True,
) -> pd.Series:
    """
    Har bar pe: pichhle `lookback_bars` mein OI itna % bada kya?

    True  = naya buildup ho raha hai (positions ban rahe hain)
    False = OI flat ya gir raha hai (unwinding — confirmation nahi)
    NaN   = us bar pe OI data hi nahi (missing, "False" nahi)

    Contract roll wale bar pe comparison nahi hota — do alag contracts ka
    OI compare karna bakwaas number deta hai. Usi tarah horizon intraday
    hai, isliye default se session ke paar bhi compare nahi hota (raat
    bhar ka OI change intraday buildup nahi hai).
    """
    if oi_series.empty:
        return pd.Series(dtype="object")
    if lookback_bars <= 0:
        raise ValueError("lookback_bars 0 se bada hona chahiye")

    previous = oi_series.shift(lookback_bars)
    change_pct = (oi_series - previous) / previous.abs() * 100
    flags = change_pct >= min_change_pct
    flags = flags.where(change_pct.notna())

    if contract_ids is not None:
        ids = contract_ids.reindex(oi_series.index)
        same_contract = ids == ids.shift(lookback_bars)
        flags = flags.where(same_contract.fillna(False))

    if same_session_only:
        sessions = pd.Series(
            pd.DatetimeIndex(oi_series.index).normalize(), index=oi_series.index
        )
        flags = flags.where(sessions == sessions.shift(lookback_bars))
    return flags.astype("object").where(flags.notna())


# ============================================================
# 4. CONTEXT — scanner ko kya-kya pass karna hai
# ============================================================

class DerivativeContext:
    """
    Har bar ke liye sub-brains ka derivative context deta hai, bina
    lookahead ke.

    Missing feed ka jawab hamesha `None` hota hai — sub-brain tab us
    factor ka weight redistribute karta hai aur reason batata hai.
    """

    MIN_IV_POINTS = 20  # vol_arb isse kam pe khud NO_TRADE deta hai (DIN)

    def __init__(
        self,
        oi_series: pd.Series | None = None,
        iv_frame: pd.DataFrame | None = None,
        contract_ids: pd.Series | None = None,
        interval: str = DEFAULT_INTERVAL,
        trend_lookback_bars: int | None = None,
        trend_min_change_pct: float = TREND_OI_MIN_CHANGE_PCT,
        breakout_lookback_bars: int | None = None,
        breakout_min_change_pct: float = BREAKOUT_OI_MIN_CHANGE_PCT,
        volume_coverage: dict | None = None,
    ):
        self.oi_series = oi_series
        self.iv_frame = iv_frame
        self.interval = interval
        self.volume_coverage = volume_coverage or {}
        # Horizon minutes mein tay hota hai; bars interval se nikalte hain
        self.trend_lookback_bars = trend_lookback_bars or lookback_bars_for_interval(
            TREND_OI_LOOKBACK_MINUTES, interval
        )
        self.breakout_lookback_bars = (
            breakout_lookback_bars
            or lookback_bars_for_interval(BREAKOUT_OI_LOOKBACK_MINUTES, interval)
        )

        if oi_series is not None and not oi_series.empty:
            self.trend_flags = oi_buildup_flags(
                oi_series, self.trend_lookback_bars, trend_min_change_pct,
                contract_ids,
            )
            self.breakout_flags = oi_buildup_flags(
                oi_series, self.breakout_lookback_bars, breakout_min_change_pct,
                contract_ids,
            )
        else:
            self.trend_flags = pd.Series(dtype="object")
            self.breakout_flags = pd.Series(dtype="object")

        self.used = {"oi_bars": 0, "iv_bars": 0, "bars": 0}

    @staticmethod
    def _flag_at(flags: pd.Series, timestamp) -> bool | None:
        key = pd.Timestamp(timestamp)
        if flags.empty or key not in flags.index:
            return None
        value = flags.loc[key]
        if isinstance(value, pd.Series):
            value = value.iloc[-1]
        if pd.isna(value):
            return None
        return bool(value)

    @staticmethod
    def _daily_iv(atm: pd.Series) -> pd.Series:
        """
        Intraday IV bars → DAILY history.

        `vol_arb` ka minimum (20) aur `PERCENTILE_LOOKBACK_DAYS` dono
        **din** mein hain. Har 5-min bar ko ek "din" ginne se percentile
        do ghante mein hi bharam se activate ho jaata hai.

        Har session ka aakhri available observation ek din ginta hai;
        chalu (aadha) din bhi apne ab tak ke aakhri IV se ek observation
        deta hai (us bar tak ka hi data — lookahead nahi).
        """
        if atm.empty:
            return atm
        sessions = pd.DatetimeIndex(atm.index).normalize()
        daily = atm.groupby(sessions).last()
        daily.index = pd.DatetimeIndex(daily.index)
        return daily.sort_index()

    def _iv_upto(self, timestamp) -> pd.DataFrame | None:
        """Sirf `timestamp` tak ka IV — aage ka bilkul nahi (no lookahead)."""
        if self.iv_frame is None or self.iv_frame.empty:
            return None
        window = self.iv_frame.loc[: pd.Timestamp(timestamp)]
        return window if not window.empty else None

    def scanner_kwargs(self, timestamp) -> dict:
        """`pipeline.stage1_scanner.run_scanner()` ko dene wale kwargs."""
        self.used["bars"] += 1
        kwargs = {
            "oi_buildup_confirmed": self._flag_at(self.trend_flags, timestamp),
            "oi_new_buildup_confirmed": self._flag_at(
                self.breakout_flags, timestamp
            ),
        }
        if kwargs["oi_buildup_confirmed"] is not None:
            self.used["oi_bars"] += 1

        iv_window = self._iv_upto(timestamp)
        if iv_window is None:
            return kwargs

        daily_atm = self._daily_iv(iv_window["atm_iv"].dropna())
        if len(daily_atm) >= self.MIN_IV_POINTS:
            kwargs["iv_series"] = daily_atm
            self.used["iv_bars"] += 1
        for column, key in (("call_iv", "call_iv"), ("put_iv", "put_iv")):
            if column in iv_window.columns:
                values = iv_window[column].dropna()
                if not values.empty:
                    kwargs[key] = float(values.iloc[-1])
        return kwargs

    def summary(self) -> dict:
        return {
            "volume": dict(self.volume_coverage),
            "oi_points": 0 if self.oi_series is None else int(len(self.oi_series)),
            "iv_points": (
                0 if self.iv_frame is None
                else int(self.iv_frame["atm_iv"].notna().sum())
            ),
            "bars_evaluated": self.used["bars"],
            "bars_with_oi": self.used["oi_bars"],
            "bars_with_iv": self.used["iv_bars"],
        }


def print_derivative_report(summary: dict) -> None:
    print("\n" + "=" * 66)
    print("DERIVATIVE FEEDS (futures volume + OI + IV)")
    print("=" * 66)
    volume = summary.get("volume") or {}
    if volume:
        print(
            f"Futures volume    : {volume.get('matched', 0)}/"
            f"{volume.get('bars', 0)} bars ({volume.get('matched_pct', 0)}%), "
            f"zero-volume {volume.get('zero_volume_pct', 0)}%"
        )
    else:
        print("Futures volume    : nahi mila — spot ka volume=0 hi chalega")
    print(f"OI points         : {summary['oi_points']}")
    print(f"IV points         : {summary['iv_points']}")
    print(
        f"Bars evaluate hue : {summary['bars_evaluated']} | OI mila "
        f"{summary['bars_with_oi']} par | IV series mili "
        f"{summary['bars_with_iv']} par"
    )
    if summary["bars_evaluated"] and not summary["bars_with_oi"]:
        print(
            "⚠️ Ek bhi bar pe OI nahi mila — OI factor har jagah SKIP hua "
            "(ise 'confirmation fail' mat samajhna)."
        )
    if summary["bars_evaluated"] and not summary["bars_with_iv"]:
        print(
            "⚠️ IV series kabhi 20 points tak nahi pahunchi — Vol-Arb "
            "sub-brain aur IV-crush veto is run mein active nahi the."
        )
    print("=" * 66)
