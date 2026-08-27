"""
Tiger Brain V6+V7 — STAGE 4: Decision-Lock Brain (Section 9, 13, 26)
=======================================================================
Agar 2 candidates bache hain to sabse mazboot chunna, do mein uljhna
nahi (No-Confusion Rule). Position size bhi yahin tay hoti hai. Output:
ek final "Trade Instruction Packet" — abhi tak koi order nahi bheja gaya.
"""

try:
    from config.thresholds import PIPELINE, RISK
except ImportError:
    raise ImportError("Repo ROOT se chalao, 'pipeline/' ke andar se nahi.")


def lock_decision(exam_results: dict, account_capital: float) -> dict:
    """
    Args:
        exam_results: dict {symbol_name: pipeline.stage3_exam.run_exam() output}
                       — sirf wahi candidates jo Stage 3 tak pahunche
        account_capital: total account capital (Section 13.6 ka 80/20 rule
                          isi pe calculate hoga)

    Returns:
        dict:
            'locked': bool — kya koi trade instruction bana ya nahi
            'symbol': str | None — kaunsa symbol chuna gaya
            'direction': 'BUY' | 'SELL' | None
            'position_size_pct': float — capital ka kitna % (deployable
                pool ke andar se, Section 13.6)
            'position_size_multiplier': float — high/medium confidence
                multiplier (Section 26)
            'deployable_capital_used': float — actual rupee amount
            'decision_notes': list
    """
    decision_notes = []

    # --- STEP 1: Sirf passed candidates lena ---
    passed_candidates = {
        symbol: result
        for symbol, result in exam_results.items()
        if result.get("passed_stage3", False)
    }

    if not passed_candidates:
        decision_notes.append("Koi bhi candidate Stage 3 pass nahi kar paaya — NO_TRADE")
        return {
            "locked": False, "symbol": None, "direction": None,
            "position_size_pct": 0, "position_size_multiplier": 0,
            "deployable_capital_used": 0, "decision_notes": decision_notes,
        }

    # --- STEP 2: No-Confusion Rule — sabse mazboot chunna ---
    # Sort by adjusted_score, highest pehle
    sorted_candidates = sorted(
        passed_candidates.items(),
        key=lambda item: item[1]["adjusted_score"],
        reverse=True,
    )

    best_symbol, best_result = sorted_candidates[0]

    if len(sorted_candidates) > 1:
        second_symbol, second_result = sorted_candidates[1]
        score_diff = best_result["adjusted_score"] - second_result["adjusted_score"]

        if score_diff <= PIPELINE["STAGE4_TIE_BREAK_SCORE_DIFF"]:
            decision_notes.append(
                f"'{best_symbol}' aur '{second_symbol}' ka score bahut close hai "
                f"(diff {score_diff:.1f}) — No-Confusion Rule: pehle wala hi "
                f"(higher score) chunte hain, do mein nahi uljhte"
            )
        else:
            decision_notes.append(
                f"'{best_symbol}' clear winner hai (score {best_result['adjusted_score']} "
                f"vs '{second_symbol}' ka {second_result['adjusted_score']})"
            )

    # --- STEP 3: Direction ka note ---
    # exam_result mein direction seedha nahi hota — verify_single() se aata
    # hai. Isliye caller ko lock_decision_from_chain() use karna chahiye
    # (neeche di gayi hai), raw exam_results dict ke bajaye.
    decision_notes.append(
        "⚠️ NOTE: direction 'exam_results' se seedha nahi milta, verified_result "
        "chain se aana chahiye — caller ko lock_decision_from_chain() use karna "
        "chahiye (neeche di gayi hai) instead of raw exam_results dict."
    )

    # --- STEP 4: Position Sizing (Section 13, 26) ---
    adjusted_score = best_result["adjusted_score"]

    if adjusted_score >= RISK["HIGH_CONFIDENCE_STAGE3_SCORE_MIN"]:
        size_multiplier = RISK["HIGH_CONFIDENCE_SIZE_MULTIPLIER"]
        confidence_tier = "HIGH"
    elif adjusted_score >= RISK["MEDIUM_CONFIDENCE_STAGE3_SCORE_MIN"]:
        size_multiplier = 1.0
        confidence_tier = "MEDIUM"
    else:
        # Ye case theoretically nahi aana chahiye kyunki Stage 3 threshold
        # hi 65 hai (RISK['MEDIUM_CONFIDENCE_STAGE3_SCORE_MIN'] ke barabar)
        size_multiplier = 0
        confidence_tier = "BELOW_THRESHOLD"

    decision_notes.append(f"Confidence tier: {confidence_tier} (score {adjusted_score})")

    # Deployable pool (80% of total capital, Section 13.6)
    deployable_pool = account_capital * (RISK["DEPLOYABLE_POOL_PCT"] / 100)

    # Base risk per trade (2-3% of capital, applied within deployable pool)
    base_risk_pct = RISK["MAX_RISK_PER_TRADE_PCT"]
    final_risk_pct = min(base_risk_pct * size_multiplier, base_risk_pct * RISK["HIGH_CONFIDENCE_SIZE_MULTIPLIER"])
    # Hard cap: chahe multiplier kuch bhi ho, kabhi bhi MAX_RISK_PER_TRADE_PCT
    # * HIGH_CONFIDENCE_SIZE_MULTIPLIER se zyada risk nahi (Section 13.1 ka
    # "hard cap 3% risk abhi bhi lagu" wala rule)

    deployable_capital_used = deployable_pool * (final_risk_pct / 100)

    # Max position per symbol check (Section 26 — 25% se zyada nahi)
    max_allowed = account_capital * (RISK["MAX_POSITION_PER_SYMBOL_PCT"] / 100)
    if deployable_capital_used > max_allowed:
        deployable_capital_used = max_allowed
        decision_notes.append(
            f"Position size capped at Max-Position-Per-Symbol limit "
            f"({RISK['MAX_POSITION_PER_SYMBOL_PCT']}% of capital)"
        )

    return {
        "locked": True,
        "symbol": best_symbol,
        "direction": None,  # dekho note upar — direction chain se aana chahiye
        "position_size_pct": round(final_risk_pct, 2),
        "position_size_multiplier": size_multiplier,
        "deployable_capital_used": round(deployable_capital_used, 2),
        "decision_notes": decision_notes,
    }


