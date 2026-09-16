"""Tiger WebSocket V2 — zero-rate-limit real-time tick stream.

Replaces REST polling (ltpData / quote calls) with a persistent
SmartWebSocketV2 connection. Thousands of ticks/sec flow in without
ever hitting Angel One's HTTP rate limits (429 Too Many Requests).

Architecture:
  - Background thread runs SmartWebSocketV2.connect() (blocking run_forever)
  - on_data callback receives parsed binary tick dicts
  - Ticks stored in thread-safe LTP cache + 1m candle builder
  - Main scan thread reads from cache (no REST calls needed)

Data flow:
  Angel One WS → on_data() → _tick_store (thread-safe)
                              → LTP cache {token: price}
                              → 1m candle builder {token: [OHLCV bars]}

Thread safety:
  - _tick_lock protects all shared data structures
  - Candle builder aggregates ticks into 1-min OHLCV bars
  - get_ltp() / get_1m_candles() are read-safe from any thread

Usage:
  ws = TigerWebSocket(broker)
  ws.start()          # background thread
  ws.subscribe_tokens(token_list)
  ltp = ws.get_ltp(token)
  df = ws.get_1m_candles(token)
  ws.stop()
"""

import logging
import threading
import time
from collections import defaultdict
from datetime import datetime
from typing import Optional

import pandas as pd

logger = logging.getLogger("tiger_brain.tiger_websocket")


# ============================================================
# EXCHANGE TYPE MAPPING — Angel One WS protocol
# ============================================================
# SmartWebSocketV2 expects numeric exchange type codes:
#   1 = NSE_CM  (equity cash, e.g. NIFTY spot, RELIANCE-EQ)
#   2 = NSE_FO  (NSE futures & options, e.g. NIFTY options)
#   5 = MCX_FO  (MCX futures & options, e.g. GOLDM futures)
#   3 = BSE_CM  (BSE cash, e.g. SENSEX spot)

EXCHANGE_TYPE_MAP = {
    "NSE": 1,   # NSE Cash Market (index spots + equity spots)
    "NFO": 2,   # NSE Futures & Options (index/stock options)
    "MCX": 5,   # MCX Futures & Options (commodity futures/options)
    "BSE": 3,   # BSE Cash Market (SENSEX spot)
    "BFO": 4,   # BSE Futures & Options
}

# Reverse map: numeric exchange type → our exchange string
EXCHANGE_TYPE_REVERSE = {1: "NSE", 2: "NFO", 3: "BSE", 4: "BFO", 5: "MCX"}


class TickCandleBuilder:
    """Aggregates real-time ticks into 1-minute OHLCV candles.

    Each token gets its own list of candles. When a tick arrives:
      - If current minute candle exists → update H/L/C + add volume
      - If new minute → close previous candle, create new one

    Volume handling:
      Angel One WS sends CUMULATIVE day volume (total since market open).
      REST historical candles have PER-BAR volume (that 1-minute's volume).
      To keep WS candles compatible with REST data, we compute per-bar
      volume by diffing consecutive cumulative volumes:
        bar_volume += max(current_cum - prev_cum, 0)
      First tick of a bar: delta is 0 (no previous reference), so we
      carry forward from the last tick of the previous bar.

    This gives Tiger real 1m candles from LIVE ticks — no REST
    historical fetch needed for scanning during the session.
    """

    def __init__(self):
        self._candles: dict[str, list[dict]] = defaultdict(list)
        self._current_bar: dict[str, dict] = {}
        self._prev_cum_vol: dict[str, int] = {}

    def add_tick(self, token: str, ltp: float, volume: int,
                 timestamp: datetime):
        """Process one tick into the candle builder."""
        if ltp <= 0:
            return

        # Determine the minute bucket (floor to minute)
        if timestamp is None:
            timestamp = datetime.now()
        bar_key = timestamp.replace(second=0, microsecond=0)

        # Compute per-bar volume delta from cumulative WS volume
        prev_cum = self._prev_cum_vol.get(token, volume)
        delta_vol = max(volume - prev_cum, 0)
        self._prev_cum_vol[token] = volume

        cur = self._current_bar.get(token)
        if cur is None or cur["ts"] != bar_key:
            # New minute bar — previous bar (if any) is already in list.
            new_bar = {
                "ts": bar_key,
                "open": ltp,
                "high": ltp,
                "low": ltp,
                "close": ltp,
                "volume": delta_vol,
            }
            self._current_bar[token] = new_bar
            self._candles[token].append(new_bar)
            if len(self._candles[token]) > 500:
                self._candles[token] = self._candles[token][-500:]
        else:
            # Same minute — update OHLC + add per-tick volume delta
            cur["high"] = max(cur["high"], ltp)
            cur["low"] = min(cur["low"], ltp)
            cur["close"] = ltp
            cur["volume"] += delta_vol

    def get_1m_dataframe(self, token: str, min_bars: int = 5) -> Optional[pd.DataFrame]:
        """Return 1m candles as a DataFrame for a token.

        Args:
            token: Angel One numeric token string
            min_bars: minimum bars needed (return None if fewer)

        Returns:
            DataFrame with columns: open, high, low, close, volume
            indexed by timestamp, or None if insufficient data.
        """
        bars = self._candles.get(token, [])
        if len(bars) < min_bars:
            return None
        df = pd.DataFrame(bars[-min(len(bars), 500):])
        df = df.set_index("ts")
        df.index.name = "datetime"
        return df[["open", "high", "low", "close", "volume"]]

    def get_bar_count(self, token: str) -> int:
        """How many 1m candles we have for a token."""
        return len(self._candles.get(token, []))


