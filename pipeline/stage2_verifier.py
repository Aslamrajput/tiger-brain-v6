"""
Tiger Brain V6+V7 — STAGE 2: Verifier Brain (Section 9, 25)
==============================================================
Stage 1 pe "aankh band karke bharosa" nahi karna — saare factors phir se
independent taur pe check karna. Agar apni jaanch Stage 1 se bahut alag
nikle, candidate reject. 4-5 candidates mein se jo bache, unme se top 2
sabse mazboot chunna.

⚠️ IMPORTANT — "independent" check ki honest limitation:
Asli production system mein Stage 2 ka "independent re-check" iska matlab
hota hai ki thodi der baad (jab data thoda fresh ho chuka ho) dobara same
calculation chale, taaki agar Stage 1 ka signal ek fluke tha (ek candle ka
noise), wo Stage 2 mein pakड़ा jaaye. Abhi ye function sirf SAME data pe
dobara run karta hai (deterministic hai, matlab same input = same output)
— isliye abhi ke liye ye "independent" check nahi hai, sirf ek CONSISTENCY
check hai (ki calculation sahi se dobara ho rahi hai, koi random bug nahi).

Jab live data feed lagega (Phase 2/3), tab Stage 2 ko ek chhota time-gap
(jaise 1-2 minute baad ka fresh data) dena hoga taaki ye asal mein
"independent" bane. Ye ek KNOWN GAP hai, chhupaya nahi ja raha.
"""

try:
    from config.thresholds import PIPELINE
    from pipeline.stage1_scanner import run_scanner
except ImportError:
    raise ImportError("Repo ROOT se chalao, 'pipeline/' ke andar se nahi.")


def verify_single(stage1_result: dict, df, vix_series=None, **scanner_kwargs) -> dict:
    """
    Ek candidate ka Stage 1 result leke usko phir se check karta hai.

    Args:
        stage1_result: pipeline.stage1_scanner.run_scanner() ka output
        df, vix_series, **scanner_kwargs: run_scanner() ko dobara call
            karne ke liye same arguments (dekho upar ka honest note —
            abhi ye same data pe re-run hai, truly independent nahi)

    Returns:
        dict:
            'passed_stage2': bool
            'stage1_score': float
            'stage2_score': float
            'score_divergence': float
            'verifier_notes': list
            'stage1_result': original stage1 result (reference ke liye)
            'stage2_result': fresh re-check ka result
    """
    verifier_notes = []

    if not stage1_result.get("passed_stage1", False):
        return {
            "passed_stage2": False,
            "stage1_score": 0, "stage2_score": 0, "score_divergence": None,
            "verifier_notes": ["Stage 1 khud hi fail hua tha — Stage 2 tak pahuncha hi nahi"],
            "stage1_result": stage1_result, "stage2_result": None,
        }

    # Independent (structurally — dekho upar ka note) re-check
    stage2_result = run_scanner(df, vix_series=vix_series, **scanner_kwargs)

    stage1_score = stage1_result["meta_brain_result"]["final_score"]
    stage2_score = stage2_result["meta_brain_result"]["final_score"]
    score_divergence = abs(stage1_score - stage2_score)

    stage1_direction = stage1_result["meta_brain_result"]["final_decision"]
    stage2_direction = stage2_result["meta_brain_result"]["final_decision"]

    # Check 1: Stage 2 apna minimum confidence bhi pass kare
    if stage2_score < PIPELINE["STAGE2_MIN_CONFIDENCE"]:
        verifier_notes.append(
            f"Stage 2 score {stage2_score} < minimum {PIPELINE['STAGE2_MIN_CONFIDENCE']} — reject"
        )
        return {
            "passed_stage2": False,
            "stage1_score": stage1_score, "stage2_score": stage2_score,
            "score_divergence": score_divergence,
            "verifier_notes": verifier_notes,
            "stage1_result": stage1_result, "stage2_result": stage2_result,
        }

    # Check 2: Direction match honi chahiye
    if stage1_direction != stage2_direction:
        verifier_notes.append(
            f"Direction mismatch: Stage 1 ne '{stage1_direction}' bola, "
            f"Stage 2 ne '{stage2_direction}' — disagreement, reject"
        )
        return {
            "passed_stage2": False,
            "stage1_score": stage1_score, "stage2_score": stage2_score,
            "score_divergence": score_divergence,
            "verifier_notes": verifier_notes,
            "stage1_result": stage1_result, "stage2_result": stage2_result,
        }

    # Check 3: Score divergence threshold (Section 25)
    if score_divergence > PIPELINE["STAGE2_MAX_DIVERGENCE_FROM_STAGE1"]:
        verifier_notes.append(
            f"Score divergence {score_divergence:.1f} > allowed "
            f"{PIPELINE['STAGE2_MAX_DIVERGENCE_FROM_STAGE1']} — bahut zyada farak, reject"
        )
        return {
            "passed_stage2": False,
            "stage1_score": stage1_score, "stage2_score": stage2_score,
            "score_divergence": score_divergence,
            "verifier_notes": verifier_notes,
            "stage1_result": stage1_result, "stage2_result": stage2_result,
        }

    verifier_notes.append(
        f"Stage 2 confirm: score {stage2_score} (divergence {score_divergence:.1f}), "
        f"direction '{stage2_direction}' match hua"
    )

    return {
        "passed_stage2": True,
        "stage1_score": stage1_score, "stage2_score": stage2_score,
        "score_divergence": score_divergence,
        "verifier_notes": verifier_notes,
        "stage1_result": stage1_result, "stage2_result": stage2_result,
    }