def lock_decision_from_chain(verified_exam_pairs: dict, account_capital: float) -> dict:
    """
    Proper entry point — jab caller ke paas har symbol ke liye
    (verify_single output, run_exam output) dono hon.

    Args:
        verified_exam_pairs: dict {symbol: (verified_result, exam_result)}
        account_capital: total capital

    Returns:
        Same as lock_decision() but with 'direction' properly filled in.
    """
    exam_results_only = {sym: exam for sym, (verified, exam) in verified_exam_pairs.items()}
    result = lock_decision(exam_results_only, account_capital)

    if result["locked"]:
        chosen_symbol = result["symbol"]
        verified, _ = verified_exam_pairs[chosen_symbol]
        result["direction"] = verified["stage2_result"]["meta_brain_result"]["final_decision"]
        # Woh confusing placeholder note hata dena
        result["decision_notes"] = [
            n for n in result["decision_notes"] if "lock_decision_from_chain()" not in n
        ]

    return result


# ============================================================
# QUICK MANUAL TEST
# Chalane ka tarika: repo ROOT se → python3 -m pipeline.stage4_decision_lock
# ============================================================
if __name__ == "__main__":
    import numpy as np
    import pandas as pd
    from pipeline.stage1_scanner import run_scanner
    from pipeline.stage2_verifier import verify_single
    from pipeline.stage3_exam import run_exam

    def make_df(seed, trend_strength):
        np.random.seed(seed)
        n = 60
        dates = pd.date_range("2025-01-01", periods=n, freq="D")
        base_price = 100
        trend = np.linspace(0, trend_strength, n)
        noise = np.random.normal(0, 0.5, n)
        close_prices = base_price + trend + noise.cumsum() * 0.2
        df = pd.DataFrame(index=dates)
        df["close"] = close_prices
        df["open"] = df["close"].shift(1).fillna(base_price)
        df["high"] = df[["open", "close"]].max(axis=1) + np.random.uniform(0.2, 0.8, n)
        df["low"] = df[["open", "close"]].min(axis=1) - np.random.uniform(0.2, 0.8, n)
        df["volume"] = np.random.randint(200000, 400000, n)
        df.loc[df.index[-1], "volume"] = 700000
        return df, pd.Series(np.random.uniform(13, 16, n), index=dates)

    df1, vix1 = make_df(42, 20)
    stage1_1 = run_scanner(df1, vix_series=vix1)
    verified_1 = verify_single(stage1_1, df1, vix_series=vix1)
    exam_1 = run_exam(verified_1)

    print("=== Stage 4 Decision-Lock Test ===\n")
    print(f"(Note: is test data se Stage 3 khud fail ho sakta hai — dekho pichla test)")
    print(f"Stage 3 passed: {exam_1['passed_stage3']}, adjusted_score: {exam_1['adjusted_score']}\n")

    result = lock_decision_from_chain(
        {"NIFTY_TEST": (verified_1, exam_1)},
        account_capital=50000,
    )
    for k, v in result.items():
        print(f"{k}: {v}")

    print("\n✅ Test complete — koi crash nahi hua.")
    print("⚠️ Ye SYNTHETIC data hai, real market data se numbers alag honge.")
      
