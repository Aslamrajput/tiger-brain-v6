"""
Tiger Brain V6+V7 — Backtest Engine (Phase 2)
================================================
Ye pehla real backtest hai — poore Stage-1 pipeline (regime + 5
sub-brains + meta-brain) ko historical data ke har din pe chalata hai
aur check karta hai ki decision "sahi" nikla ya nahi (agle din ki price
move ke hisaab se).

⚠️ HONESTY GUARDRAILS (jaisa humne discuss kiya tha, ye sirf baat nahi,
code mein bhi hai):

1. NO LOOKAHEAD BIAS — har din ka decision sirf USI DIN TAK ke data se
   liya jaata hai. Kal ka data future se nahi aata. Ye check
   `df.iloc[:i+1]` (sirf abhi tak ka data) use karke enforce hota hai.

2. IN-SAMPLE vs OUT-OF-SAMPLE SPLIT — data ko do hisso mein baanta hai.
   Agar sirf IN-SAMPLE pe accha result dikhe aur OUT-OF-SAMPLE pe kharab,
   ye OVERFITTING ka clear sign hai — is report mein dono alag-alag
   dikhaye jaate hain, ek combined number nahi.

3. SMALL-SAMPLE WARNING — agar kul trades bahut kam hain (jaise <20),
   report khud warning deta hai ki result statistically meaningless ho
   sakta hai — chahe "100% win rate" bhi dikhe.

⚠️ IMPORTANT LIMITATION — Ye abhi sirf DIRECTIONAL accuracy check karta
hai (kya price sahi disha mein gaya), REAL OPTIONS P&L nahi (jisme
premium, theta decay, IV crush shaamil hote). Isliye ye ek "starting
signal-quality check" hai, asli profit-loss simulation nahi — wo agla
step hoga jab options-pricing model banega.
"""

from __future__ import annotations

import collections
import logging

import pandas as pd

try:
    from config.thresholds import DECISION_SCORE_THRESHOLD, PIPELINE
    from pipeline.stage1_scanner import run_scanner
except ImportError:
    raise ImportError("Repo ROOT se chalao, 'backtest/' ke andar se nahi.")

logger = logging.getLogger("tiger_brain.backtest.engine")
logging.basicConfig(level=logging.INFO)


MIN_WARMUP_DAYS = 30  # regime classifier ko ye kam se kam chahiye (bars mein)
MIN_MEANINGFUL_TRADES = 20  # isse kam trades pe result statistically weak hai


def new_gate_stats() -> dict:
    """Diagnostics ka khaali dhaancha — kaun sa gate kitne din blocked kar raha hai."""
    return {
        "days": 0,
        "regimes": collections.Counter(),
        "decisions": collections.Counter(),
        "blocked_by": collections.Counter(),
        "brain_votes": collections.Counter(),
        "brain_max_confidence": {},
        "scores": [],
    }


def merge_gate_stats(target: dict, extra: dict) -> dict:
    """Do windows ke diagnostics jodta hai (walk-forward folds ke liye)."""
    target["days"] += extra["days"]
    for key in ("regimes", "decisions", "blocked_by", "brain_votes"):
        target[key].update(extra[key])
    for brain, value in extra["brain_max_confidence"].items():
        target["brain_max_confidence"][brain] = max(
            target["brain_max_confidence"].get(brain, 0.0), value
        )
    target["scores"].extend(extra["scores"])
    return target


def _record_gate_stats(stats: dict, result: dict, score_threshold: float) -> None:
    meta = result["meta_brain_result"]
    regime = result.get("regime") or {}
    stats["days"] += 1
    stats["regimes"][regime.get("regime", "UNKNOWN")] += 1
    stats["decisions"][meta["final_decision"]] += 1
    stats["scores"].append(meta.get("final_score", 0.0))

    for brain, vote_data in result.get("sub_brain_votes", {}).items():
        stats["brain_votes"][f"{brain}:{vote_data['vote']}"] += 1
        stats["brain_max_confidence"][brain] = max(
            stats["brain_max_confidence"].get(brain, 0.0),
            float(vote_data["confidence"]),
        )

    if meta["final_decision"] != "NO_TRADE":
        if not result.get("passed_stage1", True):
            stats["blocked_by"]["stage1_min_confidence"] += 1
        return
    if meta.get("veto_triggered", False):
        stats["blocked_by"]["vol_arb_hard_veto"] += 1
    elif meta.get("final_score", 0.0) < score_threshold:
        stats["blocked_by"]["score_below_threshold"] += 1
    else:
        stats["blocked_by"]["no_clear_direction"] += 1


