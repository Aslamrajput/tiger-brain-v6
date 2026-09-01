"""
Tiger Brain V6+V7 — Real Option-Chain History (KADAM 4, part 2)
=================================================================
Ab tak har option premium Black-Scholes se BANAYA jaata tha (IV = India
VIX). Uska sabse bada jhooth ye tha ki weekly ATM option ka asli IV
behaviour — event ke baad ka crush, expiry-day ka collapse, skew — VIX
se nahi aata. Isliye `--iv-crush-pct` ek ANUMAAN tha.

Ye module wo anumaan hatane ke liye hai: NFO ke ASLI option contract ki
candles Angel se laata hai, taaki entry aur exit dono par MARKET ka
premium use ho, koi model nahi.

Kaise kaam karta hai
--------------------
1. INSTRUMENT MASTER — Angel ka public scrip master (roz badalta hai)
   se NIFTY ke saare listed OPTIDX contracts (token, expiry, strike,
   CE/PE, lot size).
2. TOKEN REGISTRY — ⚠️ ye hissa sabse important hai. Angel ke master
   mein sirf ZINDA contracts hote hain; expire hote hi contract master
   se GAYAB ho jaata hai aur uska token dobara kabhi nahi milta. Isliye
   har refresh pe hum dekha hua har contract disk pe ek registry mein
   likh dete hain. Registry jitni purani hogi, utna peeche tak real
   pricing possible hogi — isliye ise roz chalana chahiye:

       python3 -m data.option_chain --refresh      # cron/scheduler se roz

3. CANDLES — resolve hue token ki candles `data/intraday.py` wali
   cache-first machinery se aati hain (wahi rate-limit backoff, wahi
   session cleaning).

⚠️ HONESTY NOTES:
  - Jo contract registry mein nahi hai (yani jo tumhare pehle refresh
    se PEHLE expire ho chuka tha), uska real premium ab kabhi nahi
    milega. Ye module us trade ko chupchaap model-price nahi karta —
    wo "miss" ginta hai aur coverage report mein dikhata hai.
  - Ye premium hai, IV/OI nahi. Real bid-ask spread bhi nahi (candles
    traded price hain), isliye slippage assumption abhi bhi zinda hai.
  - Illiquid strikes pe candle ho hi nahi sakti — wo bhi miss ginta hai,
    aur ye asli baat hai: agar market mein trade nahi hua to backtest
    ko bhi wahan trade nahi maanna chahiye.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, time

import pandas as pd

try:
    from data.intraday import (
        DEFAULT_CACHE_DIR,
        INTRADAY_INTERVAL_MINUTES,
        load_intraday,
        now_ist,
    )
except ImportError:
    raise ImportError("Repo ROOT se chalao: python3 -m data.option_chain")

logger = logging.getLogger("tiger_brain.data.option_chain")
logging.basicConfig(level=logging.INFO)


SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/"
    "OpenAPIScripMaster.json"
)
MASTER_TIMEOUT_SECONDS = 120
DEFAULT_UNDERLYING = "NIFTY"
DEFAULT_EXCHANGE = "NFO"
OPTION_INSTRUMENT_TYPE = "OPTIDX"
# Angel master mein strike paise mein hota hai (2215000 = 22150)
STRIKE_DIVISOR = 100.0
EXPIRY_FORMAT = "%d%b%Y"
# Contract listing se pehle koi candle hoti nahi, par listing date registry
# mein nahi hoti — isliye expiry se itna peeche tak maang lete hain
MAX_CONTRACT_HISTORY_DAYS = 45
# Us din ka "current weekly" isse zyada door nahi ho sakta. Jab asli weekly
# expire ho kar registry se pehle hi gayab ho chuki ho, to agli zinda expiry
# utha lena ek ALAG contract (zyada DTE, kam theta) pe P&L banata hai —
# isliye use miss maanana hi imaandari hai
MAX_EXPIRY_GAP_DAYS = 8


def registry_path(
    underlying: str = DEFAULT_UNDERLYING,
    exchange: str = DEFAULT_EXCHANGE,
    cache_dir: str = DEFAULT_CACHE_DIR,
) -> str:
    name = f"option_registry_{underlying.upper()}_{exchange.upper()}.json"
    return os.path.join(cache_dir, name)


def parse_expiry(raw: str) -> date:
    """Angel ka '08SEP2026' → date."""
    return datetime.strptime(raw.upper(), EXPIRY_FORMAT).date()


def fetch_scrip_master(url: str = SCRIP_MASTER_URL) -> list:
    """Angel ka public instrument master (koi login nahi chahiye)."""
    import urllib.request

    logger.info("Angel scrip master download ho raha hai…")
    with urllib.request.urlopen(url, timeout=MASTER_TIMEOUT_SECONDS) as response:
        return json.load(response)


def extract_option_contracts(
    master: list,
    underlying: str = DEFAULT_UNDERLYING,
    exchange: str = DEFAULT_EXCHANGE,
) -> dict:
    """Master ke rows ko {symbol: contract-dict} mein badalta hai."""
    contracts = {}
    for row in master:
        if (
            row.get("name") != underlying.upper()
            or row.get("exch_seg") != exchange.upper()
            or row.get("instrumenttype") != OPTION_INSTRUMENT_TYPE
        ):
            continue
        symbol = row.get("symbol", "")
        option_type = symbol[-2:]
        if option_type not in ("CE", "PE"):
            continue
        contracts[symbol] = {
            "symbol": symbol,
            "token": str(row["token"]),
            "expiry": parse_expiry(row["expiry"]).isoformat(),
            "strike": float(row["strike"]) / STRIKE_DIVISOR,
            "option_type": option_type,
            "lot_size": int(float(row.get("lotsize", 0) or 0)),
        }
    return contracts


def load_registry(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path) as handle:
        return json.load(handle)


def save_registry(registry: dict, path: str) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as handle:
        json.dump(registry, handle, indent=1, sort_keys=True)
    return path


def refresh_registry(
    underlying: str = DEFAULT_UNDERLYING,
    exchange: str = DEFAULT_EXCHANGE,
    cache_dir: str = DEFAULT_CACHE_DIR,
    master: list | None = None,
) -> dict:
    """
    Aaj ke live contracts registry mein jodta hai (purane kabhi hataye
    nahi jaate — wahi to expire hone ke baad kaam aate hain).

    Returns: poori registry {symbol: contract}
    """
    path = registry_path(underlying, exchange, cache_dir)
    registry = load_registry(path)
    live = extract_option_contracts(
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
        f"Registry update: {added} naye contracts, total {len(registry)} "
        f"({path})"
    )
    return registry


def registry_expiries(registry: dict) -> list:
    """Registry mein maujood saari expiry dates (sorted)."""
    return sorted({date.fromisoformat(c["expiry"]) for c in registry.values()})


def registry_coverage_start(registry: dict) -> date | None:
    """
    Pehla din jab se registry par bharosa kiya ja sakta hai.

    Har refresh us waqt ke SAARE zinda contracts likhta hai, isliye pehle
    refresh (`first_seen`) ke baad ka har din poora cover hai. Usse pehle
    ki weekly registry mein hai hi nahi — aur uska ye matlab nahi ki wo
    thi hi nahi.
    """
    seen = [
        date.fromisoformat(c["first_seen"])
        for c in registry.values()
        if c.get("first_seen")
    ]
    return min(seen) if seen else None


def nearest_expiry_on_or_after(registry: dict, day: date) -> date | None:
    """Us din ke liye 'current weekly' — pehli expiry jo us din ya baad mein ho."""
    for expiry in registry_expiries(registry):
        if expiry >= day:
            return expiry
    return None


def find_contract(
    registry: dict, expiry: date, strike: float, option_type: str
) -> dict | None:
    for contract in registry.values():
        if (
            contract["option_type"] == option_type
            and contract["expiry"] == expiry.isoformat()
            and abs(contract["strike"] - strike) < 1e-6
        ):
            return contract
    return None


class AngelOptionChain:
    """
    Real option premiums ka provider — `backtest.options_sim` isse
    poochta hai "is timestamp pe is strike ka bhaav kya tha?".

    Har contract ki candles ek baar load hoti hain (disk cache + memory),
    aur jo nahi milta wo `misses` mein reason ke saath ginta hai.
    """

    def __init__(
        self,
        broker=None,
        underlying: str = DEFAULT_UNDERLYING,
        exchange: str = DEFAULT_EXCHANGE,
        interval: str = "FIVE_MINUTE",
        cache_dir: str = DEFAULT_CACHE_DIR,
        offline: bool = False,
        registry: dict | None = None,
        max_expiry_gap_days: int = MAX_EXPIRY_GAP_DAYS,
        coverage_start: date | None = None,
    ):
        if interval not in INTRADAY_INTERVAL_MINUTES:
            raise ValueError(
                f"Interval '{interval}' support nahi hai. "
                f"Chalega: {sorted(INTRADAY_INTERVAL_MINUTES)}"
            )
        self.broker = broker
        self.underlying = underlying.upper()
        self.exchange = exchange.upper()
        self.interval = interval
        self.cache_dir = cache_dir
        self.offline = offline
        self.max_expiry_gap_days = max_expiry_gap_days
        self.registry = (
            registry if registry is not None
            else load_registry(registry_path(underlying, exchange, cache_dir))
        )
        self.coverage_start = (
            coverage_start if coverage_start is not None
            else registry_coverage_start(self.registry)
        )
        self._candles = {}
        self.hits = 0
        self.misses = {
            "no_contract": 0,   # expire ho chuka tha / listed hi nahi tha
            "no_candles": 0,    # contract mila par uski candle history nahi
            "no_bar": 0,        # history hai par us bar pe trade nahi hua
        }

    # --- candles ---

    def _ensure_broker(self):
        """Ek hi login sab contracts ke liye — har fetch pe naya login nahi."""
        if self.offline or self.broker is not None:
            return self.broker
        from broker.angel_connect import AngelBroker

        self.broker = AngelBroker()
        self.broker.login()
        return self.broker

    def _load_contract_candles(self, contract: dict) -> pd.DataFrame:
        token = contract["token"]
        if token in self._candles:
            return self._candles[token]

        expiry = date.fromisoformat(contract["expiry"])
        # Contract listing se expiry tak hi zinda hota hai. Window ka end
        # expiry pe rokna zaroori hai — warna har run expiry ke baad ka
        # (roz badhta) khaali hissa dobara download karta rehta hai.
        expiry_end = datetime.combine(expiry, time.max)
        df = load_intraday(
            symbol=contract["symbol"],
            interval=self.interval,
            days=MAX_CONTRACT_HISTORY_DAYS,
            broker=self._ensure_broker(),
            symbol_token=token,
            exchange=self.exchange,
            cache_dir=self.cache_dir,
            offline=self.offline,
            end=expiry_end,
        )
        self._candles[token] = df
        return df

    def contract_for(self, timestamp, strike: float, option_type: str) -> dict | None:
        day = pd.Timestamp(timestamp).date()
        # Registry banne se pehle ke din: us din ki asli weekly kabhi dekhi
        # hi nahi gayi, isliye jo agli expiry registry mein hai wo ek ALAG
        # contract hai — uska bhaav "real" bata dena jhooth hoga
        if self.coverage_start is not None and day < self.coverage_start:
            return None
        expiry = nearest_expiry_on_or_after(self.registry, day)
        if expiry is None or (expiry - day).days > self.max_expiry_gap_days:
            return None
        return find_contract(self.registry, expiry, strike, option_type)

    def premium(self, timestamp, strike: float, option_type: str) -> float | None:
        """
        Us bar ka ASLI traded close. Na mile to None — model-price pe
        chupchaap fallback yahan JAAN-BOOJH kar nahi hota; caller ko
        pata hona chahiye ki number kahan se aaya.
        """
        contract = self.contract_for(timestamp, strike, option_type)
        if contract is None:
            self.misses["no_contract"] += 1
            return None

        candles = self._load_contract_candles(contract)
        if candles.empty:
            self.misses["no_candles"] += 1
            return None

        key = pd.Timestamp(timestamp)
        if key.tz is not None:
            key = key.tz_localize(None)
        if key not in candles.index:
            self.misses["no_bar"] += 1
            return None

        value = float(candles.loc[key, "close"])
        if value <= 0:
            self.misses["no_bar"] += 1
            return None
        self.hits += 1
        return value

    def coverage(self) -> dict:
        total = self.hits + sum(self.misses.values())
        return {
            "registry_contracts": len(self.registry),
            "coverage_start": (
                self.coverage_start.isoformat() if self.coverage_start else None
            ),
            "registry_expiries": [
                e.isoformat() for e in registry_expiries(self.registry)
            ],
            "lookups": total,
            "hits": self.hits,
            "hit_rate_pct": round(self.hits / total * 100, 1) if total else 0.0,
            "misses": dict(self.misses),
        }



def print_coverage_report(coverage: dict) -> None:
    print("\n" + "=" * 66)
    print("REAL OPTION-CHAIN COVERAGE")
    print("=" * 66)
    print(f"Registry contracts : {coverage['registry_contracts']}")
    print(f"Registry shuru se  : {coverage.get('coverage_start') or 'pata nahi'}")
    print(f"Premium lookups    : {coverage['lookups']}")
    print(f"Real bhaav mila    : {coverage['hits']} ({coverage['hit_rate_pct']}%)")
    misses = coverage["misses"]
    print(
        f"Miss — contract hi nahi : {misses['no_contract']} "
        "(expire ho chuka tha, token registry se pehle)"
    )
    print(f"Miss — candle history nahi: {misses['no_candles']}")
    print(f"Miss — us bar pe trade nahi: {misses['no_bar']}")
    if misses["no_contract"]:
        print(
            "\n⚠️ Angel ke master mein sirf ZINDA contracts hote hain. Jo "
            "expiry tumhare pehle `--refresh` se pehle nikal gayi, uska "
            "real bhaav ab kabhi nahi milega — registry roz refresh karo, "
            "aage ka data apne aap banta jaayega."
        )
    print("=" * 66)


# ============================================================
# CLI — python3 -m data.option_chain --refresh
# ============================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m data.option_chain",
        description="Option contract registry refresh + coverage check",
    )
    parser.add_argument(
        "--refresh", action="store_true",
        help="Angel master download karke registry update karo (roz chalao)",
    )
    parser.add_argument("--underlying", default=DEFAULT_UNDERLYING)
    parser.add_argument("--exchange", default=DEFAULT_EXCHANGE)
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.refresh:
        # Ek hi master download dono registries ke liye — futures ke tokens
        # bhi expire hone par master se gayab ho jaate hain, isliye unhe bhi
        # isi daily refresh mein pakadna zaroori hai
        from data.derivatives import refresh_futures_registry

        master = fetch_scrip_master()
        registry = refresh_registry(
            args.underlying, args.exchange, args.cache_dir, master=master
        )
        refresh_futures_registry(
            args.underlying, args.exchange, args.cache_dir, master=master
        )
    else:
        registry = load_registry(
            registry_path(args.underlying, args.exchange, args.cache_dir)
        )
        if not registry:
            print(
                "Registry khaali hai — pehle chalao: "
                "python3 -m data.option_chain --refresh"
            )
            return 1

    expiries = registry_expiries(registry)
    print("\n" + "=" * 66)
    print(f"OPTION REGISTRY — {args.underlying} ({args.exchange})")
    print("=" * 66)
    print(f"Contracts : {len(registry)}")
    print(f"Expiries  : {len(expiries)}")
    if expiries:
        print(f"Range     : {expiries[0]} → {expiries[-1]}")
        oldest = min(c.get("first_seen", "") for c in registry.values())
        print(f"Sabse purana record: {oldest or 'unknown'}")
        print(
            "\nIsse pehle ki expiries ka real bhaav nahi mil sakta — "
            "registry roz refresh hoti rahegi to ye window barhti jaayegi."
        )
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
