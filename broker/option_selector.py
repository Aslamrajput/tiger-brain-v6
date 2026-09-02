"""
Tiger Brain V6.1 — BRAIN 3: Option Chain & Greeks/OI Velocity Selector
=======================================================================
Teesra brain. Brain 2 ne direction diya (BUY setup = call side, SELL
setup = put side). Ab ye brain decide karta hai ki KAUNSA option contract
kharidna hai:

  - Option type: direction se (BUY→CE, SELL→PE)
  - Strike: ATM/ITM preference (delta band ke andar)
  - Expiry: gamma/theta-safe window ke andar
  - Liquidity gates: spread aur OI minimum
  - OI velocity: jis strike pe OI tezi se badh raha hai use prefer karna

Input ek "chain snapshot" dict hai (live ya simulated — Angel One se aayega):

    {
        "underlying_price": float,
        "expiry_days": int,          # nearest expiry tak din
        "contracts": [
            {
                "strike": float,
                "option_type": "CE" | "PE",
                "ltp": float,               # premium
                "bid": float, "ask": float,
                "open_interest": float,
                "oi_change_pct": float,    # OI velocity (recent % change)
                "iv": float | None,         # implied volatility (optional)
                "delta": float | None,      # broker se aaye to warna estimate
            }, ...
        ]
    }

Delta estimate (jab broker delta nahi deta): moneyness-based approximation
— ye Black-Scholes nahi hai, honest heuristic hai; jab tak real chain data
aayega, kaafi kaam karta hai.
"""

from __future__ import annotations

import logging

try:
    from config.thresholds import BRAIN3
except ImportError:
    raise ImportError("Repo ROOT se chalao, 'broker/' ke andar se nahi.")

logger = logging.getLogger("tiger_brain.option_selector")


def estimate_delta(contract: dict, underlying_price: float, option_type: str) -> float:
    """
    Delta estimate jab broker chain mein delta nahi aata.
    ATM = ~0.5; ITM ki taraf ~1.0; OTM ki taraf ~0.0 (CE convention).
    """
    if contract.get("delta") is not None:
        return float(contract["delta"])

    strike = float(contract["strike"])
    moneyness = (underlying_price - strike) / underlying_price  # +ve = ITM for CE

    if option_type == "CE":
        raw = 0.5 + moneyness * 5.0  # 1% ITM ≈ +0.05 delta
        return max(0.01, min(0.99, raw))
    else:  # PE: moneyness ulta
        raw = 0.5 - moneyness * 5.0
        return max(0.01, min(0.99, raw))


def _spread_ok(contract: dict) -> bool:
    """Bid-ask spread premium ke % mein — BRAIN3 limit ke andar hona chahiye."""
    bid, ask, ltp = contract.get("bid"), contract.get("ask"), contract.get("ltp")
    if bid is None or ask is None or ltp is None or ltp <= 0 or ask <= bid:
        return False
    spread_pct = (ask - bid) / ltp * 100
    return spread_pct <= BRAIN3["MAX_SPREAD_PCT_OF_PREMIUM"]


