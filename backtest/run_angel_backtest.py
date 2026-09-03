"""
V6.6 Angel One Backtest Runner
==============================
Runs the EXACT V6.6 strategy engine (run_intraday_backtest) with real
historical OHLCV data pulled directly from Angel One SmartAPI instead of
yfinance. Angel One returns IST-native timestamps and REAL index volume
for NIFTY/BANKNIFTY, so the volume_delta 1.8x spike confirmation runs at
full strength (the yfinance zero-volume fallback becomes unnecessary).

Strategy logic is NOT modified — only the DATA SOURCE changes.
Credentials are read from .env via AngelBroker (never hardcoded).

Run from repo ROOT:
    python3 -m backtest.run_angel_backtest

Requires: .env with ANGEL_CLIENT_ID, ANGEL_MPIN, ANGEL_TOTP_SECRET,
          ANGEL_API_KEY (already present on the EC2 server).
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, ".")

from broker.angel_connect import AngelBroker
from data.loader import (
    fetch_angel_historical_candles,
    find_symbol_token,
    load_angel_instrument_master,
)
from backtest.intraday_backtest import run_intraday_backtest, print_report, _normalize_cols
from universe.fno_universe import (
    INDEX_SYMBOLS, STOCK_SYMBOLS, COMMODITY_SYMBOLS,
    UNIVERSE, all_symbols, segment_of,
)

logger = logging.getLogger("tiger_brain.angel_backtest")
logging.basicConfig(level=logging.INFO)

# Angel One exchange + symbol-name mapping for our universe.
# Indices are spot on NSE; stocks are NSE; commodities are MCX.
ANGEL_EXCHANGE = {
    "NIFTY": ("NSE", "Nifty 50"),
    "BANKNIFTY": ("NSE", "Nifty Bank"),
    "CRUDEOIL": ("MCX", "CRUDEOIL"),
    "GOLD": ("MCX", "GOLD"),
    "NATURALGAS": ("MCX", "NATURALGAS"),
}


def _resolve_symbol_token(broker, symbol: str) -> tuple[str, str] | None:
    """Map a universe symbol to (exchange, symboltoken) via the Angel
    instrument master. Stocks default to NSE with their own name."""
    if symbol in ANGEL_EXCHANGE:
        exchange, search = ANGEL_EXCHANGE[symbol]
    else:
        exchange, search = "NSE", symbol

    try:
        matches = find_symbol_token(exchange, search)
    except Exception as exc:
        logger.error(f"{symbol}: instrument master lookup failed: {exc}")
        return None

    if matches is None or matches.empty:
        logger.warning(f"{symbol}: no instrument match for '{search}' on {exchange}")
        return None

    # For indices/commodities pick the first exact/contains match.
    # Prefer an exact symbol match, else fall back to the first row.
    exact = matches[matches["symbol"].str.upper() == search.upper()]
    row = exact.iloc[0] if not exact.empty else matches.iloc[0]
    token = str(row["token"])
    logger.info(f"{symbol}: {exchange} token={token} ({row['symbol']})")
    return exchange, token


def fetch_angel_data(broker, days_15m: int = 60, days_1m: int = 7):
    """Fetch 15m + 1m OHLCV for the full universe from Angel One."""
    to_date = datetime.now().replace(hour=15, minute=30, second=0, microsecond=0)
    from_15m = to_date - timedelta(days=days_15m)
    from_1m = to_date - timedelta(days=days_1m)

    data_map: dict[str, "pd.DataFrame"] = {}
    data_map_1m: dict[str, "pd.DataFrame"] = {}
    failed: list[str] = []

    syms = all_symbols()
    total = len(syms)
    for idx, (sym, _tk) in enumerate(syms.items(), 1):
        tag = f"[{idx}/{total}] {sym}"
        mapping = _resolve_symbol_token(broker, sym)
        if mapping is None:
            failed.append(sym)
            continue
        exchange, token = mapping

        # 15-minute candles
        try:
            d15 = fetch_angel_historical_candles(
                broker, exchange, token, "FIFTEEN_MINUTE", from_15m, to_date,
            )
            if d15 is not None and not d15.empty:
                data_map[sym] = _normalize_cols(d15)
            else:
                logger.warning(f"{tag}: no 15m data")
                failed.append(sym)
                continue
        except Exception as exc:
            logger.error(f"{tag}: 15m fetch error: {exc}")
            failed.append(sym)
            continue

        # 1-minute candles (last 7 days)
        try:
            d1 = fetch_angel_historical_candles(
                broker, exchange, token, "ONE_MINUTE", from_1m, to_date,
            )
            if d1 is not None and not d1.empty:
                data_map_1m[sym] = _normalize_cols(d1)
        except Exception as exc:
            logger.warning(f"{tag}: 1m fetch error: {exc}")

        print(f"  {tag:30s}: 15m={len(data_map[sym]):5d}  "
              f"1m={len(data_map_1m.get(sym, [])):5d}")
        time.sleep(0.5)  # gentle on the rate limit between symbols

    print(f"\nFailed symbols: {failed}")
    print(f"Universe loaded from Angel One: 15m={len(data_map)}  1m={len(data_map_1m)}")
    return data_map, data_map_1m, failed


def main():
    print("\n" + "#" * 72)
    print("#  TIGER BRAIN V6.6 — ANGEL ONE LIVE-DATA BACKTEST")
    print("#  (real OHLCV from Angel One SmartAPI, IST-native, real index volume)")
    print("#" * 72)

    # --- Load instrument master (needed for symbol→token) ---
    print("\nLoading Angel One instrument master...")
    try:
        load_angel_instrument_master()
        print("  ✓ Instrument master loaded.")
    except Exception as exc:
        print(f"  ✗ Instrument master load failed: {exc}")
        print("  Cannot proceed without it. Check network connectivity.")
        return

    # --- Login via AngelBroker (reads .env credentials automatically) ---
    print("\nLogging in to Angel One SmartAPI...")
    broker = AngelBroker()
    try:
        broker.login()
        print("  ✓ Angel One login successful.")
    except Exception as exc:
        print(f"  ✗ Angel One login failed: {exc}")
        print("  Check .env credentials (ANGEL_CLIENT_ID, ANGEL_MPIN, "
              "ANGEL_TOTP_SECRET, ANGEL_API_KEY).")
        return

    # --- Fetch historical data for the full universe ---
    print("\nFetching 15-min + 1-min intraday data from Angel One...")
    data_map, data_map_1m, failed = fetch_angel_data(broker)

    if not data_map:
        print("\n✗ No data fetched — cannot run backtest.")
        return

    # --- Run the V6.6 strategy engine (UNCHANGED) ---
    print(f"\nRunning COMBINED portfolio (₹1.5L, {len(data_map)} symbols)...")
    combined = run_intraday_backtest(
        data_map, start_capital=150000.0, max_loss_per_trade=2000.0,
        data_map_1m=data_map_1m if data_map_1m else None,
    )

    print("\n\n")
    print("#" * 72)
    print("#  PART 1 — COMBINED PORTFOLIO (V6.6 sniper, Angel One data)")
    print("#" * 72)
    print_report(combined)

    # Per-segment standalone
    for seg_key in ("index", "stock", "commodity"):
        seg_syms = list(UNIVERSE[seg_key]["symbols"].keys())
        seg_map = {s: data_map[s] for s in seg_syms if s in data_map}
        seg_map_1m = {s: data_map_1m[s] for s in seg_syms if s in data_map_1m}
        print("\n\n")
        print("#" * 72)
        print(f"#  PART 2 — {UNIVERSE[seg_key]['label'].upper()} (standalone, ₹1.5L)")
        print("#" * 72)
        if not seg_map:
            print("  (no data for this segment)")
            continue
        print(f"  Running {seg_key} standalone ({len(seg_map)} symbols)...")
        seg_res = run_intraday_backtest(
            seg_map, start_capital=150000.0, max_loss_per_trade=2000.0,
            data_map_1m=seg_map_1m if seg_map_1m else None,
        )
        print_report(seg_res)

    # --- Logout cleanly ---
    try:
        broker.logout()
        print("\n✓ Angel One session logged out.")
    except Exception:
        pass

    print("\n" + "=" * 72)
    print("  Angel One backtest complete.")
    print("=" * 72)


if __name__ == "__main__":
    main()