def run_single_period_backtest(
    df: pd.DataFrame,
    vix_series: pd.Series = None,
    score_threshold: float = DECISION_SCORE_THRESHOLD,
    stage1_min: float = PIPELINE["STAGE1_MIN_CONFIDENCE"],
    warmup_bars: int = MIN_WARMUP_DAYS,
    session_aware: bool = False,
    context=None,
) -> dict:
    """
    Ek data-period (chahe IN-SAMPLE ho ya OUT-OF-SAMPLE) pe backtest
    chalata hai — har din ka decision uसी din tak ke data se leta hai
    (no lookahead), phir agle din ki price move se check karta hai.

    Args:
        df: poora OHLCV data is period ka
        vix_series: optional, same index alignment

    Returns:
        dict:
            'total_days_tested': int
            'total_trades': int (jitne din NO_TRADE nahi tha)
            'correct_direction': int
            'accuracy_pct': float
            'trade_log': list of per-trade dicts (day, decision, correct?)
    """
    if len(df) < warmup_bars + 2:
        logger.warning(
            f"Sirf {len(df)} bars ka data hai, kam se kam "
            f"{warmup_bars + 2} chahiye backtest ke liye."
        )
        return {
            "total_days_tested": 0, "total_trades": 0,
            "correct_direction": 0, "accuracy_pct": 0.0,
            "trade_log": [], "gate_stats": new_gate_stats(),
        }

    # Warmup ke baad se, aur last din se pehle tak (kyunki humein "agle
    # din" ka actual outcome chahiye check karne ke liye)
    return backtest_range(
        df,
        warmup_bars,
        len(df) - 1,
        vix_series=vix_series,
        score_threshold=score_threshold,
        stage1_min=stage1_min,
        warmup_bars=warmup_bars,
        session_aware=session_aware,
        context=context,
    )


def _same_session(left, right) -> bool:
    """Do bars ek hi trading din ke hain ya nahi."""
    return pd.Timestamp(left).date() == pd.Timestamp(right).date()


def backtest_range(
    df: pd.DataFrame,
    eval_start: int,
    eval_end: int,
    vix_series: pd.Series = None,
    history_start: int = 0,
    score_threshold: float = DECISION_SCORE_THRESHOLD,
    stage1_min: float = PIPELINE["STAGE1_MIN_CONFIDENCE"],
    warmup_bars: int = MIN_WARMUP_DAYS,
    session_aware: bool = False,
    context=None,
) -> dict:
    """
    Core loop — `df` ke sirf [eval_start, eval_end) wale dino pe decision
    leta hai, PAR history ke liye poora `df` ka pehla hissa use karta hai.

    Ye walk-forward ke liye zaroori hai: test-window ke pehle din ko bhi
    warmup chahiye, aur wo warmup pichhle (train) window se aana chahiye
    — na ki test window ko hi kaat ke.

    NO LOOKAHEAD yahan bhi enforce hai: din `i` ka decision sirf
    `df.iloc[:i+1]` se banta hai, aur outcome `i+1` se check hota hai.

    Args:
        df: poora OHLCV data (history + evaluation window)
        eval_start: kis index se decision lena shuru karna hai
        eval_end: kis index se PEHLE tak (exclusive) — `len(df)-1` se
                  zyada nahi, kyunki agle din ka outcome chahiye
        vix_series: optional, same index alignment
        history_start: is index se pehle ka data scanner ko dikhega hi
                       nahi — rolling window ke liye (0 = poori history)
        warmup_bars: decision lene se pehle kitne bars ki history chahiye
        session_aware: intraday bars ke liye True — tab session ka aakhri
                       bar skip hota hai, kyunki uska "agla bar" agle din
                       ka open hoga aur overnight gap ko intraday move
                       maan lena galat result deta hai
        context: `data.derivatives.DerivativeContext` — us bar tak ka OI
                 aur IV context (futures OI buildup, ATM IV series). None
                 = wo feeds available hi nahi (sub-brains khud us factor
                 ka weight redistribute karte hain). Lookahead yahan bhi
                 nahi tootta: context sirf `df.index[i]` maangta hai aur
                 usi tak ka data lautata hai.

    Returns:
        run_single_period_backtest() jaisa hi dict
    """
    trade_log = []
    gate_stats = new_gate_stats()

    eval_start = max(eval_start, history_start + warmup_bars)
    eval_end = min(eval_end, len(df) - 1)

    for i in range(eval_start, eval_end):
        if session_aware and not _same_session(df.index[i], df.index[i + 1]):
            # Session ka aakhri bar — intraday position overnight nahi rakhte
            continue

        # ⚠️ NO LOOKAHEAD: sirf index 0 se i tak ka data (aaj tak),
        # kal/future ka data bilkul nahi diya ja raha
        df_till_today = df.iloc[history_start : i + 1]
        vix_till_today = (
            vix_series.iloc[history_start : i + 1] if vix_series is not None else None
        )

        derivative_kwargs = (
            context.scanner_kwargs(df.index[i]) if context is not None else {}
        )

        try:
            result = run_scanner(
                df_till_today,
                vix_series=vix_till_today,
                score_threshold=score_threshold,
                min_stage1_confidence=stage1_min,
                **derivative_kwargs,
            )
        except Exception as exc:
            logger.warning(f"Day index {i} pe scanner error: {exc}")
            continue

        if result.get("meta_brain_result") is None:
            logger.warning(
                f"Day index {i} pe koi decision nahi bana: {result['stage1_notes']}"
            )
            continue

        _record_gate_stats(gate_stats, result, score_threshold)
        decision = result["meta_brain_result"]["final_decision"]

        if decision == "NO_TRADE":
            continue  # NO_TRADE ko "trade" nahi ginte, accuracy mein shamil nahi

        # Stage 1 apna alag cutoff rakhta hai (stage1_min > score_threshold ho
        # sakta hai) — wahan fail hua candidate trade log mein nahi jaana chahiye
        if not result.get("passed_stage1", True):
            continue

        # Agle din ka actual price move dekho (ye sirf ab, checking ke
        # liye use ho raha hai — decision lete waqt nahi dekha gaya tha)
        today_close = df["close"].iloc[i]
        next_close = df["close"].iloc[i + 1]
        actual_direction = "BUY" if next_close > today_close else "SELL"

        was_correct = decision == actual_direction

        trade_log.append({
            "date_index": i,
            "date": df.index[i],
            "decision": decision,
            "score": result["meta_brain_result"]["final_score"],
            "actual_direction": actual_direction,
            "correct": was_correct,
        })

    total_trades = len(trade_log)
    correct = sum(1 for t in trade_log if t["correct"])
    accuracy = round(correct / total_trades * 100, 1) if total_trades > 0 else 0.0

    return {
        "total_days_tested": max(eval_end - eval_start, 0),
        "total_trades": total_trades,
        "correct_direction": correct,
        "accuracy_pct": accuracy,
        "trade_log": trade_log,
        "gate_stats": gate_stats,
    }


