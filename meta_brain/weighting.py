"""
Tiger Brain V6+V7 — Meta-Brain (Section 5, 24)
=================================================
5 sub-brains ke votes ko regime-based weights se combine karke final
decision deta hai. Veto power (Section 5.3) sabse pehle check hota hai —
agar Vol-Arb hard_veto de, to baaki sab kuch irrelevant ho jaata hai.

Formula (Section 5.2):
    Final Score = Σ (Sub-Brain Vote × Confidence × Regime Weight)
    Score >= threshold (65) -> BUY/SELL signal
    Clear winner nahi -> NO_TRADE
"""

try:
    from config.thresholds import (
        META_BRAIN,
        META_BRAIN_WEIGHTS,
        DECISION_SCORE_THRESHOLD,
    )
except ImportError:
    raise ImportError("Repo ROOT se chalao, 'meta_brain/' ke andar se nahi.")


REGIME_KEY_MAP = {
    "STRONG_TREND": "STRONG_TREND",
    "WEAK_TREND": "WEAK_TREND",
    "RANGE": "RANGE",
    "COMPRESSION": "COMPRESSION",
    "HIGH_VOL": "HIGH_VOL",
    "LOW_VOL": "WEAK_TREND",   # weight table mein Low-Vol ka alag row nahi hai,
                                # WEAK_TREND jaisa balanced weight use karte hain
    "NORMAL": "WEAK_TREND",
    "EVENT_NEARBY": "EVENT_NEARBY",
}


def _participates(vote_data: dict) -> bool:
    """
    Brain tabhi score ke denominator mein ginta hai jab uske paas data ho
    aur regime uske liye bilkul hi bekaar na ho. Warna ek chup brain
    (jaise IV feed ke bina Vol-Arb) poore score ko neeche kheench leta hai.
    """
    if not vote_data.get("data_available", True):
        return False
    min_fit = META_BRAIN["MIN_REGIME_FIT_TO_PARTICIPATE"]
    return vote_data.get("regime_fit", 100) >= min_fit


def decide(
    sub_brain_votes: dict,
    current_regime: str,
    score_threshold: float = DECISION_SCORE_THRESHOLD,
) -> dict:
    """
    Args:
        sub_brain_votes: dict jisme keys hain sub-brain names aur values
            un sub-brains ke evaluate() output dicts hain:
            {
                "trend_follow": {"vote": "BUY", "confidence": 80, ...},
                "mean_reversion": {"vote": "NO_TRADE", "confidence": 20, ...},
                "breakout": {...},
                "vol_arb": {..., "hard_veto": False},
                "range_scalp": {...},
            }
        current_regime: regime/classifier.py se aaya string

    Returns:
        dict:
            'final_decision': 'BUY' | 'SELL' | 'NO_TRADE'
            'final_score': float (0-100, direction-agnostic magnitude)
            'raw_weighted_sum': float (-100 to +100, direction bhi batata hai)
            'veto_triggered': bool
            'veto_reason': str | None
            'contributing_factors': dict — har sub-brain ne kitna contribute kiya
            'regime_used': str — konsa weight-table row use hua
    """
    # --- STEP 1: Veto Check (sabse pehle, Section 5.3) ---
    vol_arb_result = sub_brain_votes.get("vol_arb", {})
    if vol_arb_result.get("hard_veto", False):
        return {
            "final_decision": "NO_TRADE",
            "final_score": 100.0,
            "raw_weighted_sum": 0.0,
            "veto_triggered": True,
            "veto_reason": (
                vol_arb_result.get("reasoning_tags", ["Vol-Arb hard veto"])[0]
            ),
            "contributing_factors": {},
            "regime_used": None,
        }

    # --- STEP 2: Regime Weight Table Select Karna ---
    weight_key = REGIME_KEY_MAP.get(current_regime, "WEAK_TREND")
    weights = META_BRAIN_WEIGHTS.get(weight_key)
    if weights is None:
        raise ValueError(f"Regime '{current_regime}' ke liye weight table nahi mila.")

    # --- STEP 3: Weighted Sum Calculate Karna ---
    raw_weighted_sum = 0.0
    participating_weight = 0.0
    contributing_factors = {}

    for brain_name, weight in weights.items():
        vote_data = sub_brain_votes.get(brain_name)
        if vote_data is None:
            continue  # ye sub-brain evaluate hi nahi hua, skip

        participates = _participates(vote_data)
        if participates:
            participating_weight += weight
        else:
            contributing_factors[brain_name] = {
                "vote": vote_data.get("vote", "NO_TRADE"),
                "confidence": vote_data.get("confidence", 0),
                "weight": weight,
                "contribution": 0.0,
                "participates": False,
            }
            continue

        vote = vote_data.get("vote", "NO_TRADE")
        confidence = vote_data.get("confidence", 0)

        if vote == "BUY":
            direction = 1
        elif vote == "SELL":
            direction = -1
        else:
            direction = 0

        contribution = direction * confidence * weight
        raw_weighted_sum += contribution
        contributing_factors[brain_name] = {
            "vote": vote,
            "confidence": confidence,
            "weight": weight,
            "contribution": round(contribution, 2),
            "participates": participates,
        }

    # Score = participating brains ka weighted AVERAGE confidence, taaki
    # data-gap wale brains score ko structurally cap na kar dein.
    if META_BRAIN["NORMALISE_BY_PARTICIPATING_WEIGHT"] and participating_weight > 0:
        final_score = round(abs(raw_weighted_sum) / participating_weight, 2)
    else:
        final_score = round(abs(raw_weighted_sum), 2)

    # --- STEP 4: Final Decision (Section 5.2) ---
    # raw sum 0 ka matlab kisi ne direction di hi nahi — threshold 0 ho to
    # bhi ise SELL nahi banana
    if final_score >= score_threshold and raw_weighted_sum != 0:
        final_decision = "BUY" if raw_weighted_sum > 0 else "SELL"
    else:
        final_decision = "NO_TRADE"

    return {
        "final_decision": final_decision,
        "final_score": final_score,
        "raw_weighted_sum": round(raw_weighted_sum, 2),
        "veto_triggered": False,
        "veto_reason": None,
        "contributing_factors": contributing_factors,
        "regime_used": weight_key,
        "participating_weight": round(participating_weight, 2),
        "score_threshold": score_threshold,
    }


