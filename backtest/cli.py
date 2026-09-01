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

    # directional accuracy ke saath simulated options P&L bhi
    python3 -m backtest.cli --source csv --csv-path data/nifty_2y.csv --options-pnl

⚠️ Angel One ONE_DAY data max ~2000 din deta hai, isliye --years 2
aaram se milta hai. VIX (regime classifier ke liye) Yahoo Finance se
aata hai; agar wo fail ho jaaye to backtest phir bhi chalega — bas
regime classification VIX-based rules ke bina thoda kamzor hoga, aur
report mein ye batata hai.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta

import pandas as pd

try:
    from backtest.engine import (
        MIN_WARMUP_DAYS,
        merge_gate_stats,
        new_gate_stats,
        print_backtest_report,
        run_backtest_with_split,
    )
    from backtest.options_sim import (
        DEFAULT_EXPIRY_WEEKDAY,
        DEFAULT_LOT_SIZE,
        DEFAULT_STRIKE_STEP,
        print_options_report,
        simulate_trade_log,
    )
    from backtest.walk_forward import (
        DEFAULT_TEST_DAYS,
        DEFAULT_TRAIN_DAYS,
        print_walk_forward_report,
        run_walk_forward,
    )
    from config.thresholds import DECISION_SCORE_THRESHOLD, PIPELINE
except ImportError:
    raise ImportError("Repo ROOT se chalao: python3 -m backtest.cli")

logger = logging.getLogger("tiger_brain.backtest.cli")
logging.basicConfig(level=logging.INFO)


NIFTY_SPOT_TOKEN = "99926000"
OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]

DAILY_INTERVAL = "ONE_DAY"
SESSION_MINUTES = 375  # 09:15 se 15:30 tak
INTRADAY_INTERVALS = {
    "ONE_MINUTE": 1,
    "THREE_MINUTE": 3,
    "FIVE_MINUTE": 5,
    "TEN_MINUTE": 10,
    "FIFTEEN_MINUTE": 15,
    "THIRTY_MINUTE": 30,
    "ONE_HOUR": 60,
}


# Intraday run ka data chhota hota hai — tab walk-forward windows bhi
# dino mein chhoti chahiye, warna ek bhi fold nahi banta
INTRADAY_TRAIN_DAYS = 10
INTRADAY_TEST_DAYS = 3