class TigerWebSocket:
    """Persistent SmartWebSocketV2 stream — zero rate limits.

    Runs SmartWebSocketV2 in a background thread. Receives real-time
    tick data for all subscribed tokens. Main thread reads from
    thread-safe caches.

    Replaces:
      - broker.get_ltp() REST calls → ws.get_ltp() cache read
      - Historical 1m candle REST fetch → ws.get_1m_candles() (live)

    Still uses REST for:
      - Historical 15m candles (initial 30-day fetch at startup)
      - Order placement (buy/sell)
      - Balance / positions (RMS API)
    """

    def __init__(self, broker, mode: int = 3):
        """Initialize TigerWebSocket.

        Args:
            broker: AngelBroker instance (must be logged in)
            mode: subscription mode
                  1 = LTP (minimal — just price)
                  2 = Quote (price + OHLC day + volume + OI)
                  3 = Snap Quote (full — quote + best 5 bids/asks + OI)
        """
        self.broker = broker
        self.mode = mode

        # Extract auth tokens from broker session
        self.auth_token = None
        self.feed_token = None
        self.api_key = broker.api_key
        self.client_code = broker.client_id

        # SmartWebSocketV2 instance
        self._sws = None

        # Thread management
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._connected = threading.Event()
        self._lock = threading.Lock()

        # Data caches (protected by _lock)
        self._ltp_cache: dict[str, float] = {}
        self._tick_count = 0
        self._last_tick_time: Optional[datetime] = None
        self._subscribed_tokens: set[str] = set()

        # Candle builder for 1m aggregation
        self.candles = TickCandleBuilder()

        # Stats
        self.connect_attempts = 0
        self.last_error: Optional[str] = None

    def _extract_tokens(self) -> bool:
        """Extract JWT auth token + feed token from broker session."""
        if self.broker.smart_api is None:
            logger.error("❌ WS: Broker not logged in — no smart_api")
            return False
        try:
            # JWT token: stored as access_token after generateSession
            # SmartWebSocketV2 needs "Bearer <jwt>" format
            jwt = self.broker.smart_api.access_token
            if jwt and not jwt.startswith("Bearer "):
                jwt = f"Bearer {jwt}"
            self.auth_token = jwt

            # Feed token: separate token for data feed
            self.feed_token = self.broker.smart_api.getfeedToken()

            if not self.auth_token or not self.feed_token:
                logger.error("❌ WS: Missing auth/feed token from session")
                return False

            logger.info("✅ WS tokens extracted — auth + feed ready")
            return True
        except Exception as exc:
            logger.error(f"❌ WS token extraction fail: {exc}")
            return False

    # ============================================================
    # SmartWebSocketV2 CALLBACKS
    # ============================================================
    def _on_open(self, wsapp):
        """WebSocket connected — subscribe to tokens."""
        self._connected.set()
        logger.info("🔥 TIGER WEBSOCKET CONNECTED — zero rate limits active!")
        if self._subscribed_tokens:
            self._do_subscribe(list(self._subscribed_tokens))

    def _on_data(self, wsapp, data):
        """Tick data received — parse + store."""
        try:
            self._process_tick(data)
        except Exception as exc:
            # Never let an exception kill the WS thread
            logger.debug(f"WS tick parse error: {exc}")

    def _on_message(self, wsapp, message):
        """Text message (usually heartbeat/pong)."""
        pass  # heartbeat — ignore

    def _on_error(self, wsapp, error):
        """WebSocket error."""
        self.last_error = str(error)
        self._connected.clear()
        logger.error(f"❌ WS error: {error}")

    def _on_close(self, wsapp):
        """WebSocket closed."""
        self._connected.clear()
        logger.warning("⚠️ WS connection closed — will attempt reconnect.")

    # ============================================================
    # TICK PROCESSING
    # ============================================================
    def _process_tick(self, data: dict):
        """Parse a tick dict from SmartWebSocketV2 + store in caches.

        Data format (from _parse_binary_data):
          subscription_mode: 1/2/3
          exchange_type: 1/2/5 (NSE_CM/NSE_FO/MCX_FO)
          token: string (numeric)
          last_traded_price: int (in paise — divide by 100)
          volume_trade_for_the_day: int (cumulative, for quote/snap modes)
          open_price_of_the_day, high, low, closed_price: int (paise)
          open_interest: int (snap quote only)
          best_5_buy_data, best_5_sell_data: list (snap quote only)
        """
        token = data.get("token")
        if not token:
            return

        # Price is in paise (Angel One WS protocol) → convert to rupees
        ltp_raw = data.get("last_traded_price", 0)
        if ltp_raw is None or ltp_raw <= 0:
            return
        ltp = ltp_raw / 100.0  # paise → rupees

        # Volume (cumulative day volume for quote/snap modes)
        volume = int(data.get("volume_trade_for_the_day", 0) or 0)

        # Timestamp
        ts_raw = data.get("exchange_timestamp", 0)
        if ts_raw and ts_raw > 0:
            try:
                ts = datetime.fromtimestamp(ts_raw / 1000.0)
            except (ValueError, OSError):
                ts = datetime.now()
        else:
            ts = datetime.now()

        # Store in caches (thread-safe)
        with self._lock:
            self._ltp_cache[token] = ltp
            self._tick_count += 1
            self._last_tick_time = datetime.now()

        # Feed candle builder
        self.candles.add_tick(token, ltp, volume, ts)

    # ============================================================
    # PUBLIC API — main thread reads from here
    # ============================================================
    def get_ltp(self, token: str) -> float:
        """Get latest LTP for a token from the live cache.

        No REST call — reads from in-memory tick cache.
        Returns 0.0 if no tick received yet for this token.
        """
        with self._lock:
            return self._ltp_cache.get(str(token), 0.0)

    def get_ltp_by_symbol(self, symbol: str) -> float:
        """Get LTP by symbol name (resolves token internally)."""
        token = self._resolve_symbol_token(symbol)
        if token is None:
            return 0.0
        return self.get_ltp(token)

    def get_1m_candles(self, token: str, min_bars: int = 5) -> Optional[pd.DataFrame]:
        """Get live 1m candles built from real-time ticks.

        Returns DataFrame or None if insufficient tick data.
        """
        return self.candles.get_1m_dataframe(str(token), min_bars)

    def is_connected(self) -> bool:
        """Is the WebSocket currently connected and receiving ticks?"""
        return self._connected.is_set()

    def tick_count(self) -> int:
        """Total ticks received since start."""
        with self._lock:
            return self._tick_count

    def last_tick_age_seconds(self) -> float:
        """Seconds since last tick received (0 if never)."""
        with self._lock:
            if self._last_tick_time is None:
                return 9999.0
            return (datetime.now() - self._last_tick_time).total_seconds()

    def is_healthy(self) -> bool:
        """WebSocket healthy = connected + recent ticks (<60s ago)."""
        return self.is_connected() and self.last_tick_age_seconds() < 60.0

    # ============================================================
    # SYMBOL → TOKEN RESOLUTION
    # ============================================================
    def _resolve_symbol_token(self, symbol: str) -> Optional[str]:
        """Resolve a scan symbol to its Angel One numeric token.

        Uses the same logic as data/loader.py resolve_underlying_token
        but returns just the token string (for WS subscription).
        """
        from data.loader import resolve_underlying_token
        result = resolve_underlying_token(symbol)
        if result is None:
            return None
        _, token = result
        return str(token)

    def _resolve_exchange_type(self, symbol: str) -> int:
        """Get numeric exchange type for WS subscription.

        Index/Stock spot → NSE_CM (1)
        Index/Stock options → NSE_FO (2)
        MCX futures → MCX_FO (5)
        """
        from data.loader import OPTION_INSTRUMENT_TYPE, INDEX_UNDERLYING_TOKENS
        upper = symbol.upper()

        # Index spot (NIFTY/BANKNIFTY/etc) → NSE_CM or BSE_CM
        if upper in INDEX_UNDERLYING_TOKENS:
            exch = INDEX_UNDERLYING_TOKENS[upper][0]
            return EXCHANGE_TYPE_MAP.get(exch, 1)

        # MCX commodity → MCX_FO
        if upper in OPTION_INSTRUMENT_TYPE:
            exch = OPTION_INSTRUMENT_TYPE[upper][1]
            return EXCHANGE_TYPE_MAP.get(exch, 1)

        # NSE stock → NSE_CM
        return EXCHANGE_TYPE_MAP.get("NSE", 1)

    def build_subscription_list(self, symbols: list[str]) -> list[dict]:
        """Build the token_list for SmartWebSocketV2.subscribe().

        Groups tokens by exchange type:
          [
            {"exchangeType": 1, "tokens": ["99926000", "3045"]},
            {"exchangeType": 5, "tokens": ["234230"]},
          ]

        Args:
            symbols: list of scan symbol names (NIFTY, RELIANCE, CRUDEOIL, etc)

        Returns:
            token_list in the format SmartWebSocketV2.subscribe() expects
        """
        # Group tokens by exchange type
        token_groups: dict[int, list[str]] = defaultdict(list)

        for sym in symbols:
            token = self._resolve_symbol_token(sym)
            if token is None:
                logger.warning(f"WS subscribe: token resolve fail for {sym}")
                continue
            exch_type = self._resolve_exchange_type(sym)
            token_groups[exch_type].append(token)

        # Build the subscription list
        sub_list = []
        for exch_type, tokens in token_groups.items():
            # Dedupe
            unique_tokens = list(dict.fromkeys(tokens))
            sub_list.append({
                "exchangeType": exch_type,
                "tokens": unique_tokens,
            })
            exch_name = EXCHANGE_TYPE_REVERSE.get(exch_type, "?")
            logger.info(f"  📡 WS subscribe: {exch_name} → {len(unique_tokens)} tokens")

        return sub_list

    # ============================================================
    # SUBSCRIBE / UNSUBSCRIBE
    # ============================================================
    def subscribe_tokens(self, tokens: list[str], exchange_type: int = 1):
        """Subscribe to raw token IDs (already resolved).

        Args:
            tokens: list of numeric token strings
            exchange_type: 1=NSE_CM, 2=NSE_FO, 5=MCX_FO
        """
        with self._lock:
            self._subscribed_tokens.update(tokens)

        if self.is_connected():
            self._do_subscribe(tokens, exchange_type)
        else:
            logger.info(f"📡 WS not yet connected — will subscribe on connect "
                        f"({len(self._subscribed_tokens)} tokens queued)")

    def subscribe_symbols(self, symbols: list[str]):
        """Subscribe to scan symbols by name (auto-resolves tokens).

        Args:
            symbols: list of symbol names (NIFTY, BANKNIFTY, RELIANCE, CRUDEOIL, etc)
        """
        sub_list = self.build_subscription_list(symbols)
        if not sub_list:
            logger.warning("WS subscribe: no tokens resolved from symbols")
            return

        all_tokens = []
        for group in sub_list:
            all_tokens.extend(group["tokens"])
            if self.is_connected():
                self._do_subscribe(group["tokens"], group["exchangeType"])

        with self._lock:
            self._subscribed_tokens.update(all_tokens)

        if not self.is_connected():
            logger.info(f"📡 WS queued {len(all_tokens)} tokens — "
                        "will subscribe on connect")

    def _do_subscribe(self, tokens: list[str], exchange_type: int = 1):
        """Actually send subscribe request to SmartWebSocketV2."""
        if self._sws is None or not self.is_connected():
            return
        try:
            token_list = [{"exchangeType": exchange_type, "tokens": tokens}]
            corr_id = f"tiger_{int(time.time())}"[:10]
            self._sws.subscribe(corr_id, self.mode, token_list)
            logger.info(f"📡 WS subscribed: {len(tokens)} tokens "
                        f"(exchange={EXCHANGE_TYPE_REVERSE.get(exchange_type, '?')}, "
                        f"mode={self.mode})")
        except Exception as exc:
            logger.error(f"❌ WS subscribe fail: {exc}")

    # ============================================================
    # CONNECTION MANAGEMENT
    # ============================================================
    def start(self):
        """Start the WebSocket in a background thread."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("WS already running")
            return

        if not self._extract_tokens():
            logger.error("❌ WS start fail — no auth tokens")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_forever, name="TigerWebSocket", daemon=True)
        self._thread.start()
        logger.info("🐅 TigerWebSocket thread started (background daemon)")

    def _run_forever(self):
        """Run SmartWebSocketV2.connect() with auto-reconnect.

        SmartWebSocketV2 has built-in retry (max_retry_attempt), but
        we add an outer loop for indefinite reconnection — Tiger should
        never die because of a temporary WS disconnect.
        """
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2

        while not self._stop_event.is_set():
            self.connect_attempts += 1
            try:
                self._sws = SmartWebSocketV2(
                    auth_token=self.auth_token,
                    api_key=self.api_key,
                    client_code=self.client_code,
                    feed_token=self.feed_token,
                    max_retry_attempt=3,
                    retry_strategy=1,
                    retry_delay=5,
                    retry_multiplier=2,
                    retry_duration=30,
                )

                # Set callbacks
                self._sws.on_open = self._on_open
                self._sws.on_data = self._on_data
                self._sws.on_message = self._on_message
                self._sws.on_error = self._on_error
                self._sws.on_close = self._on_close

                logger.info(f"🔄 WS connecting (attempt {self.connect_attempts})...")
                self._sws.connect()

            except Exception as exc:
                logger.error(f"❌ WS connect exception: {exc}")
                self.last_error = str(exc)

            if self._stop_event.is_set():
                break

            # Reconnect delay with exponential backoff
            delay = min(5 * (2 ** min(self.connect_attempts - 1, 5)), 60)
            logger.info(f"🔄 WS reconnecting in {delay}s...")
            self._stop_event.wait(delay)

    def stop(self):
        """Stop the WebSocket and clean up."""
        logger.info("🛑 Stopping TigerWebSocket...")
        self._stop_event.set()
        if self._sws is not None:
            try:
                self._sws.close_connection()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._connected.clear()
        logger.info("🛑 TigerWebSocket stopped.")

    def reconnect(self):
        """Force a reconnect (e.g. after session token refresh)."""
        logger.info("🔄 WS forced reconnect — refreshing tokens...")
        if self._sws is not None:
            try:
                self._sws.close_connection()
            except Exception:
                pass
        self._connected.clear()
        # Re-extract tokens (in case session was refreshed)
        self._extract_tokens()
        # The _run_forever loop will reconnect automatically

    # ============================================================
    # STATUS REPORT
    # ============================================================
    def status(self) -> dict:
        """Return a status dict for logging/monitoring."""
        # Read primitive values under lock, compute derived values outside.
        # CRITICAL: must NOT call last_tick_age_seconds() inside the lock —
        # that method also acquires self._lock → deadlock with threading.Lock.
        with self._lock:
            tick_count = self._tick_count
            sub_count = len(self._subscribed_tokens)
            last_tick_time = self._last_tick_time
            sample_tokens = list(self._subscribed_tokens)[:5]

        # Compute tick age outside the lock
        if last_tick_time is None:
            last_age = 9999.0
        else:
            last_age = (datetime.now() - last_tick_time).total_seconds()

        return {
            "connected": self.is_connected(),
            "healthy": self.is_connected() and last_age < 60.0,
            "tick_count": tick_count,
            "subscribed_tokens": sub_count,
            "last_tick_age_s": round(last_age, 1),
            "connect_attempts": self.connect_attempts,
            "last_error": self.last_error,
            "candles_built": {
                token: self.candles.get_bar_count(token)
                for token in sample_tokens
            },
        }
