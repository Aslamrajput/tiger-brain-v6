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
        missing_ranges,
        now_ist,
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
OI_RETRY_BACKOFF_SEC = 5.0
OI_CHUNK_PAUSE_SEC = 1.0

# OI buildup ka matlab: naye positions ban rahe hain. Trend confirmation
# ke liye ghante bhar ka rise dekhte hain, breakout ke liye chhoti aur
# tez window (fresh buildup) — dono thresholds config nahi, yahi defaults
# hain aur CLI se badle ja sakte hain
TREND_OI_LOOKBACK_BARS = 12
TREND_OI_MIN_CHANGE_PCT = 0.5
BREAKOUT_OI_LOOKBACK_BARS = 6
BREAKOUT_OI_MIN_CHANGE_PCT = 1.0


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


def select_futures_contract(
    registry: dict, day: date, roll_days: int = FUTURES_ROLL_DAYS
) -> dict | None:
    """
    Us din ka FRONT contract — pehla contract jiski expiry `roll_days`
    door ya usse zyada ho. Expiry ke ekdum kareeb wala contract chhod
    dete hain kyunki tab volume/OI agle contract mein chala jaata hai.
    """
    candidates = sorted(
        registry.values(), key=lambda c: date.fromisoformat(c["expiry"])
    )
    for contract in candidates:
        if (date.fromisoformat(contract["expiry"]) - day).days >= roll_days:
            return contract
    return None


def _contract_day_map(registry: dict, days: list, roll_days: int) -> dict:
    """{trading day: contract} — kis din kaunsa front contract chalega."""
    mapping = {}
    for day in days:
        contract = select_futures_contract(registry, day, roll_days)
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
    frame = cached.to_frame() if not cached.empty else pd.DataFrame()
    for fetch_start, fetch_end in missing_ranges(frame, start, end):
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
    lookback_bars: int = TREND_OI_LOOKBACK_BARS,
    min_change_pct: float = TREND_OI_MIN_CHANGE_PCT,
    contract_ids: pd.Series | None = None,
) -> pd.Series:
    """
    Har bar pe: pichhle `lookback_bars` mein OI itna % bada kya?

    True  = naya buildup ho raha hai (positions ban rahe hain)
    False = OI flat ya gir raha hai (unwinding — confirmation nahi)
    NaN   = us bar pe OI data hi nahi (missing, "False" nahi)

    Contract roll wale bar pe comparison nahi hota — do alag contracts ka
    OI compare karna bakwaas number deta hai.
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

    MIN_IV_POINTS = 20  # vol_arb isse kam pe khud NO_TRADE deta hai

    def __init__(
        self,
        oi_series: pd.Series | None = None,
        iv_frame: pd.DataFrame | None = None,
        contract_ids: pd.Series | None = None,
        trend_lookback_bars: int = TREND_OI_LOOKBACK_BARS,
        trend_min_change_pct: float = TREND_OI_MIN_CHANGE_PCT,
        breakout_lookback_bars: int = BREAKOUT_OI_LOOKBACK_BARS,
        breakout_min_change_pct: float = BREAKOUT_OI_MIN_CHANGE_PCT,
        volume_coverage: dict | None = None,
    ):
        self.oi_series = oi_series
        self.iv_frame = iv_frame
        self.volume_coverage = volume_coverage or {}

        if oi_series is not None and not oi_series.empty:
            self.trend_flags = oi_buildup_flags(
                oi_series, trend_lookback_bars, trend_min_change_pct,
                contract_ids,
            )
            self.breakout_flags = oi_buildup_flags(
                oi_series, breakout_lookback_bars, breakout_min_change_pct,
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

        atm = iv_window["atm_iv"].dropna()
        if len(atm) >= self.MIN_IV_POINTS:
            kwargs["iv_series"] = atm
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
