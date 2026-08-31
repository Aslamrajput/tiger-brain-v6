"""
Tiger Brain V6+V7 — Walk-Forward Validation (Phase 2, Section 29 point 4)
==========================================================================
Ek hi in-sample/out-of-sample split (engine.run_backtest_with_split) se
sirf EK baar ka answer milta hai — aur wo answer us ek split-date pe
depend karta hai. Walk-forward isse aage jaata hai: data ko kai
sequential folds mein todta hai aur har fold ka test-window ALAG,
UNSEEN period hota hai.

    fold 1:  [--- train ---][test]
    fold 2:         [--- train ---][test]
    fold 3:                [--- train ---][test]
                (rolling window — ya anchored, neeche dekho)

Isse do cheezein pata chalti hain jo single split nahi bata sakta:

1. CONSISTENCY — kya system har period mein theek chal raha hai, ya
   sirf ek lucky patch (jaise ek strong trending quarter) ki wajah se
   overall number accha dikh raha tha.

2. TIME-VARYING BEHAVIOUR — 2023 ke market mein jo chala, 2024 mein
   chala ya nahi. Regime badalne pe system ka behaviour badalta hai.

⚠️ HONEST LIMITATION #1 — ABHI "TRAIN" WINDOW PE KOI PARAMETER FITTING
NAHI HOTI. Thresholds `config/thresholds.py` mein fixed hain (blueprint
Part B ke starting numbers). Iska matlab:
  - Classic walk-forward optimization (train pe tune → test pe verify)
    ka "tune" wala hissa abhi missing hai.
  - Isliye yahan train-window ka result sirf REFERENCE ke liye report
    hota hai, aur asli conclusion sirf test-windows se nikalta hai.
  - Positive side: kyunki kuch fit nahi ho raha, in-sample overfitting
    ka khatra bhi utna nahi — par "curve fitting by hand" (humne khud
    numbers चुने) ka khatra phir bhi hai, isliye out-of-sample folds
    hi sach bolenge.
  Jab Phase 2 mein threshold-tuning add hoga, wo tuning sirf train
  window ke andar honi chahiye aur `run_walk_forward()` ko wahi tuned
  config test window pe pass karni chahiye.

⚠️ HONEST LIMITATION #2 — engine.py jaisi hi: ye DIRECTIONAL accuracy
hai (agle din price sahi disha mein gaya ya nahi), REAL OPTIONS P&L
nahi (premium, theta decay, IV crush shaamil nahi). Accuracy 55% hone
ka matlab profit NAHI hai — options buying mein theta har din paisa
khata hai chahe direction sahi ho.
"""

import logging

import pandas as pd

try:
    from backtest.engine import (
        MIN_MEANINGFUL_TRADES,
        MIN_WARMUP_DAYS,
        backtest_range,
        merge_gate_stats,
        new_gate_stats,
    )
    from config.thresholds import DECISION_SCORE_THRESHOLD, PIPELINE
except ImportError:
    raise ImportError("Repo ROOT se chalao, 'backtest/' ke andar se nahi.")

logger = logging.getLogger("tiger_brain.backtest.walk_forward")
logging.basicConfig(level=logging.INFO)


# Default window sizes — trading days mein (1 saal ≈ 250 trading din)
DEFAULT_TRAIN_DAYS = 250   # ~1 saal history/reference
DEFAULT_TEST_DAYS = 60     # ~3 mahine ka unseen test window

# Itne folds se kam pe walk-forward ka pura point hi khatam ho jaata hai
MIN_MEANINGFUL_FOLDS = 3