def run_backtest_with_split(
    df: pd.DataFrame,
    vix_series: pd.Series = None,
    in_sample_pct: float = 60,
    score_threshold: float = DECISION_SCORE_THRESHOLD,
    stage1_min: float = PIPELINE["STAGE1_MIN_CONFIDENCE"],
    warmup_bars: int = MIN_WARMUP_DAYS,
    session_aware: bool = False,
    context=None,
) -> dict:
    """
    MAIN ENTRY POINT — poora data ko IN-SAMPLE aur OUT-OF-SAMPLE mein
    baantke dono pe alag-alag backtest chalata hai, taaki overfitting
    check ho sake (Section 29 ka core requirement).

    Args:
        df: poora historical OHLCV data
        vix_series: optional
        in_sample_pct: kitna % data IN-SAMPLE (training/tuning) ke liye,
                       baaki OUT-OF-SAMPLE (unseen validation) ke liye

    Returns:
        dict:
            'in_sample': run_single_period_backtest() output
            'out_of_sample': run_single_period_backtest() output
            'overfitting_warning': str | None — agar dono mein bahut
                farak hai (possible overfitting sign)
            'sample_size_warning': str | None
    """
    split_idx = int(len(df) * (in_sample_pct / 100))

    df_in_sample = df.iloc[:split_idx]
    df_out_sample = df.iloc[split_idx:]

    vix_in = vix_series.iloc[:split_idx] if vix_series is not None else None
    vix_out = vix_series.iloc[split_idx:] if vix_series is not None else None

    logger.info(
        f"Data split: {len(df_in_sample)} din IN-SAMPLE, "
        f"{len(df_out_sample)} din OUT-OF-SAMPLE"
    )

    in_sample_result = run_single_period_backtest(
        df_in_sample, vix_in, score_threshold=score_threshold, stage1_min=stage1_min,
        warmup_bars=warmup_bars, session_aware=session_aware, context=context,
    )
    out_sample_result = run_single_period_backtest(
        df_out_sample, vix_out, score_threshold=score_threshold, stage1_min=stage1_min,
        warmup_bars=warmup_bars, session_aware=session_aware, context=context,
    )

    # --- Overfitting Check ---
    overfitting_warning = None
    if in_sample_result["total_trades"] > 0 and out_sample_result["total_trades"] > 0:
        acc_diff = in_sample_result["accuracy_pct"] - out_sample_result["accuracy_pct"]
        if acc_diff > 20:  # 20+ percentage point gap = red flag
            overfitting_warning = (
                f"⚠️ OVERFITTING KA KHATRA: IN-SAMPLE accuracy "
                f"({in_sample_result['accuracy_pct']}%) OUT-OF-SAMPLE "
                f"({out_sample_result['accuracy_pct']}%) se {acc_diff:.1f} "
                f"points zyada hai. Ye sign hai ki system 'purane data ko "
                f"yaad kar raha hai', naye/unseen data pe utna accha nahi "
                f"kaam kar raha. In-sample number pe bharosa mat karo."
            )

    # --- Small Sample Warning ---
    sample_size_warning = None
    total_trades_combined = in_sample_result["total_trades"] + out_sample_result["total_trades"]
    if total_trades_combined < MIN_MEANINGFUL_TRADES:
        sample_size_warning = (
            f"⚠️ SIRF {total_trades_combined} TOTAL TRADES mile poore "
            f"backtest mein — {MIN_MEANINGFUL_TRADES} se kam. Chahe "
            f"accuracy % kitna bhi accha/bura dikhe, itne kam samples pe "
            f"koi bhi statistical conclusion nikalna sahi nahi hai. Zyada "
            f"lamba time-period ka data chahiye reliable result ke liye."
        )

    return {
        "in_sample": in_sample_result,
        "out_of_sample": out_sample_result,
        "overfitting_warning": overfitting_warning,
        "sample_size_warning": sample_size_warning,
    }


