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


def _send_to_broker(symbol: str, direction: str, quantity: int, order_type: str,
                    broker=None, contract: dict = None) -> dict:
    """Angel One SmartApi se real order place karta hai.

    Args:
        symbol: trading symbol (NIFTY, RELIANCE, etc.)
        direction: 'BUY' ya 'SELL'
        quantity: total quantity (lot_size * lot_count)
        order_type: 'MARKET' ya 'LIMIT'
        broker: AngelBroker instance (real order ke liye chahiye).
                None = DRY_RUN simulation (fallback).
        contract: {'tradingsymbol': str, 'symboltoken': str, 'exchange': str}
                  Real option contract details. None = simulate.

    Returns:
        dict: {'success': bool, 'order_id': str|None, 'fill_price': float|None,
               'error': str|None}
    """
    if broker is None or contract is None:
        logger.warning(
            f"[SIMULATE] broker/contract missing — {direction} {quantity} {symbol} "
            f"order simulate kar rahe hain (real order nahi)."
        )
        return {"success": True, "order_id": "SIMULATED",
                "fill_price": None, "error": None}

    result = broker.place_option_order(
        tradingsymbol=contract["tradingsymbol"],
        symboltoken=contract["symboltoken"],
        exchange=contract["exchange"],
        transaction_type=direction,
        quantity=quantity,
        order_type=order_type,
        price=contract.get("limit_price", 0.0),
    )
    fill_price = None
    if result["success"] and order_type == "MARKET":
        status = broker.get_order_status(result["order_id"])
        fill_price = status.get("avg_price") or None
    return {
        "success": result["success"],
        "order_id": result.get("order_id"),
        "fill_price": fill_price,
        "error": result.get("error"),
    }


def place_order(trade_instruction: dict, max_slippage_pct: float = 0.5,
                broker=None) -> OrderResult:
    """
    Stage 4 ke "Trade Instruction Packet" ko leke order place karta hai.

    Args:
        trade_instruction: pipeline.stage4_decision_lock.lock_decision_from_chain()
                            ka output (symbol, direction, position_size_pct, etc.)
        max_slippage_pct: kitna slippage acceptable hai (Section 9:
                          "0.5%+ to order cancel/retry")
        broker: AngelBroker instance — real order ke liye. None = simulate.

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

    # --- Live mode: real Angel One SmartApi order ---
    order_type = "MARKET"
    contract = trade_instruction.get("contract")
    broker_response = _send_to_broker(
        symbol, direction, quantity, order_type,
        broker=broker, contract=contract,
    )

    if not broker_response.get("success"):
        return OrderResult(
            status="FAILED", symbol=symbol, direction=direction, quantity=quantity,
            notes=[f"Broker se order fail hua: {broker_response.get('error', '?')}"],
        )

    fill_price = broker_response.get("fill_price")
    order_id = broker_response.get("order_id")

    if fill_price is not None:
        # Slippage check (Section 9) — expected price se compare
        expected = trade_instruction.get("expected_premium")
        if expected and expected > 0:
            slip = abs(fill_price - expected) / expected * 100
            if slip > max_slippage_pct:
                logger.warning(
                    f"⚠️ Slippage {slip:.2f}% > {max_slippage_pct}% for {symbol} "
                    f"(fill={fill_price}, expected={expected})"
                )

    return OrderResult(
        status="CONFIRMED", symbol=symbol, direction=direction,
        quantity=quantity, fill_price=fill_price,
        notes=[f"Order placed: id={order_id}"] if order_id else [],
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
    
