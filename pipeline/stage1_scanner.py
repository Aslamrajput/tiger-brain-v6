"""
Tiger Brain V6+V7 — STAGE 1: Scanner Brain (Section 9)
=========================================================
Ye pehla stage hai jo sab kuch (regime, 5 sub-brains, meta-brain) ko ek
saath jodta hai aur ek candidate ka pehla "research verdict" deta hai.

Kaam: Har candidate (symbol) pe pura deep research (regime detect +
sub-brain votes + meta-brain decision), phir Stage 1 ka pass/fail
threshold check karna (Section 25: confidence >= 50).

⚠️ IMPORTANT — abhi ye function sirf ek symbol ke liye kaam karta hai.
Multiple symbols (Dynamic Universe Selection se aaye 4-5 candidates) ko
loop mein isi function se guzarna hoga — wo orchestration Universe
Selection module (jo abhi nahi bana) ya ek top-level runner script mein
hoga, Phase 2/3 mein.
"""

try:
    from config.thresholds import PIPELINE
    from regime.classifier import classify_regime
    from subbrains import trend_follow, mean_reversion, breakout, vol_arb, range_scalp
    from meta_brain.weighting import decide as meta_brain_decide
except ImportError:
    raise ImportError("Repo ROOT se chalao, 'pipeline/' ke andar se nahi.")


def run_scanner(
    df,
    vix_series=None,
    oi_buildup_confirmed=None,
    near_fresh_zone=None,
    oi_new_buildup_confirmed=None,
    iv_series=None,
    hours_to_next_event=None,
    put_iv=None,
    call_iv=None,
    bid_ask_spread_normal=True,
    is_index=True,
    score_threshold=None,
    min_stage1_confidence=None,
) -> dict:
    """
    Ek candidate (symbol) ka pura Stage-1 research karta hai.

    Args:
        df: OHLCV DataFrame us symbol ka (regime + trend/mean-rev/breakout/
            range-scalp sub-brains ke liye)
        vix_series: India VIX history (regime classifier ke liye, optional)
        oi_buildup_confirmed, near_fresh_zone, oi_new_buildup_confirmed:
            Sub-brains ke optional data gaps (jaisa unke apne files mein
            documented hai — abhi None default rahega jab tak OI/zone
            data feed connect nahi hota)
        iv_series, hours_to_next_event, put_iv, call_iv: Vol-Arb sub-brain
            ke liye (agar options-chain data available hai)
        bid_ask_spread_normal: Range-Scalp sub-brain ke liye
        is_index: Trend-Follow sub-brain ke VWAP threshold ke liye

    Returns:
        dict:
            'passed_stage1': bool
            'regime': regime classifier ka poora output
            'sub_brain_votes': har sub-brain ka vote (Section 4 format)
            'meta_brain_result': meta_brain.decide() ka poora output
            'stage1_notes': list — kya missing tha ya kya observation hai
    """
    stage1_notes = []

    # --- STEP 1: Regime Classify Karna ---
    try:
        regime_result = classify_regime(df, vix_series=vix_series)
    except ValueError as exc:
        return {
            "passed_stage1": False,
            "regime": None,
            "sub_brain_votes": {},
            "meta_brain_result": None,
            "stage1_notes": [f"Regime classify nahi ho paya: {exc}"],
        }

    current_regime = regime_result["regime"]

    # --- STEP 2: Saare 5 Sub-Brains Evaluate Karna ---
    sub_brain_votes = {}

    sub_brain_votes["trend_follow"] = trend_follow.evaluate(
        df, current_regime, oi_buildup_confirmed=oi_buildup_confirmed, is_index=is_index
    )
    sub_brain_votes["mean_reversion"] = mean_reversion.evaluate(
        df, current_regime, near_fresh_zone=near_fresh_zone
    )
    sub_brain_votes["breakout"] = breakout.evaluate(
        df, current_regime, oi_new_buildup_confirmed=oi_new_buildup_confirmed
    )
    sub_brain_votes["range_scalp"] = range_scalp.evaluate(
        df, current_regime, bid_ask_spread_normal=bid_ask_spread_normal
    )

    if iv_series is not None and len(iv_series) >= 20:
        sub_brain_votes["vol_arb"] = vol_arb.evaluate(
            iv_series,
            hours_to_next_event=hours_to_next_event,
            put_iv=put_iv,
            call_iv=call_iv,
        )
    else:
        stage1_notes.append(
            "⚠️ IV history available nahi thi (ya 20 din se kam) — Vol-Arb "
            "sub-brain skip hua. Iska matlab IV-crush veto check NAHI hua "
            "— ye ek risk hai, options data feed lagne tak."
        )
        sub_brain_votes["vol_arb"] = {
            "vote": "NO_TRADE", "confidence": 0, "reasoning_tags": [],
            "conflicting_evidence": ["IV data missing"], "regime_fit": 0,
            "hard_veto": False, "data_available": False,
        }

    # --- STEP 3: Meta-Brain Se Final Decision ---
    if score_threshold is None:
        meta_result = meta_brain_decide(sub_brain_votes, current_regime)
    else:
        meta_result = meta_brain_decide(
            sub_brain_votes, current_regime, score_threshold=score_threshold
        )

    # --- STEP 4: Stage 1 Pass/Fail Check (Section 25) ---
    stage1_min = (
        PIPELINE["STAGE1_MIN_CONFIDENCE"]
        if min_stage1_confidence is None
        else min_stage1_confidence
    )
    passed = meta_result["final_score"] >= stage1_min

    if not passed:
        stage1_notes.append(
            f"Stage 1 FAIL: score {meta_result['final_score']} < threshold "
            f"{stage1_min}"
        )

    return {
        "passed_stage1": passed,
        "regime": regime_result,
        "sub_brain_votes": sub_brain_votes,
        "meta_brain_result": meta_result,
        "stage1_notes": stage1_notes,
    }


# ============================================================
# QUICK MANUAL TEST — synthetic uptrend data se (end-to-end pipeline test)
# Chalane ka tarika: repo ROOT se → python3 -m pipeline.stage1_scanner
# ============================================================
if __name__ == "__main__":
    import numpy as np
    import pandas as pd

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

    print("=== Stage 1 Scanner — Full End-to-End Test ===\n")
    result = run_scanner(df_test, vix_series=vix_test)

    print(f"passed_stage1: {result['passed_stage1']}")
    print(f"regime: {result['regime']['regime']}")
    print(f"\nmeta_brain_result:")
    for k, v in result["meta_brain_result"].items():
        print(f"  {k}: {v}")
    print(f"\nstage1_notes: {result['stage1_notes']}")

    print("\n✅ Test complete — poora pipeline (regime + 5 sub-brains + meta-brain) chal raha hai.")
    print("⚠️ Ye SYNTHETIC data hai, real market data se numbers alag honge.")
  