def print_backtest_report(results: dict):
    """Human-readable report print karta hai, saari honesty warnings ke saath."""
    print("\n" + "=" * 60)
    print("TIGER BRAIN BACKTEST REPORT")
    print("=" * 60)

    for period_name, key in [("IN-SAMPLE (training period)", "in_sample"),
                              ("OUT-OF-SAMPLE (unseen validation)", "out_of_sample")]:
        r = results[key]
        print(f"\n--- {period_name} ---")
        print(f"Days tested: {r['total_days_tested']}")
        print(f"Total trades (non-NO_TRADE days): {r['total_trades']}")
        print(f"Correct direction: {r['correct_direction']}")
        print(f"Accuracy: {r['accuracy_pct']}%")

    print("\n" + "-" * 60)
    if results["overfitting_warning"]:
        print(results["overfitting_warning"])
    else:
        print("✅ Koi bada overfitting-gap nahi dikha in-sample vs out-of-sample mein.")

    if results["sample_size_warning"]:
        print(results["sample_size_warning"])

    print("\n⚠️ REMINDER: Ye sirf DIRECTIONAL accuracy hai (price sahi disha")
    print("mein gaya ya nahi) — REAL OPTIONS P&L (premium, theta, IV crush)")
    print("abhi shaamil nahi hai. Ye ek starting signal-quality check hai.")
    print("=" * 60 + "\n")


# ============================================================
# QUICK MANUAL TEST — Real Angel One data ke saath
# Chalane ka tarika: repo ROOT se → python3 -m backtest.engine
# ============================================================
if __name__ == "__main__":
    from datetime import datetime, timedelta
    from broker.angel_connect import AngelBroker
    from data.loader import fetch_angel_historical_candles

    print("=== Tiger Brain Backtest — Real NIFTY Data ===\n")
    print("⚠️ Ye REAL Angel One login + real historical data use karega.\n")

    broker = AngelBroker()
    broker.login()

    end = datetime.now()
    start = end - timedelta(days=180)  # jitna Angel One de sake (ONE_DAY: max 2000 din)

    df = fetch_angel_historical_candles(broker, "NSE", "99926000", "ONE_DAY", start, end)
    print(f"Total {len(df)} din ka real NIFTY data mila.\n")

    if len(df) < MIN_WARMUP_DAYS + MIN_MEANINGFUL_TRADES:
        print(
            f"⚠️ Sirf {len(df)} din ka data hai — meaningful backtest ke "
            f"liye kam se kam {MIN_WARMUP_DAYS + MIN_MEANINGFUL_TRADES} "
            f"din chahiye. Result abhi bhi print honge, par unpe zyada "
            f"bharosa mat karna."
        )

    results = run_backtest_with_split(df)
    print_backtest_report(results)
  