def select_option(chain_snapshot: dict, direction: str) -> dict:
    """
    Brain 3 ka main entry point. Direction ke hisaab se best option
    contract select karta hai.

    Args:
        chain_snapshot: upar documented format ka dict
        direction: 'BUY' (call) ya 'SELL' (put) — Brain 2 se

    Returns:
        dict:
            'selected': bool
            'contract': dict | None — chosen contract dict (copy)
            'delta': float | None
            'reasons': list — rejection/preference reasons
            'oi_velocity_note': str | None
    """
    reasons = []
    underlying = chain_snapshot.get("underlying_price")
    contracts = chain_snapshot.get("contracts", [])

    if underlying is None or not contracts:
        return {"selected": False, "contract": None, "delta": None,
                "reasons": ["chain snapshot mein underlying/contracts missing"], "oi_velocity_note": None}

    if direction not in ("BUY", "SELL"):
        return {"selected": False, "contract": None, "delta": None,
                "reasons": [f"direction '{direction}' invalid hai — BUY/SELL chahiye"], "oi_velocity_note": None}

    option_type = "CE" if direction == "BUY" else "PE"
    expiry_days = chain_snapshot.get("expiry_days")

    if expiry_days is not None:
        if expiry_days < BRAIN3["MIN_DAYS_TO_EXPIRY"]:
            reasons.append(
                f"expiry {expiry_days} din — minimum {BRAIN3['MIN_DAYS_TO_EXPIRY']} chahiye "
                f"(gamma/theta burn) — chain reject"
            )
            return {"selected": False, "contract": None, "delta": None,
                    "reasons": reasons, "oi_velocity_note": None}
        if expiry_days > BRAIN3["MAX_DAYS_TO_EXPIRY"]:
            reasons.append(
                f"expiry {expiry_days} din — maximum {BRAIN3['MAX_DAYS_TO_EXPIRY']} "
                f"(far-month illiquidity) — chain reject"
            )
            return {"selected": False, "contract": None, "delta": None,
                    "reasons": reasons, "oi_velocity_note": None}

    # --- Step 1: option-type + liquidity gates ---
    candidates = []
    for c in contracts:
        if c.get("option_type") != option_type:
            continue

        if not _spread_ok(c):
            reasons.append(
                f"strike {c.get('strike')} ({option_type}) — spread gate fail"
            )
            continue

        oi = c.get("open_interest", 0) or 0
        if oi < BRAIN3["MIN_OPEN_INTEREST"]:
            reasons.append(
                f"strike {c.get('strike')} ({option_type}) — OI {oi} < {BRAIN3['MIN_OPEN_INTEREST']}"
            )
            continue

        if c.get("ltp") is None or c.get("ltp", 0) <= 0:
            reasons.append(f"strike {c.get('strike')} — premium missing/zero")
            continue

        candidates.append(c)

    if not candidates:
        reasons.append("koi bhi contract liquidity gates pass nahi kar paya")
        return {"selected": False, "contract": None, "delta": None,
                "reasons": reasons, "oi_velocity_note": None}

    # --- Step 2: delta band filter (ATM/slightly-ITM preference) ---
    # PE deltas negative hote hain — magnitude check karo, sign nahi.
    in_band = []
    for c in candidates:
        delta = estimate_delta(c, underlying, option_type)
        if BRAIN3["MIN_DELTA"] <= abs(delta) <= BRAIN3["MAX_DELTA"]:
            in_band.append((c, abs(delta)))
        else:
            reasons.append(
                f"strike {c.get('strike')} — delta {delta:.2f} band "
                f"[{BRAIN3['MIN_DELTA']}, {BRAIN3['MAX_DELTA']}] ke bahar"
            )

    if not in_band:
        reasons.append("delta band mein koi contract nahi mila")
        return {"selected": False, "contract": None, "delta": None,
                "reasons": reasons, "oi_velocity_note": None}

    # --- Step 3: OI velocity preference (hot strikes ko priority) ---
    def score(c_and_delta):
        c, delta = c_and_delta
        oi_vel = c.get("oi_change_pct", 0) or 0
        # Score: OI velocity dominant, delta-band center pe thoda bonus
        delta_center_bonus = 1 - abs(delta - (BRAIN3["MIN_DELTA"] + BRAIN3["MAX_DELTA"]) / 2)
        return oi_vel * 1.0 + delta_center_bonus * 5.0

    best, best_delta = max(in_band, key=score)
    best_oi_vel = best.get("oi_change_pct", 0) or 0
    hot = best_oi_vel >= BRAIN3["OI_VELOCITY_HOT_PCT"]

    selected = dict(best)
    selected["estimated_delta"] = round(best_delta, 3)

    return {
        "selected": True,
        "contract": selected,
        "delta": round(best_delta, 3),
        "reasons": reasons + [
            f"strike {best['strike']} ({option_type}) selected — "
            f"delta {best_delta:.2f}, OI velocity {best_oi_vel:.1f}%"
        ],
        "oi_velocity_note": (
            f"hot strike (OI velocity {best_oi_vel:.1f}% >= {BRAIN3['OI_VELOCITY_HOT_PCT']}%)"
            if hot else f"OI velocity {best_oi_vel:.1f}% — normal"
        ),
    }


# ============================================================
# QUICK MANUAL TEST — repo ROOT se: python3 -m broker.option_selector
# ============================================================
if __name__ == "__main__":
    snapshot = {
        "underlying_price": 25000.0,
        "expiry_days": 5,
        "contracts": [
            # CE contracts — sabse liquid + hot OI 25100 pe
            {"strike": 25000, "option_type": "CE", "ltp": 180, "bid": 179, "ask": 181,
             "open_interest": 5000, "oi_change_pct": 8, "iv": 14, "delta": 0.52},
            {"strike": 25100, "option_type": "CE", "ltp": 120, "bid": 119.3, "ask": 120.7,
             "open_interest": 8000, "oi_change_pct": 25, "iv": 14, "delta": 0.45},
            {"strike": 24900, "option_type": "CE", "ltp": 240, "bid": 238.7, "ask": 241.3,
             "open_interest": 4000, "oi_change_pct": 5, "iv": 14, "delta": 0.60},
            {"strike": 24800, "option_type": "CE", "ltp": 310, "bid": 308, "ask": 312,
             "open_interest": 2000, "oi_change_pct": 3, "iv": 14, "delta": 0.70},
            # PE contracts
            {"strike": 25000, "option_type": "PE", "ltp": 175, "bid": 174, "ask": 176,
             "open_interest": 5200, "oi_change_pct": 6, "iv": 14, "delta": -0.50},
            {"strike": 24900, "option_type": "PE", "ltp": 230, "bid": 228.7, "ask": 231.3,
             "open_interest": 6000, "oi_change_pct": 18, "iv": 14, "delta": -0.58},
        ],
    }

    print("=== BUY direction (CE) ===")
    r = select_option(snapshot, "BUY")
    print(f"selected: {r['selected']}, strike: {r['contract'] and r['contract']['strike']}")
    print(f"oi note: {r['oi_velocity_note']}")

    print("\n=== SELL direction (PE) ===")
    r2 = select_option(snapshot, "SELL")
    print(f"selected: {r2['selected']}, strike: {r2['contract'] and r2['contract']['strike']}")
    print(f"oi note: {r2['oi_velocity_note']}")
