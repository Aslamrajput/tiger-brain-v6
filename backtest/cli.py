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
        DEFAULT_IV_CRUSH_PCT,
        DEFAULT_LOT_SIZE,
        DEFAULT_STRIKE_STEP,
        crush_sensitivity,
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
        help=f"Decision se pehle kitne bars ki history chahiye (default: ek "
             f"poora session, par kam se kam {MIN_WARMUP_DAYS} bars)",
    )
    parser.add_argument(
        "--train-days", type=int, default=None,
        help=f"Walk-forward train/history window (default: {DEFAULT_TRAIN_DAYS} "
             f"daily pe, {INTRADAY_TRAIN_DAYS} intraday pe)",
    )
    parser.add_argument(
        "--test-days", type=int, default=None,
        help=f"Walk-forward unseen test window (default: {DEFAULT_TEST_DAYS} "
             f"daily pe, {INTRADAY_TEST_DAYS} intraday pe)",
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
    parser.add_argument(
        "--real-option-prices", action="store_true",
        help="Premium asli NFO option candles se lo (data/option_chain.py). "
             "Jis trade ke dono legs ka bhaav mil jaaye us par Black-Scholes "
             "aur IV-crush assumption dono hat jaate hain. Pehle chalao: "
             "python3 -m data.option_chain --refresh",
    )
    parser.add_argument(
        "--futures-volume", action="store_true",
        help="Volume NIFTY FUTURES candles se lo (index spot pe volume 0 "
             "aata hai, isliye volume-confirmation factor abhi mara hua "
             "hai). Price spot ka hi rehta hai — sirf volume badalta hai.",
    )
    parser.add_argument(
        "--futures-oi", action="store_true",
        help="Front futures ka getOIData la kar sub-brains ko OI buildup "
             "confirmation do (trend_follow + breakout ka OI factor).",
    )
    parser.add_argument(
        "--real-iv-series", action="store_true",
        help="Asli ATM option candles se IV series banao (reverse "
             "Black-Scholes) — Vol-Arb sub-brain isi ke bina soya rehta hai. "
             "Mehenga hai: har sample bar pe do option lookups.",
    )
    parser.add_argument(
        "--iv-sample-bars", type=int, default=1,
        help="IV har N-ve bar pe naapo (default 1). Beech ke bars pichhli "
             "ASLI reading carry karte hain — naya data nahi banta.",
    )
    parser.add_argument(
        "--iv-crush-pct", type=float, default=DEFAULT_IV_CRUSH_PCT,
        help="Exit IV pe % haircut (default: 0). VIX ka asli move to "
             "hamesha lagta hai; ye uske upar ka event/expiry crush hai. "
             "Report har run mein sensitivity table bhi chhapti hai.",
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
    if not 0 <= args.iv_crush_pct < 100:
        parser.error("--iv-crush-pct 0 se 100 ke beech hona chahiye")
    if args.intraday_days <= 0:
        parser.error("--intraday-days 0 se bada hona chahiye")
    if args.iv_sample_bars <= 0:
        parser.error("--iv-sample-bars 0 se bada hona chahiye")
    derivative_flags = (
        args.futures_volume or args.futures_oi or args.real_iv_series
    )
    if derivative_flags and args.interval == DAILY_INTERVAL:
        parser.error(
            "Derivative feeds (--futures-volume/--futures-oi/--real-iv-series) "
            "ke liye intraday --interval chahiye (jaise FIVE_MINUTE)"
        )
    # Option candles bhi session-time pe cleaned hoti hain; daily (00:00)
    # candles us filter mein bachti hi nahi
    if args.real_option_prices and args.interval == DAILY_INTERVAL:
        parser.error(
            "--real-option-prices ke liye intraday --interval chahiye "
            "(jaise FIVE_MINUTE)"
        )
    # Regime classifier ko kam se kam itni history chahiye, warna scanner
    # koi decision hi nahi deta
    if args.warmup_bars is not None and args.warmup_bars < MIN_WARMUP_DAYS:
        parser.error(f"--warmup-bars kam se kam {MIN_WARMUP_DAYS} hona chahiye")

    intraday = args.interval != DAILY_INTERVAL
    bars_per_day = bars_per_session(args.interval)
    warmup_bars = args.warmup_bars or max(bars_per_day, MIN_WARMUP_DAYS)

    # Walk-forward windows din mein hain. Intraday run ka data hi kuch
    # hafton ka hota hai (Angel ki per-interval limit), isliye 250/60 din
    # ke daily defaults pe ek bhi fold nahi banta.
    train_days = args.train_days
    test_days = args.test_days
    if train_days is None:
        train_days = INTRADAY_TRAIN_DAYS if intraday else DEFAULT_TRAIN_DAYS
    if test_days is None:
        test_days = INTRADAY_TEST_DAYS if intraday else DEFAULT_TEST_DAYS

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

    df, context = _build_derivative_context(df, args)

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
            session_aware=intraday, context=context,
        )
        print_walk_forward_report(results)
    else:
        results = run_backtest_with_split(
            df, vix, in_sample_pct=args.in_sample_pct,
            score_threshold=args.score_threshold, stage1_min=args.stage1_min,
            warmup_bars=warmup_bars, session_aware=intraday, context=context,
        )
        print_backtest_report(results)

    if context is not None:
        from data.derivatives import print_derivative_report

        print_derivative_report(context.summary())

    if args.diagnose:
        print_gate_diagnostics(_collect_gate_stats(results, args.mode), args)

    if args.options_pnl:
        _run_options_sim(df, results, vix, args)

    return 0


def _build_derivative_context(df: pd.DataFrame, args):
    """
    Futures volume + futures OI + asli ATM IV series ko ek context mein
    baandhta hai.

    Returns: (df, context) — df ka `volume` column futures se aa sakta
    hai, context `None` rehta hai agar koi bhi derivative feed maanga hi
    nahi gaya.

    ⚠️ Jo feed maanga par mila nahi, wo CHUP-CHAAP spot pe fallback NAHI
    hota — warning chhapti hai aur wo factor missing hi rehta hai.
    """
    if not (args.futures_volume or args.futures_oi or args.real_iv_series):
        return df, None

    from data.derivatives import (
        DerivativeContext,
        attach_futures_volume,
        contract_ids_for_index,
        futures_registry_path,
        load_futures_candles,
        load_futures_oi,
    )
    from data.option_chain import load_registry

    offline = args.source == "csv"
    broker = None if offline else _login_broker()
    registry = load_registry(
        futures_registry_path(exchange="NFO", cache_dir=args.cache_dir)
    )
    if not registry:
        print(
            "⚠️ Futures registry khaali hai — volume/OI feeds nahi mil sakte. "
            "Pehle chalao: python3 -m data.option_chain --refresh"
        )

    volume_coverage = {}
    if args.futures_volume:
        futures = load_futures_candles(
            df.index, interval=args.interval, broker=broker,
            cache_dir=args.cache_dir, offline=offline, registry=registry,
        )
        if futures.empty:
            print(
                "⚠️ Futures candles nahi mili — volume-confirmation factor is "
                "run mein bhi SKIP rahega (spot ka volume=0 hi hai). Ise "
                "'volume confirm ho gaya' mat samajhna."
            )
        else:
            df, volume_coverage = attach_futures_volume(df, futures)

    oi_series = None
    contract_ids = None
    if args.futures_oi:
        oi_series = load_futures_oi(
            df.index, interval=args.interval, broker=broker,
            cache_dir=args.cache_dir, offline=offline, registry=registry,
        )
        if oi_series.empty:
            print("⚠️ Futures OI nahi mili — OI factor har bar pe SKIP rahega.")
            oi_series = None
        elif registry:
            contract_ids = contract_ids_for_index(oi_series.index, registry)

    iv_frame = None
    if args.real_iv_series:
        iv_frame = _build_iv_frame(df, args, broker, offline)

    return df, DerivativeContext(
        oi_series=oi_series, iv_frame=iv_frame, contract_ids=contract_ids,
        volume_coverage=volume_coverage,
    )


def _login_broker():
    from broker.angel_connect import AngelBroker

    broker = AngelBroker()
    broker.login()
    return broker


def _build_iv_frame(df: pd.DataFrame, args, broker, offline: bool):
    """
    ATM IV series — asli option candles se. Iska provider trade-pricing
    wale provider se ALAG hai, warna IV lookups option-chain coverage
    report ko ganda kar dete (aur wo report trade pricing ki hai).
    """
    from data.iv_series import build_atm_iv_series
    from data.option_chain import AngelOptionChain, load_registry, registry_path

    registry = load_registry(registry_path(cache_dir=args.cache_dir))
    if not registry:
        print(
            "⚠️ Option registry khaali hai — IV series nahi ban sakti, "
            "Vol-Arb is run mein bhi soya rahega."
        )
        return None

    provider = AngelOptionChain(
        interval=args.interval, cache_dir=args.cache_dir, registry=registry,
        broker=broker, offline=offline,
    )
    iv_frame = build_atm_iv_series(
        df, provider, strike_step=args.strike_step,
        sample_every_bars=args.iv_sample_bars,
    )
    if iv_frame["atm_iv"].notna().sum() == 0:
        print(
            "⚠️ Ek bhi bar pe ATM option ka asli bhaav nahi mila — IV series "
            "khaali hai (registry itni purani nahi hai)."
        )
        return None
    return iv_frame


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


def print_crush_sensitivity(rows: list) -> None:
    """IV-crush assumption pe result kitna tikka hai, ye table dikhata hai."""
    print("\n" + "=" * 66)
    print("IV-CRUSH SENSITIVITY (wahi trades, alag crush assumptions)")
    print("=" * 66)
    print(f"{'crush %':>8} | {'net P&L':>14} | {'PF':>6} | {'win rate':>9}")
    print("-" * 66)
    for row in rows:
        print(
            f"{row['iv_crush_pct']:>8.0f} | ₹{row['total_pnl']:>13,.2f} | "
            f"{row['profit_factor']:>6} | {row['win_rate_pct']:>8}%"
        )
    print(
        "\nReal option-chain quotes ke bina crush ka asli number pata nahi. "
        "Result ko is range ki tarah padho, ek aankde ki tarah nahi."
    )
    print("=" * 66)


def _build_price_provider(args):
    """Real option-chain provider — registry khaali ho to saaf batao."""
    from data.option_chain import AngelOptionChain, load_registry, registry_path

    registry = load_registry(registry_path(cache_dir=args.cache_dir))
    if not registry:
        print(
            "⚠️ Option registry khaali hai — --real-option-prices ka koi asar "
            "nahi hoga. Pehle chalao: python3 -m data.option_chain --refresh"
        )
        return None

    return AngelOptionChain(
        interval=args.interval, cache_dir=args.cache_dir, registry=registry,
        offline=args.source == "csv",
    )


def _run_options_sim(df: pd.DataFrame, results: dict, vix, args) -> None:
    trade_log = _collect_trade_log(results, args.mode)
    if args.mode == "split":
        print("(Options P&L sirf OUT-OF-SAMPLE trades pe — in-sample pe nahi.)")

    provider = _build_price_provider(args) if args.real_option_prices else None
    sim_kwargs = {
        "vix_series": vix, "lot_size": args.lot_size,
        "strike_step": args.strike_step, "expiry_weekday": args.expiry_weekday,
    }
    sim = simulate_trade_log(
        df, trade_log, iv_crush_pct=args.iv_crush_pct,
        price_provider=provider, **sim_kwargs
    )
    print_options_report(sim, lot_size=args.lot_size)

    if provider is not None:
        from data.option_chain import print_coverage_report

        print_coverage_report(provider.coverage())

    # Crush ek MODEL assumption hai — jo trades market ke bhaav pe chale
    # unpe iska koi matlab nahi, isliye all-real run pe table nahi chhapti
    if sim["trades"] and sim.get("pricing", {}).get("model"):
        print_crush_sensitivity(
            crush_sensitivity(df, trade_log, price_provider=provider, **sim_kwargs)
        )


if __name__ == "__main__":
    sys.exit(main())