def generate_folds(
    total_len: int,
    train_days: int = DEFAULT_TRAIN_DAYS,
    test_days: int = DEFAULT_TEST_DAYS,
    anchored: bool = False,
) -> list[dict]:
    """
    Sequential (non-overlapping test windows wale) folds banata hai.

    Args:
        total_len: kul kitne din ka data hai
        train_days: har fold ka train/history window
        test_days: har fold ka unseen test window
        anchored: False = rolling window (train hamesha `train_days` ka,
                  purana data drop hota jaata hai — recent regime pe
                  zyada focus). True = anchored/expanding (train hamesha
                  din 0 se shuru, lamba hota jaata hai).

    Returns:
        list of dicts: {'fold', 'train_start', 'train_end', 'test_start',
        'test_end'} — sab integer indexes, test window [test_start,
        test_end) exclusive-end hai.
    """
    if train_days < MIN_WARMUP_DAYS:
        raise ValueError(
            f"train_days ({train_days}) kam se kam MIN_WARMUP_DAYS "
            f"({MIN_WARMUP_DAYS}) hona chahiye — regime classifier ko "
            f"itni history chahiye hi chahiye."
        )
    if test_days < 1:
        raise ValueError("test_days kam se kam 1 hona chahiye.")

    folds = []
    test_start = train_days

    # Last din ka outcome check nahi ho sakta (agle din ka close chahiye),
    # isliye evaluation `total_len - 1` pe rukta hai
    last_evaluable = total_len - 1

    while test_start < last_evaluable:
        test_end = min(test_start + test_days, last_evaluable)
        folds.append({
            "fold": len(folds) + 1,
            "train_start": 0 if anchored else test_start - train_days,
            "train_end": test_start,
            "test_start": test_start,
            "test_end": test_end,
        })
        test_start = test_end

    return folds


