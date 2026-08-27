"""
Tiger Brain V6+V7 — Dynamic Universe Selection Engine (Section 7, 28, 31)
============================================================================
Har scan cycle (15-30 min) mein batata hai: aaj/abhi kaunse top 4-5
symbols pe focus karna hai (Section 7, 28), aur Index vs Stock Options
mein se kaunsa better fit hai (Section 31, V7.0).

⚠️ IMPORTANT — abhi live data feed nahi hai:
Ye module khud data fetch NAHI karta — wo `data/loader.py` (aur aage
broker ke live market-data API, jab connect hoga) ka kaam hai. Ye module
sirf ek "metrics dict" leta hai (jisme volume, OI velocity, ATR,
liquidity spread already calculated hone chahiye) aur usko score/rank
karta hai. Isse testing aasan hai aur data-fetching logic se decoupled
rehta hai.
"""

try:
    from config.thresholds import UNIVERSE
except ImportError:
    raise ImportError("Repo ROOT se chalao, 'universe/' ke andar se nahi.")


# ============================================================
# SECTION 7, 28 — SYMBOL SCORING (Dynamic Universe Selection)
# ============================================================

def score_symbol(metrics: dict) -> dict:
    """
    Ek symbol ka composite score nikalta hai (Section 28 formula).

    Args:
        metrics: dict jisme ye keys honi chahiye (sab 0-100 scale pe
                 pehle se normalize/rank kiye hue honge, caller ki
                 zimmedari hai ye normalize karna):
            'volume_rank': 0-100 (100 = sabse zyada volume aaj)
            'oi_velocity_rank': 0-100
            'volatility_rank': 0-100
            'liquidity_rank': 0-100
            'spread_pct_of_premium': float (Section 28 ka liquidity gate check)

    Returns:
        dict:
            'composite_score': float (0-100)
            'passes_liquidity_gate': bool
            'breakdown': dict — har factor ka weighted contribution
    """
    weights = UNIVERSE["SCORE_WEIGHTS"]

    breakdown = {
        "volume_rank": metrics.get("volume_rank", 0) * weights["volume_rank"],
        "oi_velocity": metrics.get("oi_velocity_rank", 0) * weights["oi_velocity"],
        "volatility_rank": metrics.get("volatility_rank", 0) * weights["volatility_rank"],
        "liquidity_rank": metrics.get("liquidity_rank", 0) * weights["liquidity_rank"],
    }

    composite_score = sum(breakdown.values())

    spread_pct = metrics.get("spread_pct_of_premium", 0)
    passes_liquidity_gate = spread_pct < UNIVERSE["MIN_LIQUIDITY_SPREAD_PCT_OF_PREMIUM"]

    return {
        "composite_score": round(composite_score, 2),
        "passes_liquidity_gate": passes_liquidity_gate,
        "breakdown": {k: round(v, 2) for k, v in breakdown.items()},
    }


def select_top_symbols(candidates: dict, top_n: int = None) -> list:
    """
    Saare candidates ko score karke top N (default config se, usually 5)
    chunta hai. Jo liquidity gate fail karte hain, unko hata diya jaata hai
    chahe unka score kitna bhi accha ho (Section 28: "chahe kitna bhi
    accha score ho, exclude").

    Args:
        candidates: dict {symbol_name: metrics_dict}
        top_n: kitne symbols chahiye (default config se)

    Returns:
        list of (symbol_name, score_result) tuples, sorted highest-first
    """
    top_n = top_n or UNIVERSE["TOP_N_SYMBOLS"]

    scored = {}
    excluded = []

    for symbol, metrics in candidates.items():
        result = score_symbol(metrics)
        if result["passes_liquidity_gate"]:
            scored[symbol] = result
        else:
            excluded.append(symbol)

    sorted_symbols = sorted(
        scored.items(), key=lambda item: item[1]["composite_score"], reverse=True
    )

    return sorted_symbols[:top_n]


# ============================================================
# SECTION 31 (V7.0) — INDEX vs STOCK AUTO-DECISION ENGINE
# ============================================================

