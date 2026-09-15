"""
Tiger Brain V6+V7 — Angel One Broker Connection (Login/Session Management)
=============================================================================
This module performs automatic login against the Angel One SmartAPI, removing
the need for daily manual login. TOTP is generated automatically via pyotp,
so no one has to read a code from an Authenticator app each day.

⚠️ IMPORTANT — This module has NOT yet been tested against the REAL Angel One
API (internet is disabled in the sandbox). The logic was written following
SmartAPI's official documentation and the smartapi-python package usage
patterns, but verify on first run on your server that login succeeds.

⚠️ CREDENTIALS: This module reads credentials only from the .env file —
nothing is hardcoded. The .env file must never be committed to GitHub
(it should already be in .gitignore).
"""

import logging
import os
from datetime import datetime, timedelta

from dotenv import load_dotenv

logger = logging.getLogger("tiger_brain.broker.angel_connect")
logging.basicConfig(level=logging.INFO)

# Load .env file (should be in repo root)
load_dotenv()


class AngelConnectionError(Exception):
    """Raised when login fails or the session is invalid."""
    pass


class AngelBroker:
    """
    Manages a session with the Angel One SmartAPI.

    Usage:
        broker = AngelBroker()
        broker.login()
        # now broker.smart_api can be used for data/orders
        # or broker.get_session_token() to retrieve the token
    """

    def __init__(self):
        self.client_id = os.getenv("ANGEL_CLIENT_ID")
        self.mpin = os.getenv("ANGEL_MPIN")
        self.totp_secret = os.getenv("ANGEL_TOTP_SECRET")
        self.api_key = os.getenv("ANGEL_API_KEY")

        self._validate_credentials()

        self.smart_api = None
        self.session_data = None
        self.login_time = None

        # Tiger WebSocket V2 — real-time tick stream (zero rate limits)
        # Initialized lazily on first use (after login)
        self.websocket = None
        self._ws_enabled = os.getenv("TIGER_WEBSOCKET", "true").lower() == "true"

    def _validate_credentials(self):
        """
        Checks that all required credentials were loaded from .env — if any
        are missing, raises a clear error immediately (no silent failure,
        so debugging is easy).
        """
        missing = []
        if not self.client_id:
            missing.append("ANGEL_CLIENT_ID")
        if not self.mpin:
            missing.append("ANGEL_MPIN")
        if not self.totp_secret:
            missing.append("ANGEL_TOTP_SECRET")
        if not self.api_key:
            missing.append("ANGEL_API_KEY")

        if missing:
            raise AngelConnectionError(
                f"These credentials are missing from the .env file: {', '.join(missing)}. "
                f"A .env file must exist in the repo ROOT folder with all of "
                f"these values set."
            )

    def _generate_totp(self) -> str:
        """
        Generates the current 6-digit TOTP code from the secret — the same
        code that the Google Authenticator app shows every 30 seconds.
        """
        try:
            import pyotp
        except ImportError:
            raise AngelConnectionError(
                "pyotp is not installed. Run: pip3 install pyotp --user"
            )

        totp = pyotp.TOTP(self.totp_secret)
        return totp.now()

    def login(self, max_retries: int = 3) -> bool:
        """
        Sends a login request to the Angel One SmartAPI using Client ID +
        MPIN + a fresh TOTP code.

        Args:
            max_retries: how many times to retry if login fails (e.g. network issue)

        Returns:
            True if login succeeds, otherwise raises AngelConnectionError
            after max retries.
        """
        try:
            from SmartApi import SmartConnect
        except ImportError:
            raise AngelConnectionError(
                "smartapi-python is not installed. Run: "
                "pip3 install smartapi-python pyotp logzero websocket-client "
                "pycryptodome --user"
            )

        last_error = None

        for attempt in range(1, max_retries + 1):
            try:
                totp_code = self._generate_totp()

                self.smart_api = SmartConnect(api_key=self.api_key)
                session = self.smart_api.generateSession(
                    self.client_id, self.mpin, totp_code
                )

                if not session.get("status"):
                    raise AngelConnectionError(
                        f"Login failed: {session.get('message', 'Unknown error')}"
                    )

                self.session_data = session
                self.login_time = datetime.now()

                logger.info(
                    f"✅ Angel One login successful — {self.login_time.strftime('%Y-%m-%d %H:%M:%S')}"
                )

                # Auto-start WebSocket after successful login
                if self._ws_enabled:
                    self.start_websocket()

                return True

            except Exception as exc:
                last_error = exc
                logger.warning(
                    f"Login attempt {attempt}/{max_retries} failed: {exc}"
                )

        raise AngelConnectionError(
            f"Login failed after {max_retries} attempts. "
            f"Last error: {last_error}"
        )

    # ============================================================
    # WEBSOCKET V2 — real-time tick stream (zero rate limits)
    # ============================================================
    def start_websocket(self):
        """Start SmartWebSocketV2 for real-time tick data.

        After login, this creates a persistent WebSocket connection that
        streams live ticks without hitting REST rate limits. The WS runs
        in a background daemon thread.
        """
        if self.websocket is not None and self.websocket.is_connected():
            logger.info("📡 WS already connected — skip start")
            return
        try:
            from broker.tiger_websocket import TigerWebSocket
            self.websocket = TigerWebSocket(self, mode=3)  # SNAP_QUOTE
            self.websocket.start()
            logger.info("🔥 TigerWebSocket V2 started — real-time ticks streaming")
        except Exception as exc:
            logger.warning(f"⚠️ WebSocket start fail (REST fallback active): {exc}")
            self.websocket = None

    def stop_websocket(self):
        """Stop the WebSocket connection."""
        if self.websocket is not None:
            self.websocket.stop()
            self.websocket = None

    def ws_get_ltp(self, tradingsymbol: str, symboltoken: str,
                   exchange: str) -> float:
        """Get LTP from WebSocket cache (zero rate limits, zero REST calls).

        Falls back to REST get_ltp() if:
          - WebSocket not connected
          - No tick received for this token yet
          - WebSocket disabled in config
        """
        if self.websocket is not None and self.websocket.is_healthy():
            ltp = self.websocket.get_ltp(symboltoken)
            if ltp > 0:
                return ltp
            # WS healthy but no tick for this token yet — fall through to REST
        # REST fallback
        return self.get_ltp(tradingsymbol, symboltoken, exchange)

    def is_session_valid(self) -> bool:
        """
        Checks whether the session is still valid. Angel One sessions are
        usually valid for one trading day — if more than 24 hours have
        passed since login_time, the session is treated as expired
        (safe default; exact expiry should be confirmed from Angel One
        documentation).
        """
        if self.session_data is None or self.login_time is None:
            return False

        elapsed = datetime.now() - self.login_time
        return elapsed < timedelta(hours=20)  # conservative — refresh before 24h

    def ensure_logged_in(self):
        """
        Performs a fresh login if the session is not valid. This function
        is called from automation/scheduler.py's pre-market-wakeup job
        each trading day.
        """
        if not self.is_session_valid():
            logger.info("Session invalid/expired — performing fresh login...")
            self.login()
        else:
            logger.info("Session already valid, no fresh login needed.")

    def logout(self):
        """Closes the session — should be called after market close
        (Section 34's "market close" step).
        """
        # Stop WebSocket first
        self.stop_websocket()
        if self.smart_api is not None:
            try:
                self.smart_api.terminateSession(self.client_id)
                logger.info("Session successfully logged out/terminated.")
            except Exception as exc:
                logger.warning(f"Error during logout (can be ignored): {exc}")
            finally:
                self.session_data = None
                self.login_time = None

    # ============================================================
    # LIVE MARKET DATA + ORDER PLACEMENT (SmartApi)
    # ============================================================
    def get_ltp(self, tradingsymbol: str, symboltoken: str,
                exchange: str) -> float:
        """Fetches real market LTP (Last Traded Price).

        Uses the REAL market price rather than a SIMULATED premium for
        Tiger's affordability check.

        Args:
            tradingsymbol: e.g. 'ICICIBANK29SEP261440PE'
            symboltoken: numeric token
            exchange: 'NFO' or 'MCX'

        Returns:
            float: real LTP. 0 if the API fails.
        """
        self.ensure_logged_in()
        try:
            resp = self.smart_api.ltpData(
                exchange, tradingsymbol, str(symboltoken))
            if not resp or not resp.get("data"):
                logger.warning(f"ltpData() fail for {tradingsymbol}")
                return 0.0
            data = resp["data"]
            ltp = float(data.get("ltp", 0) or 0)
            if ltp <= 0:
                ltp = float(data.get("close", 0) or 0)
            return ltp
        except Exception as exc:
            logger.error(f"LTP fetch fail {tradingsymbol}: {exc}")
            return 0.0

    def get_balance(self) -> float:
        """Fetches the real available balance of the Angel One account.

        Available margin is retrieved via SmartApi rmsLimit().
        RESILIENT: on failure, performs a fresh login + retry.
        Tiger uses this for capital-based position sizing.

        Returns:
            float: available cash/margin for trading. 0 if the API fails
                   (even after 2 retries — caller will not place orders at 0).
        """
        for attempt in range(1, 3):  # 2 attempts: direct + after re-login
            self.ensure_logged_in()
            try:
                rms = self.smart_api.rmsLimit()
                if not rms or not rms.get("data"):
                    logger.warning("rmsLimit() returned no data (attempt %d/2).", attempt)
                else:
                    data = rms["data"]
                    avail = float(data.get("availablecash", 0) or 0)
                    logger.info(f"💰 Angel One balance: ₹{avail:,.2f}")
                    return avail
            except Exception as exc:
                logger.error(f"Balance fetch fail (attempt %d/2): %s", attempt, exc)

            # Attempt 1 failed — fresh login then retry
            if attempt < 2:
                logger.warning("Balance fetch fail — fresh login + retry...")
                try:
                    self.login()
                except Exception as exc:
                    logger.error(f"Re-login fail: {exc}")

        logger.error("Balance fetch failed after 2 attempts — returning 0.")
        return 0.0

    def place_option_order(
        self,
        tradingsymbol: str,
        symboltoken: str,
        exchange: str,
        transaction_type: str,
        quantity: int,
        product_type: str = "INTRADAY",
        order_type: str = "MARKET",
        price: float = 0.0,
        is_exit: bool = False,
    ) -> dict:
        """Places a real option order via the Angel One SmartApi.

        Args:
            tradingsymbol: Angel One tradingsymbol (e.g. 'NIFTY24SEP22500CE')
            symboltoken: Angel One symbol token (numeric string)
            exchange: 'NSE' or 'MCX'
            transaction_type: 'BUY' or 'SELL'
            quantity: lot count * lot size
            product_type: 'INTRADAY' (default) or 'CARRYFORWARD'
            order_type: 'MARKET' (default) or 'LIMIT'
            price: limit price for LIMIT orders (0 for MARKET)

        Returns:
            dict: {'success': bool, 'order_id': str, 'error': str|None}
        """
        self.ensure_logged_in()
        if transaction_type not in ("BUY", "SELL"):
            return {"success": False, "order_id": None,
                    "error": f"Invalid transaction_type: {transaction_type}"}

        # === TIGER BUY-ONLY GUARD ===
        # Tiger ONLY buys options (CE/PE). SELL is allowed ONLY for
        # closing an existing bought position (intraday exit / square-off).
        # A naked SELL as fresh entry is permanently BLOCKED.
        if transaction_type == "SELL" and not is_exit:
            logger.error(
                f"🚫 BLOCKED naked SELL entry: {quantity} {tradingsymbol} — "
                f"Tiger only buys options, never sells to open")
            return {"success": False, "order_id": None,
                    "error": "Naked SELL entry blocked — Tiger is buy-only"}

        params = {
            "variety": "NORMAL",
            "tradingsymbol": tradingsymbol,
            "symboltoken": str(symboltoken),
            "transactiontype": transaction_type,
            "exchange": exchange,
            "ordertype": order_type,
            "producttype": product_type,
            "duration": "DAY",
            "price": str(price),
            "quantity": str(quantity),
            "squareoff": "0",
            "stoploss": "0",
        }
        try:
            order_id = self.smart_api.placeOrder(params)
            logger.info(
                f"✅ Order placed: {transaction_type} {quantity} {tradingsymbol} "
                f"@ {order_type} → order_id={order_id}"
            )
            return {"success": True, "order_id": str(order_id), "error": None}
        except Exception as exc:
            logger.error(
                f"❌ Order fail: {transaction_type} {quantity} {tradingsymbol} — {exc}"
            )
            return {"success": False, "order_id": None, "error": str(exc)}

    def get_order_status(self, order_id: str) -> dict:
        """Actual status of a placed order — accepted, rejected, executed?

        placeOrder() returns an order_id but does not guarantee that RMS
        accepted it. reject_reason indicates why it was rejected (e.g.
        'Insufficient Margin').

        Returns:
            dict: {'status': str, 'filled_qty': int, 'avg_price': float,
                   'reject_reason': str|None}
        """
        self.ensure_logged_in()
        try:
            book = self.smart_api.orderBook()
            if not book or not book.get("data"):
                return {"status": "UNKNOWN", "filled_qty": 0,
                        "avg_price": 0.0, "reject_reason": None}
            for o in book["data"]:
                if str(o.get("orderid")) == str(order_id):
                    return {
                        "status": o.get("status", "UNKNOWN"),
                        "filled_qty": int(o.get("filledquantity", 0) or 0),
                        "avg_price": float(o.get("averageprice", 0) or 0),
                        "reject_reason": o.get("text", None) or
                                         o.get("rejectreason", None),
                    }
            return {"status": "UNKNOWN", "filled_qty": 0,
                    "avg_price": 0.0, "reject_reason": None}
        except Exception as exc:
            logger.warning(f"Order status fetch fail: {exc}")
            return {"status": "UNKNOWN", "filled_qty": 0,
                    "avg_price": 0.0, "reject_reason": None}

    def get_positions(self) -> list:
        """Fetches current open positions (for square-off)."""
        self.ensure_logged_in()
        try:
            pos = self.smart_api.position()
            return pos.get("data", []) if pos else []
        except Exception as exc:
            logger.warning(f"Position fetch fail: {exc}")
            return []

    def square_off_all(self, exchange: str = None) -> int:
        """Closes all open positions (intraday + delivery).

        Product type comes from the position data — delivery (CARRYFORWARD)
        positions are closed as CARRYFORWARD, INTRADAY as INTRADAY.
        A wrong product type causes Angel One to reject the order.

        Args:
            exchange: None = all positions. 'MCX' = MCX only.
                      NSE/NFO positions close at 15:15, MCX at 23:15.

        Returns: how many positions were attempted to be closed.
        """
        positions = self.get_positions()
        closed = 0
        for p in positions:
            sym = p.get("tradingsymbol", "")
            token = p.get("symboltoken", "")
            exch = p.get("exchange", "")
            qty = int(p.get("netqty", 0) or 0)
            if qty == 0 or not sym:
                continue
            # Exchange filter — only positions for the specified exchange
            if exchange and exch != exchange:
                continue
            # Product type from position — INTRADAY or CARRYFORWARD
            pos_product = p.get("producttype", "INTRADAY")
            if pos_product not in ("INTRADAY", "CARRYFORWARD"):
                pos_product = "INTRADAY"
            # Net long → SELL to close, net short → BUY to close
            close_side = "SELL" if qty > 0 else "BUY"
            close_qty = abs(qty)
            res = self.place_option_order(
                tradingsymbol=sym, symboltoken=token, exchange=exch,
                transaction_type=close_side, quantity=close_qty,
                product_type=pos_product, order_type="MARKET",
                is_exit=True,
            )
            if res["success"]:
                closed += 1
                logger.info(f"Square-off: {close_side} {close_qty} {sym} [{exch}]")
            else:
                logger.error(f"Square-off FAIL {sym} [{exch}]: {res['error']}")
        logger.info(f"Square-off complete: {closed} positions closed"
                    f"{' (' + exchange + ')' if exchange else ''}")
        return closed


# ============================================================
# QUICK MANUAL TEST — ⚠️ This will attempt to connect to your REAL
# Angel One account. Only run it after .env is correctly set up on
# your server. This test could not be run in the sandbox
# (internet disabled).
# How to run: from repo ROOT → python3 -m broker.angel_connect
# ============================================================
if __name__ == "__main__":
    print("=== Angel One Login Test ===\n")
    print("⚠️ This will connect to your REAL Angel One account.")
    print("If credentials are wrong, an error will appear — this is normal,")
    print("it tells you what to fix in the .env file.\n")

    try:
        broker = AngelBroker()
        broker.login()
        print("\n✅ LOGIN SUCCESSFUL!")
        print(f"Session valid: {broker.is_session_valid()}")
    except AngelConnectionError as exc:
        print(f"\n❌ LOGIN FAILED: {exc}")
        print(
            "\nCheck the .env file: all 4 values (CLIENT_ID, MPIN, "
            "TOTP_SECRET, API_KEY) must be correct, with no extra spaces/quotes."
          )
      