def bars_per_session(interval: str) -> int:
    """Ek trading din mein is interval ke kitne bars aate hain."""
    if interval == DAILY_INTERVAL:
        return 1
    return max(SESSION_MINUTES // INTRADAY_INTERVALS[interval], 1)


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


def load_intraday_from_angel(
    token: str, exchange: str, interval: str, days: int, cache_dir: str
) -> pd.DataFrame:
    """Intraday candles — cache-first, missing hissa Angel se (data.intraday)."""
    from broker.angel_connect import AngelBroker
    from data.intraday import load_intraday

    broker = AngelBroker()
    broker.login()

    return load_intraday(
        interval=interval, days=days, broker=broker,
        symbol_token=token, exchange=exchange, cache_dir=cache_dir,
    )


def load_vix(df: pd.DataFrame, intraday: bool = False) -> pd.Series | None:
    """
    India VIX ko price data ke index pe align karta hai (regime classifier
    ke liye). Fail ho jaye to None — backtest fir bhi chalega.

    Intraday par ek din ka VIX us din ka CLOSE hota hai — use 09:20 ke
    decision mein daalna lookahead hai, isliye tab har bar ko PICHHLE
    session ka VIX milta hai.
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
        if intraday:
            vix.index = vix.index + pd.Timedelta(days=1)
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
        "--interval", default=DAILY_INTERVAL,
        choices=[DAILY_INTERVAL, *sorted(INTRADAY_INTERVALS)],
        help="Candle interval. ONE_DAY (default) = purana daily backtest; "
             "baaki sab intraday — tab ek din mein kai decisions bante hain "
             "aur position overnight nahi rakhi jaati.",
    )
    parser.add_argument(
        "--intraday-days", type=int, default=30,
        help="Intraday interval ke saath kitne din ka data (default: 30). "
             "Angel ki per-interval limit yaad rahe (1-min = 30 din).",
    )
    parser.add_argument(
        "--cache-dir", default="data_cache",
        help="Intraday candles ka local cache (default: data_cache)",
    )
    parser.add_argument(
        "--warmup-bars", type=int, default=None,
        help="Decision se pehle kitne bars ki history chahiye (default: daily "
             "pe 30, intraday pe ek poora session)",
    )
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
    parser.add_argument(
        "--score-threshold", type=float, default=DECISION_SCORE_THRESHOLD,
        help="Meta-Brain ka BUY/SELL cutoff (default: "
             f"{DECISION_SCORE_THRESHOLD}) — sensitivity analysis ke liye",
    )
    parser.add_argument(
        "--stage1-min", type=float, default=PIPELINE["STAGE1_MIN_CONFIDENCE"],
        help=f"Stage-1 pass cutoff (default: {PIPELINE['STAGE1_MIN_CONFIDENCE']})",
    )
    parser.add_argument(
        "--diagnose", action="store_true",
        help="Gate diagnostics chhapo — kaun sa gate kitne din block kar raha hai",
    )
    parser.add_argument(
        "--options-pnl", action="store_true",
        help="Directional decisions ko simulated ATM option trades mein badalke "
             "rupee P&L bhi nikalo (theta + costs shaamil)",
    )
    parser.add_argument(
        "--lot-size", type=int, default=DEFAULT_LOT_SIZE,
        help=f"--options-pnl ke liye lot size (default NIFTY: {DEFAULT_LOT_SIZE})",
    )
    parser.add_argument(
        "--strike-step", type=int, default=DEFAULT_STRIKE_STEP,
        help=f"ATM strike rounding step (default: {DEFAULT_STRIKE_STEP})",
    )
    parser.add_argument(
        "--expiry-weekday", type=int, default=DEFAULT_EXPIRY_WEEKDAY,
        help="Weekly expiry ka weekday (0=Mon ... 3=Thu, default: 3)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Backtest chalne se PEHLE validate karo — warna galat value pe crash
    # poore run ke baad aata hai
    if args.lot_size <= 0:
        parser.error("--lot-size 0 se bada hona chahiye")
    if args.strike_step <= 0:
        parser.error("--strike-step 0 se bada hona chahiye")
    if not 0 <= args.expiry_weekday <= 6:
        parser.error("--expiry-weekday 0 (Mon) se 6 (Sun) ke beech hona chahiye")
    if args.intraday_days <= 0:
        parser.error("--intraday-days 0 se bada hona chahiye")
    if args.warmup_bars is not None and args.warmup_bars < 1:
        parser.error("--warmup-bars kam se kam 1 hona chahiye")

    intraday = args.interval != DAILY_INTERVAL
    bars_per_day = bars_per_session(args.interval)
    warmup_bars = args.warmup_bars or (bars_per_day if intraday else MIN_WARMUP_DAYS)

    # Walk-forward windows din mein hain. Intraday run ka data hi kuch
    # hafton ka hota hai (Angel ki per-interval limit), isliye 250/60 din
    # ke daily defaults pe ek bhi fold nahi banta.
    train_days, test_days = args.train_days, args.test_days
    if intraday:
        if train_days == DEFAULT_TRAIN_DAYS:
            train_days = INTRADAY_TRAIN_DAYS
        if test_days == DEFAULT_TEST_DAYS:
            test_days = INTRADAY_TEST_DAYS

    if args.source == "csv":
        if not args.csv_path:
            print("ERROR: --source csv ke saath --csv-path dena zaroori hai.")
            return 2
        df = load_from_csv(args.csv_path)
    elif intraday:
        df = load_intraday_from_angel(
            args.symbol_token, args.exchange, args.interval,
            args.intraday_days, args.cache_dir,
        )
        if args.save_csv and not df.empty:
            df.to_csv(args.save_csv)
            print(f"Data save ho gaya: {args.save_csv}")
    else:
        df = load_from_angel(args.symbol_token, args.exchange, args.years)
        if args.save_csv and not df.empty:
            df.to_csv(args.save_csv)
            print(f"Data save ho gaya: {args.save_csv}")

    if df.empty:
        print("ERROR: Data khali aaya — backtest nahi chal sakta.")
        return 1

    unit = "bars" if intraday else "din"
    print(f"\nTotal {len(df)} {unit} ka data ({df.index[0]} se {df.index[-1]} tak).")
    if intraday:
        print(
            f"Interval {args.interval}: {bars_per_day} bars/din, warmup "
            f"{warmup_bars} bars. Har session ka aakhri bar skip hota hai "
            "(position overnight nahi rakhi jaati)."
        )

    vix = None if args.no_vix else load_vix(df, intraday=intraday)
    if vix is None:
        print(
            "⚠️ India VIX data nahi hai — regime classification ke VIX-based "
            "rules (High Vol / Low Vol / Vol Shock, Section 17) skip honge. "
            "Result thoda kam bharose ka hai."
        )

    if args.mode == "walkforward":
        results = run_walk_forward(
            df, vix_series=vix, train_days=train_days,
            test_days=test_days, anchored=args.anchored,
            score_threshold=args.score_threshold, stage1_min=args.stage1_min,
            bars_per_day=bars_per_day, warmup_bars=warmup_bars,
            session_aware=intraday,
        )
        print_walk_forward_report(results)
    else:
        results = run_backtest_with_split(
            df, vix, in_sample_pct=args.in_sample_pct,
            score_threshold=args.score_threshold, stage1_min=args.stage1_min,
            warmup_bars=warmup_bars, session_aware=intraday,
        )
        print_backtest_report(results)

    if args.diagnose:
        print_gate_diagnostics(_collect_gate_stats(results, args.mode), args)

    if args.options_pnl:
        _run_options_sim(df, results, vix, args)

    return 0


def _collect_trade_log(results: dict, mode: str) -> list:
    """Dono modes ke result-shapes se ek flat trade log banata hai."""
    if mode == "walkforward":
        return [t for fold in results["folds"] for t in fold["trade_log"]]
    # split mode: sirf OUT-OF-SAMPLE trades — in-sample P&L pe bharosa nahi
    return list(results["out_of_sample"]["trade_log"])


def _collect_gate_stats(results: dict, mode: str) -> dict:
    """Dono modes ke result-shapes se ek hi gate-stats dict."""
    if mode == "walkforward":
        return results["gate_stats"]
    merged = new_gate_stats()
    merge_gate_stats(merged, results["in_sample"]["gate_stats"])
    merge_gate_stats(merged, results["out_of_sample"]["gate_stats"])
    return merged


def print_gate_diagnostics(stats: dict, args) -> None:
    """
    Kaun sa gate signals rok raha hai — bina iske threshold badalna
    andhera mein teer chalana hai.
    """
    print("\n" + "=" * 66)
    print("GATE DIAGNOSTICS (kaun kis wajah se rok raha hai)")
    print("=" * 66)
    print(f"Din evaluate hue      : {stats['days']}")
    print(f"Score threshold        : {args.score_threshold}")

    if not stats["days"]:
        print("Koi din evaluate nahi hua — diagnostics khaali.")
        return

    print("\n--- Regime distribution ---")
    for regime, count in stats["regimes"].most_common():
        print(f"  {regime:<14} {count:>5} din ({count / stats['days'] * 100:.1f}%)")

    print("\n--- Final decisions ---")
    for decision, count in stats["decisions"].most_common():
        print(f"  {decision:<14} {count:>5}")

    print("\n--- NO_TRADE kis gate pe ruka ---")
    for reason, count in stats["blocked_by"].most_common():
        print(f"  {reason:<24} {count:>5}")

    scores = pd.Series(stats["scores"])
    print("\n--- Meta-Brain score distribution ---")
    print(
        f"  median {scores.median():.1f} | p90 {scores.quantile(0.9):.1f} | "
        f"max {scores.max():.1f}"
    )
    if scores.max() < args.score_threshold:
        print(
            f"  ⚠️ Max score ({scores.max():.1f}) threshold "
            f"({args.score_threshold}) se neeche hai — ye gate structurally "
            "band hai, tuning se pehle iski wajah dekho."
        )

    print("\n--- Sub-brain votes (max confidence ke saath) ---")
    for brain in sorted(stats["brain_max_confidence"]):
        votes = {
            key.split(":")[1]: count
            for key, count in stats["brain_votes"].items()
            if key.startswith(f"{brain}:")
        }
        print(
            f"  {brain:<15} max_conf {stats['brain_max_confidence'][brain]:>5.1f} | "
            f"{votes}"
        )
    print("=" * 66)


def _run_options_sim(df: pd.DataFrame, results: dict, vix, args) -> None:
    trade_log = _collect_trade_log(results, args.mode)
    if args.mode == "split":
        print("(Options P&L sirf OUT-OF-SAMPLE trades pe — in-sample pe nahi.)")

    sim = simulate_trade_log(
        df, trade_log, vix_series=vix, lot_size=args.lot_size,
        strike_step=args.strike_step, expiry_weekday=args.expiry_weekday,
    )
    print_options_report(sim, lot_size=args.lot_size)


if __name__ == "__main__":
    sys.exit(main())