# ============================================================
# QUICK MANUAL TEST — mock sub-brain votes se
# Chalane ka tarika: repo ROOT se → python3 -m meta_brain.weighting
# ============================================================
if __name__ == "__main__":
    print("=== Meta-Brain Test 1: Strong Trend, sab BUY bol rahe hain ===")
    mock_votes_1 = {
        "trend_follow": {"vote": "BUY", "confidence": 85, "reasoning_tags": []},
        "mean_reversion": {"vote": "NO_TRADE", "confidence": 10, "reasoning_tags": []},
        "breakout": {"vote": "BUY", "confidence": 60, "reasoning_tags": []},
        "vol_arb": {"vote": "NO_TRADE", "confidence": 30, "hard_veto": False, "reasoning_tags": []},
        "range_scalp": {"vote": "NO_TRADE", "confidence": 0, "reasoning_tags": []},
    }
    result1 = decide(mock_votes_1, current_regime="STRONG_TREND")
    for k, v in result1.items():
        print(f"{k}: {v}")

    print("\n=== Meta-Brain Test 2: Vol-Arb Hard Veto (sab bullish ho phir bhi NO_TRADE) ===")
    mock_votes_2 = {
        "trend_follow": {"vote": "BUY", "confidence": 90, "reasoning_tags": []},
        "breakout": {"vote": "BUY", "confidence": 85, "reasoning_tags": []},
        "vol_arb": {
            "vote": "NO_TRADE", "confidence": 100, "hard_veto": True,
            "reasoning_tags": ["🚫 HARD VETO: Event 6hr mein hai + IV bahut high"],
        },
    }
    result2 = decide(mock_votes_2, current_regime="STRONG_TREND")
    for k, v in result2.items():
        print(f"{k}: {v}")

    print("\n=== Meta-Brain Test 3: Range regime, mixed signals -> NO_TRADE ===")
    mock_votes_3 = {
        "trend_follow": {"vote": "BUY", "confidence": 40, "reasoning_tags": []},
        "mean_reversion": {"vote": "SELL", "confidence": 45, "reasoning_tags": []},
        "breakout": {"vote": "NO_TRADE", "confidence": 0, "reasoning_tags": []},
        "vol_arb": {"vote": "NO_TRADE", "confidence": 20, "hard_veto": False, "reasoning_tags": []},
        "range_scalp": {"vote": "SELL", "confidence": 55, "reasoning_tags": []},
    }
    result3 = decide(mock_votes_3, current_regime="RANGE")
    for k, v in result3.items():
        print(f"{k}: {v}")

    print("\n✅ Test complete — koi crash nahi hua.")
  
