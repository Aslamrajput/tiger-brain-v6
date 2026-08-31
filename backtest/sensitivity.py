"""
Tiger Brain V6+V7 — Threshold Sensitivity Analysis
==================================================

Kyun: pipeline real 2-saal NIFTY data pe 0 trades de raha tha. Bina ye
jaane ki kaun sa gate rok raha hai, threshold girana andhera mein teer
chalana hai. Ye module ek hi dataset pe kai score-thresholds chalata hai
aur har threshold pe trade-count + directional accuracy dikhata hai.

⚠️ IMPORTANT — ye TUNING TOOL NAHI hai:
Yahan se "best accuracy wala threshold" utha kar production mein daalna
seedha overfitting hai (same out-of-sample data pe select kar rahe ho).
Ye sirf ye batata hai ki gate kis level pe khulta hai aur kitne signals
milte hain — production value alag, forward/paper data se validate honi
chahiye.
"""

import pandas as pd

try:
    from backtest.walk_forward import run_walk_forward
except ImportError:
    raise ImportError("Repo ROOT se chalao, 'backtest/' ke andar se nahi.")


DEFAULT_THRESHOLD_GRID = [20, 25, 30, 35, 40, 45, 50, 55, 60, 65]


def run_threshold_sweep(
    df: pd.DataFrame,
    vix_series: pd.Series = None,
    thresholds: list = None,
    train_days: int = 250,
    test_days: int = 60,
    anchored: bool = False,
) -> pd.DataFrame:
    """
    Har threshold pe poora walk-forward chalata hai (out-of-sample only)
    aur ek summary table lautata hai.
    """
    grid = DEFAULT_THRESHOLD_GRID if thresholds is None else thresholds
    rows = []

    for threshold in grid:
        results = run_walk_forward(
            df,
            vix_series=vix_series,
            train_days=train_days,
            test_days=test_days,
            anchored=anchored,
            score_threshold=threshold,
            stage1_min=threshold,
        )
        agg = results["aggregate"]
        consistency = results["consistency"]
        rows.append({
            "score_threshold": threshold,
            "days_tested": agg["total_days_tested"],
            "trades": agg["total_trades"],
            "trade_rate_pct": round(
                agg["total_trades"] / agg["total_days_tested"] * 100, 1
            ) if agg["total_days_tested"] else 0.0,
            "accuracy_pct": agg["accuracy_pct"],
            "folds_with_trades": consistency["folds_with_trades"] if consistency else 0,
            "accuracy_spread_pct": (
                consistency["accuracy_spread_pct"] if consistency else None
            ),
        })

    return pd.DataFrame(rows)


def print_sweep_report(table: pd.DataFrame) -> None:
    print("\n" + "=" * 66)
    print("THRESHOLD SENSITIVITY SWEEP (out-of-sample walk-forward)")
    print("=" * 66)
    print(table.to_string(index=False))
    print(
        "\n⚠️ Ye tuning result NAHI hai — isme se sabse acchi accuracy wala "
        "threshold chunna overfitting hoga. Ye sirf dikhata hai ki gate kis "
        "level pe khulta hai aur signal-count kaisa badalta hai."
    )
