"""
Tiger Brain V6.1 — BRAIN 4 (part 2): Dynamic Position Sizer
============================================================
Broker se LIVE available capital laakar trade size dynamically scale
karta hai. Kabhi bhi hardcoded/static lot size nahi.

Rules:
  - Capital source: Angel One (getRMS available cash). Broker fail ho to
    fallback config value (default None = trade block — fail-safe).
  - Per trade: maximum BRAIN4['MAX_CAPITAL_PER_TRADE_PCT'] (10%) of
    available capital.
  - Total exposure: MAX_TOTAL_EXPOSURE_PCT se zyada nahi.
  - Quantity = floor(allocatable capital / (premium * lot_size)) * lot_size.
    Lot size 0/None ho to 1 maan lo (contract-level trading).
"""

from __future__ import annotations

import logging

try:
    from config.thresholds import BRAIN4
except ImportError:
    raise ImportError("Repo ROOT se chalao, 'broker/' ke andar se nahi.")

logger = logging.getLogger("tiger_brain.position_sizer")


def get_available_capital(broker) -> dict:
    """
    Angel One broker se LIVE available cash laata hai.

    Args:
        broker: AngelBroker instance (broker/angel_connect.py) — logged-in
                hona chahiye. None ho to fallback apply hoga.

    Returns:
        dict:
            'available_capital': float | None
            'source': 'broker' | 'fallback' | 'none'
            'note': str | None
    """
    if broker is None:
        fallback = BRAIN4["FALLBACK_CAPITAL_ON_BROKER_FAIL"]
        if fallback is None:
            return {"available_capital": None, "source": "none",
                    "note": "broker diya hi nahi gaya aur fallback None — trade block"}
        return {"available_capital": float(fallback), "source": "fallback",
                "note": f"broker None — fallback capital {fallback} use hua"}

    try:
        # Angel One SmartAPI: RMS = Risk Management System — available cash
        rms = broker.smart_api.getRMS()
        if not isinstance(rms, dict) or not rms.get("data"):
            raise ValueError(f"getRMS ne unexpected response diya: {rms}")

        available = float(rms["data"]["availablecash"])
        return {"available_capital": available, "source": "broker", "note": None}

    except Exception as exc:
        logger.warning(f"Broker se capital fetch fail: {exc}")
        fallback = BRAIN4["FALLBACK_CAPITAL_ON_BROKER_FAIL"]
        if fallback is None:
            return {"available_capital": None, "source": "none",
                    "note": f"broker capital fetch fail ({exc}) aur fallback None — trade block"}
        return {"available_capital": float(fallback), "source": "fallback",
                "note": f"broker fail ({exc}) — fallback capital {fallback} use hua"}


def size_position(
    available_capital: float,
    premium: float,
    lot_size: int | None = None,
    current_exposure: float = 0.0,
) -> dict:
    """
    Available capital + premium se position size nikalta hai.

    Args:
        available_capital: live available cash (get_available_capital se)
        premium: option ka per-unit price (LTP)
        lot_size: contract ka lot size (None/0 = 1)
        current_exposure: pehle se lagii hui total capital (open positions)

    Returns:
        dict:
            'quantity': int — total units (lots * lot_size)
            'lots': int
            'allocated_capital': float
            'allocation_pct': float — available capital ka %
            'notes': list
    """
    notes = []
    lot = int(lot_size) if lot_size and int(lot_size) > 0 else 1

    if available_capital is None or available_capital <= 0:
        return {"quantity": 0, "lots": 0, "allocated_capital": 0.0,
                "allocation_pct": 0.0,
                "notes": ["available capital nahi hai (None/<=0) — trade nahi ho sakta"]}
    if premium is None or premium <= 0:
        return {"quantity": 0, "lots": 0, "allocated_capital": 0.0,
                "allocation_pct": 0.0,
                "notes": [f"premium invalid ({premium}) — trade nahi ho sakta"]}

    # --- Per-trade cap: 10% of available capital ---
    per_trade_cap = available_capital * (BRAIN4["MAX_CAPITAL_PER_TRADE_PCT"] / 100)

    # --- Total exposure cap ---
    max_total = available_capital * (BRAIN4["MAX_TOTAL_EXPOSURE_PCT"] / 100)
    headroom = max_total - current_exposure
    if headroom <= 0:
        return {"quantity": 0, "lots": 0, "allocated_capital": 0.0,
                "allocation_pct": 0.0,
                "notes": [
                    f"total exposure cap hit: current {current_exposure} + cap "
                    f"{max_total} — naya trade nahi"
                ]}
    allocatable = min(per_trade_cap, headroom)

    # --- Quantity from premium & lot ---
    cost_per_lot = premium * lot
    if cost_per_lot > allocatable:
        notes.append(
            f"ek lot ka cost ({cost_per_lot:.2f}) allocatable "
            f"({allocatable:.2f}) se zyada — trade nahi ho sakta"
        )
        return {"quantity": 0, "lots": 0, "allocated_capital": 0.0,
                "allocation_pct": 0.0, "notes": notes}

    lots = int(allocatable // cost_per_lot)
    allocated = lots * cost_per_lot

    notes.append(
        f"dynamic sizing: available {available_capital:.2f}, per-trade cap "
        f"{BRAIN4['MAX_CAPITAL_PER_TRADE_PCT']}% = {per_trade_cap:.2f}, "
        f"premium {premium} x lot {lot} -> {lots} lots"
    )

    return {
        "quantity": lots * lot,
        "lots": lots,
        "allocated_capital": round(allocated, 2),
        "allocation_pct": round(allocated / available_capital * 100, 2),
        "notes": notes,
    }


# ============================================================
# QUICK MANUAL TEST — repo ROOT se: python3 -m broker.position_sizer
# ============================================================
if __name__ == "__main__":
    print("=== Test 1: 100k capital, 200 premium, lot 75 ===")
    print(size_position(100000, 200, lot_size=75))

    print("\n=== Test 2: small capital, expensive option ===")
    print(size_position(5000, 200, lot_size=75))

    print("\n=== Test 3: exposure cap ===")
    print(size_position(100000, 200, lot_size=75, current_exposure=48000))

    print("\n=== Test 4: no broker, no fallback ===")
    print(get_available_capital(None))
