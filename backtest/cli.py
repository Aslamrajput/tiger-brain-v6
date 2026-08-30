"""
Tiger Brain V6+V7 — Backtest CLI (Phase 2)
=============================================
Ek hi command se 1-2 saal ka data lekar backtest chalane ke liye. Do
modes hain:

  * walkforward (default) — kai sequential unseen test windows
    (backtest/walk_forward.py). Ye Section 29 ka "walk-forward
    validation" wala requirement hai.
  * split — ek hi in-sample/out-of-sample split (backtest/engine.py).

Data do jagah se aa sakta hai:

  * --source angel  → real Angel One historical candles (login zaroori,
    .env mein credentials chahiye)
  * --source csv --csv-path <file>  → koi bhi saved OHLCV CSV. Isse
    backtest bina broker login ke, offline bhi chalta hai — aur ek hi
    data snapshot pe baar-baar reproducible run milta hai.

Examples (repo ROOT se):

    # 2 saal NIFTY daily, Angel One se, walk-forward
    python3 -m backtest.cli --years 2

    # pehle data save karo, phir usi snapshot pe baar-baar chalao
    python3 -m backtest.cli --years 2 --save-csv data/nifty_2y.csv
    python3 -m backtest.cli --source csv --csv-path data/nifty_2y.csv

    # window sizes badalke stability check
    python3 -m backtest.cli --source csv --csv-path data/nifty_2y.csv \\
        --train-days 250 --test-days 40 --anchored

⚠️ Angel One ONE_DAY data max ~2000 din deta hai, isliye --years 2
aaram se milta hai. VIX (regime classifier ke liye) Yahoo Finance se
aata hai; agar wo fail ho jaaye to backtest phir bhi chalega — bas
regime classification VIX-based rules ke bina thoda kamzor hoga, aur
report mein ye batata hai.
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta

import pandas as pd

try:
    from backtest.engine import print_backtest_report, run_backtest_with_split
    from backtest.walk_forward import (
        DEFAULT_TEST_DAYS,
        DEFAULT_TRAIN_DAYS,
        print_walk_forward_report,
        run_walk_forward,
    )
except ImportError:
    raise ImportError("Repo ROOT se chalao: python3 -m backtest.cli")

logger = logging.getLogger("tiger_brain.backtest.cli")
logging.basicConfig(level=logging.INFO)


NIFTY_SPOT_TOKEN = "99926000"
OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


def load_from_csv(path: str) -> pd.DataFrame:
    """Saved OHLCV CSV padhta hai (pehla column = timestamp index)."""
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df.columns = [c.lower() for c in df.columns]

    missing = [c for c in OHLCV_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"CSV mein ye columns nahi mile: {missing}. Chahiye: {OHLCV_COLUMNS} "
            f"(pehla column timestamp index hona chahiye)."
        )

    return df[OHLCV_COLUMNS].sort_index()


def load_from_angel(token: str, exchange: str, years: float) -> pd.DataFrame:
    """Angel One se daily candles — login karke, chunking loader handle karta hai."""
    from broker.angel_connect import AngelBroker
    from data.loader import fetch_angel_historical_candles

    broker = AngelBroker()
    broker.login()

    end = datetime.now()
    start = end - timedelta(days=int(years * 365))

    logger.info(f"Angel One se data: {start.date()} se {end.date()} tak")
    return fetch_angel_historical_candles(
        broker, exchange, token, "ONE_DAY", start, end
    )


def load_vix(df: pd.DataFrame) -> pd.Series | None:
    """
    India VIX ko price data ke index pe align karta hai (regime classifier
    ke liye). Fail ho jaye to None — backtest fir bhi chalega.
    """
    try:
        from data.loader import fetch_india_vix_history

        vix_df = fetch_india_vix_history(days_back=len(df) * 2)
        if vix_df is None or vix_df.empty:
            return None

        close_col = "Close" if "Close" in vix_df.columns else vix_df.columns[0]
        vix = vix_df[close_col]
        if isinstance(vix, pd.DataFrame):  # yfinance multi-index columns
            vix = vix.iloc[:, 0]

        vix.index = pd.to_datetime(vix.index).tz_localize(None)
        price_index = pd.to_datetime(df.index).tz_localize(None)
        aligned = vix.reindex(price_index, method="ffill")

        # Purane saved dataset pe VIX (jo sirf recent din deta hai) price
        # dates ko cover hi nahi karta — chup-chaap NaN series dene se
        # VIX rules bina bataye gayab ho jaate hain.
        coverage = float(aligned.notna().mean())
        if coverage == 0:
            logger.warning(
                "India VIX aapke price dates ko bilkul cover nahi karta "
                f"({price_index.min().date()} se {price_index.max().date()}) "
                "— VIX ke bina chala rahe hain."
            )
            return None
        if coverage < 0.9:
            logger.warning(
                f"India VIX sirf {coverage:.0%} dino ko cover karta hai — "
                "baaki dino pe VIX-based regime rules skip honge."
            )
        return aligned
    except Exception as exc:
        logger.warning(
            f"India VIX load nahi hua ({exc}) — VIX ke bina chala rahe hain."
        )
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m backtest.cli",
        description="Tiger Brain backtest runner (walk-forward ya single split)",
    )
    parser.add_argument(
        "--mode", choices=["walkforward", "split"], default="walkforward",
        help="walkforward = kai unseen test windows (default), split = ek hi split",
    )
    parser.add_argument(
        "--source", choices=["angel", "csv"], default="angel",
        help="Data kahan se aaye (default: angel)",
    )
    parser.add_argument("--csv-path", help="--source csv ke saath: CSV file ka path")
    parser.add_argument(
        "--save-csv",
        help="Angel se aaya data yahan save karo (baar-baar reproducible run ke liye)",
    )
    parser.add_argument(
        "--years", type=float, default=2.0,
        help="Kitne saal ka data Angel se maangna hai (default: 2)",
    )
    parser.add_argument(
        "--symbol-token", default=NIFTY_SPOT_TOKEN,
        help=f"Angel instrument token (default: NIFTY spot {NIFTY_SPOT_TOKEN})",
    )
    parser.add_argument("--exchange", default="NSE", help="NSE ya NFO (default: NSE)")
    parser.add_argument(
        "--train-days", type=int, default=DEFAULT_TRAIN_DAYS,
        help=f"Walk-forward train/history window (default: {DEFAULT_TRAIN_DAYS})",
    )
    parser.add_argument(
        "--test-days", type=int, default=DEFAULT_TEST_DAYS,
        help=f"Walk-forward unseen test window (default: {DEFAULT_TEST_DAYS})",
    )
    parser.add_argument(
        "--anchored", action="store_true",
        help="Train window expanding rakho (default: rolling)",
    )
    parser.add_argument(
        "--in-sample-pct", type=float, default=60,
        help="--mode split ke liye: kitna %% data in-sample (default: 60)",
    )
    parser.add_argument(
        "--no-vix", action="store_true", help="India VIX fetch mat karo"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.source == "csv":
        if not args.csv_path:
            print("ERROR: --source csv ke saath --csv-path dena zaroori hai.")
            return 2
        df = load_from_csv(args.csv_path)
    else:
        df = load_from_angel(args.symbol_token, args.exchange, args.years)
        if args.save_csv and not df.empty:
            df.to_csv(args.save_csv)
            print(f"Data save ho gaya: {args.save_csv}")

    if df.empty:
        print("ERROR: Data khali aaya — backtest nahi chal sakta.")
        return 1

    print(f"\nTotal {len(df)} din ka data ({df.index[0]} se {df.index[-1]} tak).")

    vix = None if args.no_vix else load_vix(df)
    if vix is None:
        print(
            "⚠️ India VIX data nahi hai — regime classification ke VIX-based "
            "rules (High Vol / Low Vol / Vol Shock, Section 17) skip honge. "
            "Result thoda kam bharose ka hai."
        )

    if args.mode == "walkforward":
        results = run_walk_forward(
            df, vix_series=vix, train_days=args.train_days,
            test_days=args.test_days, anchored=args.anchored,
        )
        print_walk_forward_report(results)
    else:
        results = run_backtest_with_split(df, vix, in_sample_pct=args.in_sample_pct)
        print_backtest_report(results)

    return 0


if __name__ == "__main__":
    sys.exit(main())