def run_walk_forward(
    df: pd.DataFrame,
    vix_series: pd.Series = None,
    train_days: int = DEFAULT_TRAIN_DAYS,
    test_days: int = DEFAULT_TEST_DAYS,
    anchored: bool = False,
    score_threshold: float = DECISION_SCORE_THRESHOLD,
    stage1_min: float = PIPELINE["STAGE1_MIN_CONFIDENCE"],
) -> dict:
    """
    MAIN ENTRY POINT — poore data pe walk-forward validation chalata hai
    aur sirf out-of-sample (test) windows ko jodkar final verdict deta
    hai.

    Har fold mein decision lete waqt sirf us din tak ka data milta hai
    (engine.backtest_range ka no-lookahead loop), aur warmup pichhle
    train window se aata hai — test window ko kaata nahi jaata.

    Args:
        df: poora historical OHLCV data (index = dates)
        vix_series: optional India VIX, same index
        train_days / test_days: window size trading-days mein
        anchored: expanding train window (True) ya rolling (False)

    Returns:
        dict:
            'folds': per-fold result list
            'aggregate': saare test windows ka combined result
            'consistency': kitne folds 50%+ accuracy pe rahe, spread etc.
            'warnings': list[str] — honest warnings (kam folds, kam
                        trades, unstable results)
            'config': jo settings use hui
    """
    warnings = []

    folds_spec = generate_folds(len(df), train_days, test_days, anchored)

    if not folds_spec:
        return {
            "folds": [],
            "aggregate": _empty_aggregate(),
            "consistency": None,
            "gate_stats": new_gate_stats(),
            "warnings": [
                f"Data sirf {len(df)} din ka hai — train_days={train_days} "
                f"+ test_days={test_days} ke liye kam se kam "
                f"{train_days + test_days + 1} din chahiye. Ek bhi fold "
                f"nahi ban paya, koi conclusion mat nikalna."
            ],
            "config": _config_dict(train_days, test_days, anchored, len(df)),
        }

    fold_results = []
    gate_stats = new_gate_stats()
    for spec in folds_spec:
        # NOTE: `df` poora pass ho raha hai par scanner ko sirf
        # [train_start, i] dikhta hai — rolling mein purana data cut,
        # anchored mein train_start=0. Decisions sirf test window pe.
        result = backtest_range(
            df,
            spec["test_start"],
            spec["test_end"],
            vix_series=vix_series,
            history_start=spec["train_start"],
            score_threshold=score_threshold,
            stage1_min=stage1_min,
        )
        merge_gate_stats(gate_stats, result["gate_stats"])
        last_test_index = min(spec["test_end"], len(df) - 1) - 1
        fold_results.append({
            **spec,
            "test_start_date": df.index[spec["test_start"]],
            "test_end_date": df.index[last_test_index],
            "total_days_tested": result["total_days_tested"],
            "total_trades": result["total_trades"],
            "correct_direction": result["correct_direction"],
            "accuracy_pct": result["accuracy_pct"],
            "trade_log": result["trade_log"],
        })

    aggregate = _aggregate_folds(fold_results)
    consistency = _consistency_stats(fold_results)

    if len(fold_results) < MIN_MEANINGFUL_FOLDS:
        warnings.append(
            f"Sirf {len(fold_results)} fold bana — walk-forward ka matlab "
            f"hi tabhi hai jab kam se kam {MIN_MEANINGFUL_FOLDS} alag-alag "
            f"periods pe test ho. Lamba data (1-2 saal) do, ya test_days "
            f"chhota karo."
        )

    if aggregate["total_trades"] < MIN_MEANINGFUL_TRADES:
        warnings.append(
            f"Poore walk-forward mein sirf {aggregate['total_trades']} "
            f"trades mile ({MIN_MEANINGFUL_TRADES} se kam). Accuracy % "
            f"chahe kuch bhi dikhe, itne kam samples pe statistical "
            f"conclusion nikalna galat hai."
        )

    if consistency and consistency["accuracy_spread_pct"] > 40:
        warnings.append(
            f"Folds ke beech accuracy ka farak {consistency['accuracy_spread_pct']} "
            f"points hai (best {consistency['best_fold_accuracy_pct']}%, "
            f"worst {consistency['worst_fold_accuracy_pct']}%). Matlab "
            f"result period pe bahut depend kar raha hai — ye stability "
            f"ki kami hai, average number pe bharosa mat karo."
        )

    if consistency and consistency["folds_with_trades"] > 0:
        below_half = consistency["folds_with_trades"] - consistency["folds_above_50pct"]
        if below_half >= consistency["folds_with_trades"] / 2:
            warnings.append(
                f"{below_half}/{consistency['folds_with_trades']} folds mein "
                f"accuracy 50% se neeche rahi — yani coin-toss se behtar "
                f"nahi. Ye system ko live/paper trading pe le jaane ka "
                f"signal NAHI hai."
            )

    return {
        "folds": fold_results,
        "gate_stats": gate_stats,
        "aggregate": aggregate,
        "consistency": consistency,
        "warnings": warnings,
        "config": _config_dict(train_days, test_days, anchored, len(df)),
    }


def _config_dict(train_days, test_days, anchored, data_len) -> dict:
    return {
        "train_days": train_days,
        "test_days": test_days,
        "mode": "anchored/expanding" if anchored else "rolling",
        "total_data_days": data_len,
    }


def _empty_aggregate() -> dict:
    return {
        "total_days_tested": 0, "total_trades": 0,
        "correct_direction": 0, "accuracy_pct": 0.0,
    }


def _aggregate_folds(fold_results: list[dict]) -> dict:
    """Saare test windows ko jodkar ek combined out-of-sample number."""
    total_trades = sum(f["total_trades"] for f in fold_results)
    correct = sum(f["correct_direction"] for f in fold_results)
    return {
        "total_days_tested": sum(f["total_days_tested"] for f in fold_results),
        "total_trades": total_trades,
        "correct_direction": correct,
        "accuracy_pct": round(correct / total_trades * 100, 1) if total_trades else 0.0,
    }


