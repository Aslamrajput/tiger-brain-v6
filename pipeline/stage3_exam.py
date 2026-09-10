"""
Tiger Brain V6+V7 — STAGE 3: Exam Brain (Section 9, 25)
==========================================================
The Stage 3 Exam Brain — the strictest filter before execution. The
Self-Challenge Engine looks for bearish evidence against a bullish
setup (and vice-versa). Any hard-veto factor (IV danger, liquidity,
data quality) triggers immediate rejection regardless of score.

Section 25 sets the highest confidence threshold here: >= 65
(stricter than Stage 1's 50 and Stage 2's 50).
"""

try:
    from config.thresholds import PIPELINE
except ImportError:
    raise ImportError("Repo ROOT se chalao, 'pipeline/' ke andar se nahi.")


def self_challenge(verified_result: dict) -> list:
    """
    Section 9 ka "Self-Challenge Engine" — jo direction Meta-Brain ne
    choose ki hai, uske KHILAF saboot dhundhta hai. Ye har contributing
    sub-brain ke conflicting_evidence ko ikattha karke ek "counter-case"
    banata hai.

    Returns:
        list of str — har bearish/contradicting point (agar BUY hai) ya
        bullish/contradicting point (agar SELL hai)
    """
    stage2_result = verified_result.get("stage2_result")
    if stage2_result is None:
        return ["Stage 2 result missing — self-challenge nahi ho saka"]

    final_decision = stage2_result["meta_brain_result"]["final_decision"]
    counter_evidence = []

    for brain_name, vote_data in stage2_result["sub_brain_votes"].items():
        vote = vote_data.get("vote", "NO_TRADE")

        # Agar final decision BUY hai, to SELL/NO_TRADE votes "counter-evidence" hain
        # Agar final decision SELL hai, to BUY/NO_TRADE votes "counter-evidence" hain
        is_contradicting = (
            (final_decision == "BUY" and vote in ("SELL", "NO_TRADE"))
            or (final_decision == "SELL" and vote in ("BUY", "NO_TRADE"))
        )

        if is_contradicting and vote_data.get("confidence", 0) > 0:
            counter_evidence.append(
                f"[{brain_name}] voted {vote} (confidence {vote_data['confidence']}) — "
                f"contradicts final '{final_decision}' direction"
            )

        # Har sub-brain ka apna conflicting_evidence bhi shamil karna
        for evidence in vote_data.get("conflicting_evidence", []):
            counter_evidence.append(f"[{brain_name}] {evidence}")

    return counter_evidence


def check_hard_vetoes(
    verified_result: dict,
    data_quality_ok: bool = True,
    liquidity_ok: bool = True,
) -> list:
    """
    Section 5.3 ke saare veto powers final baar check karna:
    - Vol-Arb veto (already meta_brain mein check hua, yahan dobara confirm)
    - Data Quality veto (naya — abhi tak kahin check nahi hua tha)
    - Liquidity veto (naya — abhi tak kahin check nahi hua tha)

    Returns:
        list of veto reasons (khaali list = koi veto nahi laga)
    """
    vetoes = []

    stage2_result = verified_result.get("stage2_result")
    if stage2_result and stage2_result["meta_brain_result"].get("veto_triggered"):
        vetoes.append(
            f"Vol-Arb veto (Meta-Brain se): {stage2_result['meta_brain_result']['veto_reason']}"
        )

    if not data_quality_ok:
        vetoes.append("Data Quality veto: data stale/corrupt hai — koi bhi sub-brain override nahi kar sakta")

    if not liquidity_ok:
        vetoes.append("Liquidity veto: bid-ask spread bahut chauda hai — trade rukega chahe signal kitna bhi strong ho")

    return vetoes