def select_top_candidates(verified_candidates: dict, top_n: int = 2) -> list:
    """
    Multiple symbols ke verify_single() results se top N (default 2)
    sabse mazboot candidates chunta hai (Section 9: "jo bachein unme se
    top 2 sabse mazboot chunna").

    Args:
        verified_candidates: dict {symbol_name: verify_single() output}
        top_n: kitne candidates chahiye (default 2, Section 9 ke hisaab se)

    Returns:
        list of (symbol_name, verified_result) tuples, score ke hisaab se
        sorted (highest pehle) — sirf jo passed_stage2=True hain
    """
    passed = {
        symbol: result
        for symbol, result in verified_candidates.items()
        if result.get("passed_stage2", False)
    }

    sorted_candidates = sorted(
        passed.items(),
        key=lambda item: item[1]["stage2_score"],
        reverse=True,
    )

    return sorted_candidates[:top_n]


# ============================================================
# QUICK MANUAL TEST
# Chalane ka tarika: repo ROOT se → python3 -m pipeline.stage2_verifier
# ============================================================
if __name__ == "__main__":
    import numpy as np
    import pandas as pd
    from pipeline.stage1_scanner import run_scanner

    def make_synthetic_df(seed, trend_strength):
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

    print("=== Stage 2 Verifier Test — 2 candidates ===\n")

    df1, vix1 = make_synthetic_df(seed=42, trend_strength=20)
    stage1_a = run_scanner(df1, vix_series=vix1)
    verified_a = verify_single(stage1_a, df1, vix_series=vix1)

    df2, vix2 = make_synthetic_df(seed=99, trend_strength=5)  # weaker trend
    stage1_b = run_scanner(df2, vix_series=vix2)
    verified_b = verify_single(stage1_b, df2, vix_series=vix2)

    print("Candidate A:", "PASS" if verified_a["passed_stage2"] else "FAIL", verified_a["verifier_notes"])
    print("Candidate B:", "PASS" if verified_b["passed_stage2"] else "FAIL", verified_b["verifier_notes"])

    top = select_top_candidates({"SYMBOL_A": verified_a, "SYMBOL_B": verified_b}, top_n=2)
    print(f"\nTop candidates selected: {[name for name, _ in top]}")

    print("\n✅ Test complete — koi crash nahi hua.")
    print("⚠️ Ye SYNTHETIC data hai, real market data se numbers alag honge.")
  