def score_asset_class(
    regime_clarity: float,
    confluence: float,
    liquidity_score: float,
    risk_reward: float,
) -> float:
    """
    Section 31 formula: Asset-Class Score = Regime Clarity + Confluence +
    Liquidity/Spread + Risk-Reward. Sab 0-100 scale pe input lena hai,
    equal-weighted average deta hai (blueprint mein exact weights nahi
    diye the, isliye simple average use kar rahe hain — Phase 2 backtest
    se ye weights bhi calibrate ho sakte hain).

    Returns:
        float — composite asset-class score (0-100)
    """
    return round((regime_clarity + confluence + liquidity_score + risk_reward) / 4, 2)


def choose_asset_class(index_metrics: dict, stock_candidates: dict) -> dict:
    """
    Index vs har stock candidate ka score compare karke best chunta hai.
    Hardcoded "sirf index" ya "sirf stock" bias nahi — jo zyada score
    kare, wahi jeetega (Section 31).

    Args:
        index_metrics: dict with 'regime_clarity', 'confluence',
                        'liquidity_score', 'risk_reward' (Index ke liye)
        stock_candidates: dict {stock_symbol: same-shape metrics dict}

    Returns:
        dict:
            'chosen': str — 'INDEX' ya stock symbol name
            'chosen_score': float
            'all_scores': dict — sabka score (comparison ke liye)
    """
    all_scores = {}

    all_scores["INDEX"] = score_asset_class(**index_metrics)

    for symbol, metrics in stock_candidates.items():
        all_scores[symbol] = score_asset_class(**metrics)

    best = max(all_scores.items(), key=lambda item: item[1])

    return {
        "chosen": best[0],
        "chosen_score": best[1],
        "all_scores": all_scores,
    }


# ============================================================
# QUICK MANUAL TEST
# Chalane ka tarika: repo ROOT se → python3 -m universe.selector
# ============================================================
if __name__ == "__main__":
    print("=== Test 1: Symbol Scoring + Top-N Selection ===\n")
    mock_candidates = {
        "NIFTY": {
            "volume_rank": 95, "oi_velocity_rank": 90, "volatility_rank": 70,
            "liquidity_rank": 100, "spread_pct_of_premium": 0.3,
        },
        "BANKNIFTY": {
            "volume_rank": 85, "oi_velocity_rank": 95, "volatility_rank": 80,
            "liquidity_rank": 95, "spread_pct_of_premium": 0.4,
        },
        "RELIANCE": {
            "volume_rank": 60, "oi_velocity_rank": 40, "volatility_rank": 50,
            "liquidity_rank": 70, "spread_pct_of_premium": 0.8,
        },
        "SOME_ILLIQUID_STOCK": {
            "volume_rank": 90, "oi_velocity_rank": 85, "volatility_rank": 90,
            "liquidity_rank": 20, "spread_pct_of_premium": 2.5,  # liquidity gate FAIL
        },
    }

    top = select_top_symbols(mock_candidates, top_n=3)
    for symbol, result in top:
        print(f"{symbol}: score={result['composite_score']}, breakdown={result['breakdown']}")

    print("\n(Note: 'SOME_ILLIQUID_STOCK' high score ke bawajood exclude hua — liquidity gate fail)")

    print("\n=== Test 2: Index vs Stock Auto-Decision ===\n")
    index_metrics = {
        "regime_clarity": 85, "confluence": 70, "liquidity_score": 95, "risk_reward": 65,
    }
    stock_candidates = {
        "RELIANCE": {"regime_clarity": 60, "confluence": 90, "liquidity_score": 70, "risk_reward": 80},
        "TCS": {"regime_clarity": 50, "confluence": 40, "liquidity_score": 65, "risk_reward": 55},
    }
    decision = choose_asset_class(index_metrics, stock_candidates)
    for k, v in decision.items():
        print(f"{k}: {v}")

    print("\n✅ Test complete — koi crash nahi hua.")
  