def run_exam(
    verified_result: dict,
    data_quality_ok: bool = True,
    liquidity_ok: bool = True,
) -> dict:
    """
    Stage 3 ka main entry point.

    Args:
        verified_result: pipeline.stage2_verifier.verify_single() ka output
        data_quality_ok: True agar data feed reliable hai (broker-side
            check — abhi ye manual flag hai, actual data-quality monitor
            Phase 2/3 mein banega)
        liquidity_ok: True agar bid-ask spread normal hai

    Returns:
        dict:
            'passed_stage3': bool
            'adjusted_score': float — self-challenge ke baad ka score
            'counter_evidence': list — jo saboot direction ke khilaf mile
            'vetoes': list — koi hard-veto laga to yahan
            'exam_notes': list
    """
    exam_notes = []

    if not verified_result.get("passed_stage2", False):
        return {
            "passed_stage3": False, "adjusted_score": 0,
            "counter_evidence": [], "vetoes": [],
            "exam_notes": ["Stage 2 khud fail hua tha — Stage 3 tak pahuncha hi nahi"],
        }

    # --- STEP 1: Hard Veto Check (sabse pehle, kabhi bhi override nahi) ---
    vetoes = check_hard_vetoes(verified_result, data_quality_ok, liquidity_ok)
    if vetoes:
        return {
            "passed_stage3": False, "adjusted_score": 0,
            "counter_evidence": [], "vetoes": vetoes,
            "exam_notes": ["Hard veto laga — score irrelevant, turant reject"],
        }

    # --- STEP 2: Self-Challenge ---
    counter_evidence = self_challenge(verified_result)

    stage2_score = verified_result["stage2_score"]

    # Har counter-evidence point score ko thoda kam karta hai (penalty).
    #
    # ⚠️ KNOWN LIMITATION (honestly flagged): Abhi ye penalty crude hai —
    # ye "genuine contradicting vote" (jaise mean-reversion ne SELL bola
    # jab final BUY hai) aur "data missing warning" (jaise "OI data nahi
    # tha") dono ko BARABAR treat kar raha hai (-3 points har ek). Ye
    # galat hai — missing data hona aur genuinely bearish signal hona do
    # alag cheezein hain. Test mein ye dikha bhi — ek candidate jo Stage
    # 1/2 pass kar chuka tha, sirf isliye Stage 3 mein fail hua kyunki
    # OI/zone/IV data missing tha (jo abhi genuinely nahi hai humare
    # paas), na ki kyunki koi sub-brain genuinely disagree kar raha tha.
    #
    # Phase 2 mein isko fix karna hoga: genuine contradicting votes aur
    # missing-data warnings ko ALAG penalty weight dena chahiye (missing
    # data ka penalty bahut kam hona chahiye ya alag se track hona
    # chahiye, taaki "no data" aur "bad signal" gaddmaddh na ho).
    genuine_contradictions = [
        e for e in counter_evidence if "contradicts final" in e
    ]
    data_gap_warnings = [
        e for e in counter_evidence if "data" in e.lower() and "available nahi" in e.lower()
    ]
    other_notes = [
        e for e in counter_evidence
        if e not in genuine_contradictions and e not in data_gap_warnings
    ]

    penalty = min(
        len(genuine_contradictions) * 5     # genuine disagreement — bada penalty
        + len(other_notes) * 3               # weak-signal observations — medium
        + len(data_gap_warnings) * 1,        # sirf data missing — chhota penalty
        30,
    )
    adjusted_score = max(stage2_score - penalty, 0)

    if penalty > 0:
        exam_notes.append(
            f"{len(counter_evidence)} counter-evidence points mile — "
            f"score {stage2_score} se {adjusted_score} kiya gaya ({penalty} penalty)"
        )

    # --- STEP 3: Final Threshold Check (Section 25 — sabse sakht bar: 65) ---
    passed = adjusted_score >= PIPELINE["STAGE3_MIN_CONFIDENCE"]

    if not passed:
        exam_notes.append(
            f"Stage 3 FAIL: adjusted score {adjusted_score} < threshold "
            f"{PIPELINE['STAGE3_MIN_CONFIDENCE']}"
        )
    else:
        exam_notes.append(f"Stage 3 PASS: adjusted score {adjusted_score}")

    return {
        "passed_stage3": passed,
        "adjusted_score": adjusted_score,
        "counter_evidence": counter_evidence,
        "vetoes": [],
        "exam_notes": exam_notes,
    }


# ============================================================
# QUICK MANUAL TEST
# Chalane ka tarika: repo ROOT se → python3 -m pipeline.stage3_exam
# ============================================================
if __name__ == "__main__":
    import numpy as np
    import pandas as pd
    from pipeline.stage1_scanner import run_scanner
    from pipeline.stage2_verifier import verify_single

    np.random.seed(42)
    n = 60
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    base_price = 100
    trend = np.linspace(0, 20, n)
    noise = np.random.normal(0, 0.5, n)
    close_prices = base_price + trend + noise.cumsum() * 0.2

    df_test = pd.DataFrame(index=dates)
    df_test["close"] = close_prices
    df_test["open"] = df_test["close"].shift(1).fillna(base_price)
    df_test["high"] = df_test[["open", "close"]].max(axis=1) + np.random.uniform(0.2, 0.8, n)
    df_test["low"] = df_test[["open", "close"]].min(axis=1) - np.random.uniform(0.2, 0.8, n)
    df_test["volume"] = np.random.randint(200000, 400000, n)
    df_test.loc[df_test.index[-1], "volume"] = 700000
    vix_test = pd.Series(np.random.uniform(13, 16, n), index=dates)

    print("=== Stage 3 Exam Test — Normal case ===")
    stage1 = run_scanner(df_test, vix_series=vix_test)
    verified = verify_single(stage1, df_test, vix_series=vix_test)
    exam_result = run_exam(verified, data_quality_ok=True, liquidity_ok=True)
    for k, v in exam_result.items():
        print(f"{k}: {v}")

    print("\n=== Stage 3 Exam Test — Data Quality Veto case ===")
    exam_result2 = run_exam(verified, data_quality_ok=False, liquidity_ok=True)
    for k, v in exam_result2.items():
        print(f"{k}: {v}")

    print("\n✅ Test complete — koi crash nahi hua.")
    print("⚠️ Ye SYNTHETIC data hai, real market data se numbers alag honge.")
      
