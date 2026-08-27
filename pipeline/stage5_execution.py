"""
Tiger Brain V6+V7 — STAGE 5: Pure Execution Brain (Section 9)
================================================================
Ye "sochta" nahi — sirf Stage 4 ka Instruction Packet leke asli order
bhejta hai. Order type chunna (limit/market), slippage monitor karna,
partial fill handle karna, fill confirm hote hi SL/target auto-place
karna.

⚠️⚠️ SABSE ZAROORI HONEST NOTE ⚠️⚠️
Abhi tak humne KISI BHI BROKER (Angel One SmartAPI ya kisi aur) ka actual
API connect NAHI kiya hai. Isliye:
  - `place_order()` function abhi ek STUB hai — ye real broker ko order
    NAHI bhejta, sirf simulate karta hai (chahe DRY_RUN=True ho ya False)
  - Jab tak `_send_to_broker()` mein real SmartAPI integration nahi
    likha jaata, is poore Stage 5 ka "live mode" bhi effectively
    "simulate mode" jaisa hai — bas isliye ki NAHI PATA broker se
    connect kaise karna hai (credentials, session management, order
    API endpoints) jab tak wo decide/setup nahi hota.
  - Jaisa humne discuss kiya tha — DRY_RUN flag (config/thresholds.py
    mein) ka switch yahan bhi respect hota hai, par abhi dono states
    mein koi real paisa nahi lagta kyunki broker hi connect nahi hai.

Jab Angel One (ya jo bhi broker) ka SmartAPI setup ho jaye, `_send_to_broker()`
function ke andar real API call daalni hogi — us waqt DRY_RUN ka fasla
genuinely real ban jayega.
"""

import logging

try:
    from config.thresholds import DRY_RUN
except ImportError:
    raise ImportError("Repo ROOT se chalao, 'pipeline/' ke andar se nahi.")

logger = logging.getLogger("tiger_brain.stage5_execution")
logging.basicConfig(level=logging.INFO)


class OrderResult:
    """Ek order ke result ko represent karta hai."""

    def __init__(self, status, symbol, direction, quantity, fill_price=None, notes=None):
        self.status = status  # 'CONFIRMED' | 'FAILED' | 'SIMULATED'
        self.symbol = symbol
        self.direction = direction
        self.quantity = quantity
        self.fill_price = fill_price
        self.notes = notes or []

    def __repr__(self):
        return (
            f"OrderResult(status={self.status}, symbol={self.symbol}, "
            f"direction={self.direction}, quantity={self.quantity}, "
            f"fill_price={self.fill_price})"
        )

    def to_dict(self):
        return {
            "status": self.status, "symbol": self.symbol,
            "direction": self.direction, "quantity": self.quantity,
            "fill_price": self.fill_price, "notes": self.notes,
        }


def _send_to_broker(symbol: str, direction: str, quantity: int, order_type: str) -> dict:
    """
    ⚠️ STUB FUNCTION — ye abhi REAL broker ko kuch nahi bhejta.

    Jab Angel One SmartAPI (ya jo bhi broker) connect ho, ISI FUNCTION ke
    andar real API call likhni hai. Abhi ye sirf ek fake "success"
    response return karta hai taaki upar ka logic (slippage check, retry,
    SL/target placement) test ho sake.

    TODO (Phase 2/3 mein): SmartAPI se login, session token, actual order
    placement API call yahan aayegi.
    """
    logger.warning(
        "🚧 _send_to_broker() abhi STUB hai — koi real broker call nahi ho "
        "rahi. Jab tak Angel One SmartAPI integrate nahi hoti, ye sirf "
        "simulate kar raha hai."
    )
    return {
        "success": True,
        "fill_price": None,  # real integration mein broker se aayega
        "order_id": "STUB_ORDER_ID",
    }


def place_order(trade_instruction: dict, max_slippage_pct: float = 0.5) -> OrderResult:
    """
    Stage 4 ke "Trade Instruction Packet" ko leke order place karta hai.

    Args:
        trade_instruction: pipeline.stage4_decision_lock.lock_decision_from_chain()
                            ka output (symbol, direction, position_size_pct, etc.)
        max_slippage_pct: kitna slippage acceptable hai (Section 9:
                          "0.5%+ to order cancel/retry")

    Returns:
        OrderResult object
    """
    if not trade_instruction.get("locked", False):
        return OrderResult(
            status="FAILED",
            symbol=None, direction=None, quantity=0,
            notes=["Trade instruction 'locked=False' hai — koi order banta hi nahi"],
        )

    symbol = trade_instruction["symbol"]
    direction = trade_instruction["direction"]

    if direction is None:
        return OrderResult(
            status="FAILED", symbol=symbol, direction=None, quantity=0,
            notes=["Direction missing hai trade_instruction mein — order nahi bhej sakte"],
        )

    # Quantity calculation abhi placeholder hai — real lot-size, premium
    # price, aur deployable_capital_used se calculate hoga jab options-chain
    # data connect hoga
    quantity = 1  # TODO: real lot-size calculation Phase 2/3 mein

    if DRY_RUN:
        logger.info(f"[DRY_RUN] Simulating order: {direction} {symbol} qty={quantity}")
        return OrderResult(
            status="SIMULATED", symbol=symbol, direction=direction, quantity=quantity,
            notes=["DRY_RUN mode — koi real order nahi bheja gaya (jaisa expect kiya)"],
        )

    # --- Live mode (⚠️ abhi bhi effectively simulate hai, upar dekho) ---
    order_type = "LIMIT"  # spread ke hisaab se better fill ke liye, hardcoded abhi
    broker_response = _send_to_broker(symbol, direction, quantity, order_type)

    if not broker_response.get("success"):
        return OrderResult(
            status="FAILED", symbol=symbol, direction=direction, quantity=quantity,
            notes=["Broker se order fail hua"],
        )

    fill_price = broker_response.get("fill_price")

    # Slippage check (Section 9 — abhi fill_price None hai kyunki stub hai,
    # isliye ye check abhi effectively skip hoga jab tak real broker data na aaye)
    if fill_price is not None:
        # TODO: expected price se compare karke slippage % nikalna, agar
        # max_slippage_pct se zyada hai to cancel/retry logic yahan aayega
        pass

    return OrderResult(
        status="CONFIRMED", symbol=symbol, direction=direction,
        quantity=quantity, fill_price=fill_price,
        notes=[
            "⚠️ Ye 'CONFIRMED' status stub broker response se aaya hai — "
            "real broker integration hone tak ye asli trade NAHI hai."
        ],
    )


# ============================================================
# QUICK MANUAL TEST
# Chalane ka tarika: repo ROOT se → python3 -m pipeline.stage5_execution
# ============================================================
if __name__ == "__main__":
    print("=== Stage 5 Execution Test — DRY_RUN mode ===")
    fake_instruction = {
        "locked": True, "symbol": "NIFTY_TEST", "direction": "BUY",
        "position_size_pct": 2.5, "deployable_capital_used": 1000,
    }
    result = place_order(fake_instruction)
    print(result)
    print(result.to_dict())

    print("\n=== Stage 5 Execution Test — locked=False case ===")
    fake_instruction_2 = {"locked": False}
    result2 = place_order(fake_instruction_2)
    print(result2)

    print("\n✅ Test complete — koi crash nahi hua.")
    print(
        "⚠️ REMINDER: Broker integration abhi stub hai. Jab tak Angel One "
        "(ya koi bhi broker) SmartAPI connect nahi hota, ye poora stage "
        "sirf structure/interface test kar raha hai, asli trades nahi kar raha."
    )
    