def _consistency_stats(fold_results: list[dict]) -> dict | None:
    """
    Stability check — average accuracy jhooth bol sakti hai agar ek fold
    bahut accha aur baaki bekaar ho. Isliye spread aur per-fold count.
    """
    scored = [f for f in fold_results if f["total_trades"] > 0]
    if not scored:
        return None

    accuracies = [f["accuracy_pct"] for f in scored]
    return {
        "total_folds": len(fold_results),
        "folds_with_trades": len(scored),
        "folds_above_50pct": sum(1 for a in accuracies if a > 50),
        "best_fold_accuracy_pct": max(accuracies),
        "worst_fold_accuracy_pct": min(accuracies),
        "accuracy_spread_pct": round(max(accuracies) - min(accuracies), 1),
        "mean_fold_accuracy_pct": round(sum(accuracies) / len(accuracies), 1),
    }


def print_walk_forward_report(results: dict):
    """Human-readable report — saari honesty warnings ke saath."""
    cfg = results["config"]

    print("\n" + "=" * 66)
    print("TIGER BRAIN — WALK-FORWARD VALIDATION REPORT")
    print("=" * 66)
    print(
        f"Data: {cfg['total_data_days']} din | train window: "
        f"{cfg['train_days']} din ({cfg['mode']}) | test window: "
        f"{cfg['test_days']} din"
    )

    if not results["folds"]:
        for w in results["warnings"]:
            print(f"\n{w}")
        print("=" * 66 + "\n")
        return

    print("\n--- PER-FOLD (har test window ALAG, unseen period hai) ---")
    header = f"{'Fold':<6}{'Test period':<26}{'Trades':<9}{'Correct':<9}{'Accuracy'}"
    print(header)
    print("-" * len(header))
    for f in results["folds"]:
        period = f"{_d(f['test_start_date'])} → {_d(f['test_end_date'])}"
        print(
            f"{f['fold']:<6}{period:<26}{f['total_trades']:<9}"
            f"{f['correct_direction']:<9}{f['accuracy_pct']}%"
        )

    agg = results["aggregate"]
    print("\n--- AGGREGATE (sirf out-of-sample windows) ---")
    print(f"Days tested: {agg['total_days_tested']}")
    print(f"Total trades (non-NO_TRADE days): {agg['total_trades']}")
    print(f"Correct direction: {agg['correct_direction']}")
    print(f"Accuracy: {agg['accuracy_pct']}%")

    c = results["consistency"]
    if c:
        print("\n--- CONSISTENCY (average se zyada ye maayne rakhta hai) ---")
        print(
            f"Folds with at least 1 trade: {c['folds_with_trades']}"
            f"/{c['total_folds']}"
        )
        print(
            f"Folds above 50% accuracy: {c['folds_above_50pct']}"
            f"/{c['folds_with_trades']}"
        )
        print(
            f"Best fold: {c['best_fold_accuracy_pct']}% | worst fold: "
            f"{c['worst_fold_accuracy_pct']}% | spread: "
            f"{c['accuracy_spread_pct']} points"
        )
        print(f"Mean of fold accuracies: {c['mean_fold_accuracy_pct']}%")

    print("\n" + "-" * 66)
    if results["warnings"]:
        for w in results["warnings"]:
            print(f"⚠️ {w}\n")
    else:
        print("✅ Koi bada red flag nahi mila is walk-forward run mein.")

    print("⚠️ REMINDER: Ye DIRECTIONAL accuracy hai, REAL OPTIONS P&L nahi")
    print("(premium/theta/IV crush shaamil nahi). 55% accuracy ka matlab")
    print("profit nahi hota — theta har din premium khata hai. Aur train")
    print("window pe abhi koi parameter tuning nahi hoti (module docstring")
    print("mein detail hai), isliye ye 'classic' walk-forward optimization")
    print("nahi, uska validation-only version hai.")
    print("=" * 66 + "\n")


def _d(value) -> str:
    """Date ko chhote format mein — index datetime na ho to as-is."""
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return str(value)
