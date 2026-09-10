"""Tiger V19 — Live Trading Runner (24x7 Automation)

This module runs Tiger in LIVE mode on AWS. Full cycle:
  09:00  Pre-market wake  → broker login + instrument master + data fetch
  09:15  Market open      → intraday scanning start
  20min  Intraday scan    → zone detect → entry signal → real order
  15:15  NSE square-off   → close all NSE positions (smart)
  23:15  MCX square-off   → close all MCX positions (smart)
  00:00  Nightly replay   → audit day + pattern tracking

To run (on AWS):
  python3 -m automation.tiger_live

Or via scheduler:
  python3 -m automation.scheduler
"""
from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import datetime

import pandas as pd

logger = logging.getLogger("tiger_brain.tiger_live")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from broker.angel_connect import AngelBroker, AngelConnectionError
from automation.scheduler import (
    TigerBrainScheduler, get_day_mode, get_active_market, is_market_hours, is_opening_range_period,
    is_mcx_hours,
)
from data.loader import resolve_option_contract, OPTION_INSTRUMENT_TYPE
from config.thresholds import AUTOMATION, MARKET_CATEGORIES
from backtest.tiger_fund_brain import announce_fund_plan, size_trade_with_fund_brain
from risk.capital_manager import CapitalManager
from automation.daily_cleanup import run_daily_cleanup
from pipeline.seven_brains import count_aligned_brains


def resolve_exchange_for_symbol(symbol: str) -> str:
    """Resolve the exchange for a symbol (MCX commodity vs NFO equity/index).

    Authoritative lookup via OPTION_INSTRUMENT_TYPE (data/loader.py) —
    not a hardcoded set, so new MCX commodities (ALUMINIUM, MENTHAOIL,
    etc.) are detected automatically.
    """
    if not symbol:
        return "NFO"
    upper = symbol.upper()
    # Authoritative: OPTION_INSTRUMENT_TYPE has exchange for every symbol
    if upper in OPTION_INSTRUMENT_TYPE:
        return OPTION_INSTRUMENT_TYPE[upper][1]
    # MINI variants: strip suffix and check the parent symbol
    for suffix in ("M", "MINI"):
        if upper.endswith(suffix):
            parent = upper[:-len(suffix)]
            if parent in OPTION_INSTRUMENT_TYPE:
                return OPTION_INSTRUMENT_TYPE[parent][1]
    return "NFO"


class TigerLiveRunner:
    """Manages the full live trading cycle — broker + scheduler + data."""

    # Persist placed order keys to disk — no duplicates on restart
    _ORDER_KEYS_FILE = "/tmp/tiger_placed_orders.json"
    # Persist per-position peak + target_booked — Tiger's eyes stay open across restart
    _POSITION_TRACK_FILE = "/tmp/tiger_position_peaks.json"

    def __init__(self):
        self.broker: AngelBroker | None = None
        self.scheduler: TigerBrainScheduler | None = None
        self.data_map: dict = {}
        self.data_map_1m: dict = {}
        self.instrument_master = None
        self._running = False
        # Track placed order keys — load from disk, restart-safe
        self._placed_order_keys: set[str] = self._load_order_keys()
        self._order_log: list[dict] = []
        self._daily_entries_taken: dict = {}  # date → count (Brain 4 quota)
        # Scalper tracking — per-day count + last trade time
        self._scalper_trades: dict = {}  # date → count
        self._last_trade_time: datetime | None = None
        # Real account capital — fetched from Angel One pre-market
        self.account_capital: float = 0.0
        # Capital lifecycle: start → after_entry → after_exit
        self.capital_start: float = 0.0
        self.capital_after_entry: float = 0.0
        self.capital_after_exit: float = 0.0
        # Open position tracker — Tiger's eyes always on broker positions
        # {tsym: {"peak": float, "target_booked": bool, "entry": float, "is_scalper": bool}}
        self._position_peaks: dict = self._load_position_peaks()
        # Scalper positions tracker — tsym → True (for special exit rules)
        self._scalper_positions: set = self._load_scalper_positions()
        # Data refresh throttle — with 1-min scans, only refresh REST candles
        # every 5 min. SmartWebSocketV2 live ticks fill the gap between refreshes.
        self._last_data_refresh: datetime | None = None

    def _live_re_size(
        self, real_balance: float, real_ltp: float, real_lot_size: int,
        is_delivery: bool, current_exposure: float = 0.0,
    ) -> dict:
        """Derive quantity from REAL balance + REAL LTP + REAL lot size.

        Backtest derived quantity from a simulated premium — that could be
        wrong. On live orders we re-size with FUND BRAIN based on actual
        capital. Quantity is always a multiple of the lot size (so Angel
        One won't reject it).

        Returns:
            dict: {quantity, lots, allocated_capital, reason}
            quantity=0 means SKIP (not affordable or fund plan failed)
        """
        if real_balance <= 0 or real_ltp <= 0 or real_lot_size <= 0:
            return {"quantity": 0, "lots": 0, "allocated_capital": 0,
                    "reason": "invalid balance/ltp/lotsize"}

        lot = int(real_lot_size)
        try:
            plan = announce_fund_plan(real_balance)
        except ValueError as exc:
            logger.warning(f"Fund plan fail: {exc} — skip order")
            return {"quantity": 0, "lots": 0, "allocated_capital": 0,
                    "reason": f"fund_plan_fail: {exc}"}

        # Stop distance: entry × 40% (60% stop-loss = 40% risk per unit)
        entry = real_ltp
        stop = entry * 0.60
        sizing = size_trade_with_fund_brain(
            plan, entry, stop, lot,
            current_exposure=current_exposure,
            is_delivery=is_delivery)

        qty = sizing.get("quantity", 0)
        lots = sizing.get("lots", 0)

        # Safety: enforce lot multiple (fund brain already does this,
        # but double-check against REAL lot size)
        if qty > 0 and lot > 0:
            lots = qty // lot
            qty = lots * lot

        # Final affordability: qty × real_ltp MUST fit in balance
        cost = qty * real_ltp
        if cost > real_balance:
            affordable_lots = int(real_balance // (real_ltp * lot))
            if affordable_lots < 1:
                logger.info(
                    f"   💰 SKIP — 1 lot ₹{lot * real_ltp:,.0f} > "
                    f"balance ₹{real_balance:,.0f}")
                return {"quantity": 0, "lots": 0, "allocated_capital": 0,
                        "reason": "not_affordable"}
            qty = affordable_lots * lot
            cost = qty * real_ltp
            logger.info(
                f"   💰 Re-sized to {affordable_lots} lots = {qty} qty "
                f"= ₹{cost:,.0f} (fit balance ₹{real_balance:,.0f})")

        return {
            "quantity": qty,
            "lots": lots,
            "allocated_capital": round(cost, 2),
            "reason": sizing.get("reason", "sized_by_fund_brain"),
        }

    def _load_order_keys(self) -> set[str]:
        """Load placed order keys from disk (restart-safe)."""
        import json
        try:
            with open(self._ORDER_KEYS_FILE) as f:
                return set(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return set()

    def _save_order_keys(self):
        """Save placed order keys to disk."""
        import json
        try:
            with open(self._ORDER_KEYS_FILE, "w") as f:
                json.dump(sorted(self._placed_order_keys), f)
        except OSError as exc:
            logger.warning(f"Order keys save fail: {exc}")

    def _load_position_peaks(self) -> dict:
        """Load per-position peak + target_booked from disk (restart-safe).

        Even if Tiger restarts, open positions' peaks are remembered —
        trail locking won't break.
        """
        import json
        try:
            with open(self._POSITION_TRACK_FILE) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _save_position_peaks(self):
        """Save per-position peak + target_booked to disk."""
        import json
        try:
            with open(self._POSITION_TRACK_FILE, "w") as f:
                json.dump(self._position_peaks, f)
        except OSError as exc:
            logger.warning(f"Position peaks save fail: {exc}")

    _SCALPER_POSITIONS_FILE = "/tmp/tiger_scalper_positions.json"

    def _load_scalper_positions(self) -> set:
        """Load scalper positions from disk (restart-safe)."""
        import json
        try:
            with open(self._SCALPER_POSITIONS_FILE) as f:
                return set(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return set()

    def _save_scalper_positions(self):
        """Save scalper positions to disk."""
        import json
        try:
            with open(self._SCALPER_POSITIONS_FILE, "w") as f:
                json.dump(sorted(self._scalper_positions), f)
        except OSError as exc:
            logger.warning(f"Scalper positions save fail: {exc}")

    # ============================================================
    # PRE-MARKET (09:00) — login + data load
    # ============================================================
    def pre_market_wake(self):
        """Broker login + instrument master + fresh data fetch."""
        logger.info("=" * 60)
        logger.info("🐅 TIGER PRE-MARKET WAKE — %s", datetime.now().strftime("%A %Y-%m-%d"))
        logger.info("=" * 60)

        if get_day_mode() != "TRADING":
            logger.info("Today is NOT a TRADING day — pre-market skip.")
            return

        # 1. Broker login
        try:
            self.broker = AngelBroker()
            self.broker.ensure_logged_in()
            logger.info("✅ Angel One broker logged in.")
        except AngelConnectionError as exc:
            logger.error("❌ Broker login fail: %s — Tiger won't trade today.", exc)
            self.broker = None
            return

        # 2. Instrument master
        try:
            from data.loader import load_angel_instrument_master
            self.instrument_master = load_angel_instrument_master()
            logger.info("✅ Instrument master loaded (%d symbols).",
                        len(self.instrument_master) if self.instrument_master is not None else 0)
        except Exception as exc:
            logger.error("❌ Instrument master fail: %s", exc)

        # 2b. REAL account balance — fetched from Angel One
        # ❗ ₹10,000 fallback REMOVED. If balance not found, account_capital=0,
        # backtest simulation runs from ₹10,000 (for scanning) BUT real
        # orders won't be placed (get_balance() fail in _place_live_orders = return 0).
        try:
            self.account_capital = self.broker.get_balance()
            if self.account_capital <= 0:
                logger.warning("⚠️ Balance ₹0 — rmsLimit() fail. "
                               "Real orders BLOCKED. Scan simulation from ₹10,000.")
                self.account_capital = 0.0
            self.capital_start = self.account_capital
            self.capital_after_entry = self.account_capital
            self.capital_after_exit = self.account_capital
            logger.info("💰 Trading capital: ₹%.0f (100%% of Angel One balance)",
                        self.account_capital)
            logger.info("💰 Capital lifecycle START: ₹%.0f", self.capital_start)
        except Exception as exc:
            logger.error("❌ Balance fetch fail: %s — real orders BLOCKED.", exc)
            self.account_capital = 0.0

        # 3. Fetch fresh data — ACTIVE market symbols (NSE or MCX).
        # At 09:00 pre-market, no market is active yet -> default to NSE (next to open).
        # Mid-market restart: fetch whichever market is currently active.
        try:
            from backtest.run_tiger_brain_backtest import fetch_angel_data
            from universe.fno_universe import get_active_scan_symbols, nse_scan_symbols
            syms, market = get_active_scan_symbols()
            if not syms:
                syms = nse_scan_symbols()
                market = "NSE (pre-market)"
            self.data_map, self.data_map_1m, failed = fetch_angel_data(
                self.broker, days_15m=30, days_1m=7, fetch_1m=True,
                symbols=syms)
            logger.info("✅ Data fetched [%s]: %d symbols (15m), %d (1m). Failed: %d",
                        market, len(self.data_map), len(self.data_map_1m), len(failed))

            # 3b. Subscribe scan symbols to WebSocket for live tick stream
            self._subscribe_ws_symbols(list(syms.keys()))
        except Exception as exc:
            logger.error("❌ Data fetch fail: %s", exc)
            self.data_map, self.data_map_1m = {}, {}

    def _subscribe_ws_symbols(self, symbols: list[str]):
        """Subscribe scan symbols to WebSocket for real-time ticks.

        This replaces per-scan REST LTP calls with a persistent stream.
        Called after data fetch (pre-market + each market transition).
        """
        if self.broker is None or self.broker.websocket is None:
            return
        try:
            self.broker.websocket.subscribe_symbols(symbols)
            logger.info("📡 WS subscribed: %d scan symbols for live ticks",
                        len(symbols))
        except Exception as exc:
            logger.warning(f"WS subscribe fail (REST fallback): {exc}")

    # ============================================================
    # LIVE DATA REFRESH — fetch FRESH data every 20 min
    # ============================================================
    def _refresh_live_data(self):
        """Fetch FRESH 15m data on every intraday scan — ACTIVE market only.

        Two-market session:
          NSE (09:15-15:15): fetch 4 INDEX + up to 50 liquid STOCKS (Bhavcopy filter)
          MCX (15:30-23:15): fetch 4 MCX symbols (GOLDM, SILVERM, CRUDEOIL, NATURALGAS)

        INDEX scanned first (priority), STOCKS after. Options buying only.
        """
        if self.broker is None:
            return
        try:
            from backtest.run_tiger_brain_backtest import fetch_angel_data
            from universe.fno_universe import get_active_scan_symbols

            symbols, market = get_active_scan_symbols()
            if not symbols:
                logger.info("Live data refresh: market CLOSED, skip fetch.")
                return

            fresh_15m, fresh_1m, failed = fetch_angel_data(
                self.broker, days_15m=10, days_1m=3, fetch_1m=True,
                symbols=symbols)
            if fresh_15m:
                self.data_map = fresh_15m
            if fresh_1m:
                self.data_map_1m = fresh_1m
            logger.info("Live data refresh [%s]: %d symbols (15m), %d (1m). Failed: %d",
                        market, len(fresh_15m), len(fresh_1m), len(failed))

            # Re-subscribe to WebSocket for new market symbols
            self._subscribe_ws_symbols(list(symbols.keys()))
        except Exception as exc:
            logger.warning("Live data refresh fail — continuing with stale data: %s", exc)

    # ============================================================
    # MARKET OPEN (09:15) — ready signal
    # ============================================================
    def market_open(self):
        """NSE market open (09:15) — Tiger ready for NSE scanning."""
        logger.info("🐅 NSE MARKET OPEN (09:15) — Tiger ready for NSE scanning.")
        if self.broker is None or not self.broker.is_session_valid():
            logger.warning("⚠️ Broker session invalid — pre-market login did not happen.")
            self.pre_market_wake()
        # Tiger's eyes immediately on open positions — don't forget yesterday's trade
        self.monitor_open_positions()

    def mcx_market_open(self):
        """MCX market open (15:30) — Tiger switches to MCX scanning.

        NSE closed at 15:15 (square-off done). Tiger now hunts MCX
        commodities (GOLDM, SILVERM, CRUDEOIL, NATURALGAS) till 23:15.
        Does NOT call pre_market_wake() (which fetches NSE symbols).
        Instead: ensure broker login, then fetch MCX data only.
        """
        logger.info("🐅 MCX MARKET OPEN (15:30) — Tiger switches to MCX scanning.")
        # Ensure broker session (login if needed, but skip NSE data fetch)
        if self.broker is None or not self.broker.is_session_valid():
            try:
                self.broker = AngelBroker()
                self.broker.ensure_logged_in()
                logger.info("✅ Angel One broker logged in for MCX session.")
            except AngelConnectionError as exc:
                logger.error("❌ MCX broker login fail: %s", exc)
                self.broker = None
                return
        # Fetch MCX data only (4 symbols, not all 27)
        self._refresh_live_data()
        self.monitor_open_positions()

    # ============================================================
    # POSITION MONITOR — Tiger's eyes always on open positions
    # ============================================================
    def monitor_open_positions(self) -> int:
        """Apply V19 exit logic directly using real broker positions + LTP.

        Even if Tiger restarts, it fetches open positions from the broker
        and tracks their profit/loss. INDEPENDENT of the backtest engine —
        if the backtest isn't tracking a position (forgot on restart), Tiger
        still makes exit decisions from real broker data.

        For each open position:
          1. Real LTP fetch (Angel One ltpData)
          2. gain_pct = (ltp - entry) / entry * 100
          3. Stop-loss: ltp ≤ entry × 0.60 → EXIT
          4. Trail (active at +5%): ltp ≤ peak_lock → EXIT
          5. Fixed target (+50%): book 40% quantity
          6. Runaway safety (+250%): full exit
          7. Peak update + disk save (restart-safe)

        Returns:
            int: how many exit orders were placed
        """
        if self.broker is None:
            return 0
        try:
            positions = self.broker.get_positions()
        except Exception as exc:
            logger.error("👁️ Position monitor: fetch fail: %s", exc)
            return 0
        if not positions:
            logger.info("👁️ No open broker positions.")
            return 0

        from backtest.run_tiger_brain_backtest import (
            V19_TRAIL_ACTIVATE_PCT, V19_TRAIL_LOCK_PCT,
            V19_FIXED_TARGET_PCT, V19_FIXED_TARGET_BOOK,
            V19_RUNAWAY_EXIT_PCT,
        )

        closed = 0
        active_tsyms = set()
        for p in positions:
            tsym = p.get("tradingsymbol", "")
            token = p.get("symboltoken", "")
            exch = p.get("exchange", "")
            qty = int(p.get("netqty", 0) or 0)
            if qty <= 0 or not tsym:
                continue
            active_tsyms.add(tsym)

            entry_price = float(p.get("buyavgprice", 0)
                                or p.get("avgnetprice", 0) or 0)
            if entry_price <= 0:
                continue

            # Real LTP — fresh from broker
            ltp = self.broker.ws_get_ltp(tsym, token, exch)
            if ltp <= 0:
                ltp = float(p.get("ltp", 0) or 0)
            if ltp <= 0:
                logger.warning(f"👁️ {tsym}: LTP not found — skip.")
                continue

            gain_pct = (ltp - entry_price) / entry_price * 100

            # Position tracker load (peak + target_booked)
            tracker = self._position_peaks.get(tsym, {})
            peak = max(float(tracker.get("peak", 0) or 0), ltp, entry_price)
            target_booked = bool(tracker.get("target_booked", False))
            self._position_peaks[tsym] = {
                "peak": peak, "target_booked": target_booked,
                "entry": entry_price,
            }

            logger.info(
                f"👁️ {tsym}: entry=₹{entry_price:.2f} ltp=₹{ltp:.2f} "
                f"gain={gain_pct:+.1f}% peak=₹{peak:.2f} "
                f"{'[target_booked]' if target_booked else ''}"
                f"{' [SCALPER]' if tsym in self._scalper_positions else ''}")

            # === SCALPER EXIT LOGIC (fast in, fast out) ===
            # Scalper positions have different rules:
            #   - Target +15% → instant full exit (no partial booking)
            #   - Stop ₹500 loss → instant full exit
            #   - NO trail, NO fixed target booking, NO runaway
            if tsym in self._scalper_positions:
                from config.thresholds import SCALPER
                exit_reason = None
                exit_qty = qty

                # Scalper stop: ₹500 loss → full exit
                loss_rupees = (entry_price - ltp) * qty

                # Scalper target: +15% → full exit
                if gain_pct >= SCALPER["TARGET_PCT"]:
                    exit_reason = f"scalper_target_{SCALPER['TARGET_PCT']:.0f}pct"
                elif loss_rupees >= SCALPER["MAX_STOP_RUPEES"]:
                    exit_reason = f"scalper_stop_₹{SCALPER['MAX_STOP_RUPEES']}"

                if exit_reason:
                    pos_product = p.get("producttype", "INTRADAY")
                    if pos_product not in ("INTRADAY", "CARRYFORWARD"):
                        pos_product = "INTRADAY"
                    result = self.broker.place_option_order(
                        tradingsymbol=tsym, symboltoken=token, exchange=exch,
                        transaction_type="SELL", quantity=exit_qty,
                        product_type=pos_product, order_type="MARKET")
                    if result.get("success"):
                        closed += 1
                        self._scalper_positions.discard(tsym)
                        self._save_scalper_positions()
                        logger.info(
                            f"📤 SCALPER EXIT {tsym}: {exit_reason} — "
                            f"SELL {exit_qty}/{qty} @ LTP ₹{ltp:.2f} "
                            f"(gain {gain_pct:+.1f}%)")
                    else:
                        logger.error(
                            f"❌ SCALPER EXIT FAIL {tsym}: {exit_reason} — {result.get('error')}")
                continue  # scalper positions don't use V19 exit logic

            # === V19 EXIT LOGIC (on real broker data) ===
            exit_reason = None
            exit_qty = qty

            # 1. Stop-loss (60% of entry)
            stop_premium = entry_price * 0.60
            if ltp <= stop_premium:
                exit_reason = "stop_loss_60pct"

            # 2. Trail (active at +5%) — exit on 30% give-back from peak
            elif gain_pct >= V19_TRAIL_ACTIVATE_PCT:
                peak_gain = (peak - entry_price) / entry_price
                trail_floor = entry_price * (1 + peak_gain * V19_TRAIL_LOCK_PCT / 100)
                if ltp <= trail_floor:
                    exit_reason = "trail_lock_70pct"

            # 3. Fixed target — book 40% quantity at +50% (first time only)
            if exit_reason is None and gain_pct >= V19_FIXED_TARGET_PCT \
                    and not target_booked:
                exit_qty = max(1, int(qty * V19_FIXED_TARGET_BOOK))
                exit_reason = "fixed_target_50pct_book40"
                self._position_peaks[tsym]["target_booked"] = True

            # 4. Runaway safety
            if gain_pct >= V19_RUNAWAY_EXIT_PCT:
                exit_reason = "runaway_safety_250pct"
                exit_qty = qty

            # === EXIT ORDER PLACE ===
            # Product type MUST match the entry order's product type.
            # If the position was opened on CARRYFORWARD (delivery), the exit
            # must also be CARRYFORWARD — an INTRADAY exit will be rejected by
            # Angel (product type mismatch).
            if exit_reason:
                pos_product = p.get("producttype", "INTRADAY")
                if pos_product not in ("INTRADAY", "CARRYFORWARD"):
                    pos_product = "INTRADAY"
                result = self.broker.place_option_order(
                    tradingsymbol=tsym, symboltoken=token, exchange=exch,
                    transaction_type="SELL", quantity=exit_qty,
                    product_type=pos_product, order_type="MARKET")
                if result.get("success"):
                    closed += 1
                    logger.info(
                        f"📤 EXIT {tsym}: {exit_reason} — "
                        f"SELL {exit_qty}/{qty} @ LTP ₹{ltp:.2f} "
                        f"(gain {gain_pct:+.1f}%)")
                else:
                    logger.error(
                        f"❌ EXIT FAIL {tsym}: {exit_reason} — {result.get('error')}")

        # Cleanup: remove closed positions from the broker tracker
        for tsym in list(self._position_peaks.keys()):
            if tsym not in active_tsyms:
                del self._position_peaks[tsym]
                logger.info(f"👁️ {tsym}: position closed — tracker cleanup.")

        # Cleanup scalper positions that are no longer open
        closed_scalper = self._scalper_positions - active_tsyms
        if closed_scalper:
            for tsym in closed_scalper:
                self._scalper_positions.discard(tsym)
                logger.info(f"👁️ {tsym}: scalper position closed — cleanup.")
            self._save_scalper_positions()

        self._save_position_peaks()
        if closed:
            logger.info(f"👁️ Position monitor: {closed} exit orders placed.")
        return closed

    # ============================================================
    # INTRADAY SCAN (every 20 min) — entry signals + exits
    # ============================================================
    def intraday_scan(self):
        """Scan zones every 20 min, find entry signals, check exits.

        The backtest engine generates strategy signals. Then _place_live_orders()
        converts those signals into REAL Angel One orders.
        """
        if get_day_mode() != "TRADING":
            return
        if not is_market_hours():
            logger.info("Intraday scan: market is closed, skip.")
            return
        if is_opening_range_period():
            logger.info("Intraday scan: opening range period, skip (15 min wait).")
            return
        if self.broker is None:
            logger.warning("Intraday scan: no broker, skip.")
            return

        from automation.scheduler import get_active_market
        market = get_active_market()
        logger.info("🐅 INTRADAY SCAN [%s] — %s", market, datetime.now().strftime("%H:%M"))

        # WebSocket status — zero rate limits active?
        if self.broker.websocket is not None:
            ws_status = self.broker.websocket.status()
            if ws_status["healthy"]:
                logger.info("📡 WS: connected, %d ticks, %d tokens, last %ss ago",
                            ws_status["tick_count"], ws_status["subscribed_tokens"],
                            ws_status["last_tick_age_s"])
            else:
                logger.warning("📡 WS: unhealthy (%s) — REST fallback active",
                               ws_status.get("last_error", "disconnected"))

        # === FRESH DATA — throttled refresh (every 5 min, not every 1-min scan) ===
        # With 1-min scan intervals, fetching REST candles for 42 symbols every
        # minute would hit Angel One rate limits. SmartWebSocketV2 live ticks
        # update prices between refreshes. Refresh every 5th minute only.
        now_dt = datetime.now()
        need_refresh = True
        if self._last_data_refresh is not None:
            mins_since = (now_dt - self._last_data_refresh).total_seconds() / 60
            if mins_since < 5.0:
                need_refresh = False
        if need_refresh:
            self._refresh_live_data()
            self._last_data_refresh = now_dt
        else:
            logger.debug("Data refresh skipped (last %.0f min ago) — using WS live ticks",
                         mins_since)

        # === TIGER'S EYES — monitor open positions first ===
        # Fetch real LTP from broker + apply V19 exit logic. INDEPENDENT of backtest.
        # Don't forget yesterday's trade — check positions on every scan.
        try:
            monitored = self.monitor_open_positions()
        except Exception as exc:
            logger.error("👁️ Position monitor error: %s", exc)
            monitored = 0

        # === HARD 50s TIMEOUT — 1-min scan must never block the scheduler ===
        # If the scan exceeds 50s (slow REST, rate limit, hung API call),
        # abort it so the next 1-min cycle can fire cleanly.
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

        def _run_scan():
            try:
                # === REAL-TIME LIVE SCANNER (independent of backtest) ===
                from automation.live_scanner import scan_live_signals

                today = datetime.now().date()
                daily_entries = self._daily_entries_taken.get(today, 0)
                scalper_today = self._scalper_trades.get(today, 0)

                signals = scan_live_signals(
                    self.data_map,
                    self.data_map_1m if self.data_map_1m else None,
                    self.broker,
                    now=datetime.now(),
                    daily_entries_taken=daily_entries,
                    last_trade_time=self._last_trade_time,
                    scalper_trades_today=scalper_today,
                )

                # Signals are for the current bar — all are entries (exit_ts = None).
                # Exits already came from monitor_open_positions() (above).
                placed = self._place_live_orders(signals)

                # Update the daily entry counter
                self._daily_entries_taken[today] = daily_entries + placed
                if placed > 0:
                    self._last_trade_time = datetime.now()
                    # Track scalper trades separately
                    for s in signals:
                        if s.get("is_scalper") and placed > 0:
                            self._scalper_trades[today] = scalper_today + 1
                            break

                logger.info("Scan done: %d live signals, %d buy orders placed, "
                            "%d monitored exits, balance ₹%.0f",
                            len(signals), placed, monitored,
                            self.account_capital)
            except Exception as exc:
                logger.error("Intraday scan error: %s", exc)

        try:
            with ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(_run_scan)
                future.result(timeout=50)
        except FuturesTimeout:
            logger.warning("⚠️ Scan timed out after 50s — aborting (next 1-min cycle will retry)")
        except Exception as exc:
            logger.error("Scan wrapper error: %s", exc)

    def _place_live_orders(self, trades: list[dict]) -> int:
        """Backtest signals → REAL Angel One orders.

        Before each trade Tiger itself checks:
          1. Real Angel One balance fetch (₹)
          2. Option contract's real lot_size (from instrument master)
          3. REAL market LTP fetch (from ltpData API — NOT simulated premium)
          4. Real trade cost = quantity × real_ltp
          5. Affordable? real_cost ≤ available balance
             → YES: place order
             → NO:  skip (even a single lot didn't fit)
          6. After order placement, check STATUS (rejected or not?)
          7. Full capital lifecycle log: start → after_entry → remaining

        Returns:
            int: how many real orders were successfully placed + accepted
        """
        if not trades or self.broker is None:
            return 0

        today = datetime.now().date()
        placed_count = 0
        now = datetime.now()

        # Step 1: Real balance fetch (fresh on every scan)
        # ❗ FAIL = NO orders. ₹10,000 fallback REMOVED — Tiger should not
        # place orders from a wrong balance. If balance not found, stop.
        # 2 retries (get_balance internal + here): handle transient fail
        # (rate limit, session expire); genuine fail = no orders.
        available_balance = 0.0
        for bal_attempt in range(1, 3):
            try:
                available_balance = self.broker.get_balance()
            except Exception as exc:
                logger.error("❌ Balance fetch FAIL (attempt %d/2): %s", bal_attempt, exc)
                available_balance = 0.0
            if available_balance > 0:
                break
            if bal_attempt < 2:
                logger.warning("⚠️ Balance 0 — wait 2s, retry...")
                time.sleep(2)
        if available_balance <= 0:
            logger.error("❌ Balance fetch FAILED after 2 retries — no orders.")
            return 0

        # Capital lifecycle
        if self.capital_after_exit > 0:
            available_balance = max(available_balance, self.capital_after_exit)

        # NSE hard cutoff — no NSE orders after 15:30 (MARKET_CLOSE_TIME)
        # (neither intraday nor delivery). Only MCX commodity allowed
        # (MCX runs 09:00-23:30).
        # Between 15:00-15:30: NSE delivery allowed, intraday blocked (strategy).
        nse_close_str = AUTOMATION.get("MARKET_CLOSE_TIME", "15:30")
        nse_close_h, nse_close_m = map(int, nse_close_str.split(":"))
        nse_hard_cutoff = now.replace(
            hour=nse_close_h, minute=nse_close_m, second=0, microsecond=0)
        is_nse_closed = now >= nse_hard_cutoff

        # Intraday strategy cutoff — new intraday blocked after 15:00,
        # only delivery + MCX.
        cutoff_str = AUTOMATION.get("INTRADAY_ENTRY_CUTOFF_TIME", "15:00")
        cutoff_h, cutoff_m = map(int, cutoff_str.split(":"))
        intraday_cutoff = now.replace(hour=cutoff_h, minute=cutoff_m,
                                      second=0, microsecond=0)
        is_after_intraday_cutoff = now >= intraday_cutoff

        if is_nse_closed:
            logger.info("=" * 60)
            logger.info("🌙 NSE CLOSED (15:30) — only MCX commodity allowed. "
                        "No NSE orders (intraday/delivery both blocked).")
            logger.info("💰 Available balance: ₹%.0f", available_balance)
            logger.info("=" * 60)
        elif is_after_intraday_cutoff:
            logger.info("=" * 60)
            logger.info("⏰ 3 PM CUTOFF — NSE intraday blocked. "
                        "Only NSE delivery + MCX commodity allowed.")
            logger.info("💰 Available balance: ₹%.0f", available_balance)
            logger.info("💰 Capital lifecycle: START ₹%.0f → now ₹%.0f",
                        self.capital_start, available_balance)
            logger.info("=" * 60)
        else:
            logger.info("=" * 60)
            logger.info("💰 CAPITAL CHECK — Available balance: ₹%.0f",
                        available_balance)
            logger.info("💰 Capital lifecycle: START ₹%.0f → now ₹%.0f",
                        self.capital_start, available_balance)
            logger.info("=" * 60)

        for t in trades:
            entry_ts = t.get("entry_ts")
            if entry_ts is None:
                continue
            try:
                trade_date = entry_ts.date() if hasattr(entry_ts, 'date') else \
                    pd.Timestamp(entry_ts).date()
            except Exception:
                continue
            if trade_date != today:
                continue

            symbol = t.get("symbol", "")
            strike = t.get("strike", 0)
            option_type = t.get("option_type", "")
            direction = t.get("direction", "")
            # NOTE: backtest quantity is IGNORED — re-sized by Fund Brain using
            # REAL balance + REAL LTP + REAL lot size
            is_delivery = t.get("is_delivery", False)

            # NSE hard cutoff: no NSE orders after 15:30
            # (neither intraday nor delivery — both blocked). Only MCX commodity.
            # 15:00-15:30: NSE delivery allowed, intraday blocked.
            is_mcx_commodity = MARKET_CATEGORIES.get(
                resolve_exchange_for_symbol(symbol), "") == "commodity"
            if is_nse_closed and not is_mcx_commodity:
                logger.info(
                    f"   🌙 SKIP {symbol} {strike}{option_type} — "
                    f"NSE closed (15:30), only MCX commodity allowed")
                continue
            if is_after_intraday_cutoff and not is_delivery and not is_mcx_commodity:
                logger.info(
                    f"   ⏰ SKIP {symbol} {strike}{option_type} — "
                    f"NSE intraday blocked after 3PM, only delivery/MCX")
                continue

            # Duplicate check
            order_key = f"{symbol}_{strike}_{option_type}_{trade_date}"
            if order_key in self._placed_order_keys:
                continue

            # Step 2: Resolve contract with real lot_size
            contract = resolve_option_contract(symbol, strike, option_type)
            if contract is None:
                logger.warning(
                    f"⚠️ Order skip: {symbol} {strike}{option_type} token not found")
                continue

            real_lot_size = contract.get("lotsize", 1)
            if real_lot_size <= 0:
                real_lot_size = 1

            # Step 4: REAL re-size with FUND BRAIN — not the backtest's qty!
            # Backtest derived qty from a simulated premium — that can be WRONG.
            # Now we size properly via Fund Brain using REAL balance + REAL LTP +
            # REAL lot size. Quantity is always a multiple of the lot size (P2 fix).
            sim_premium = t.get("entry_premium", 0.0)
            real_ltp = self.broker.ws_get_ltp(
                contract["tradingsymbol"],
                contract["symboltoken"],
                contract["exchange"],
            )
            # If LTP fetch fails, fall back to simulated premium (with warning)
            if real_ltp <= 0:
                logger.warning(
                    f"   ⚠️ LTP fetch fail — using simulated premium ₹{sim_premium:.2f}")
                real_ltp = sim_premium if sim_premium > 0 else 0.5

            one_lot_cost = real_lot_size * real_ltp

            # Current exposure: total deployed capital in open positions
            current_exposure = sum(
                float(o.get("trade_cost", 0)) for o in self._order_log
                if o.get("success")
            )

            # MCX MINI fallback — if a full-size MCX contract is not affordable,
            # try the MINI variant (smaller lot = less capital).
            from data.loader import MCX_MINI_FALLBACK
            if (one_lot_cost > available_balance
                    and symbol in MCX_MINI_FALLBACK):
                mini_symbol = MCX_MINI_FALLBACK[symbol]
                mini_contract = resolve_option_contract(
                    mini_symbol, strike, option_type)
                if mini_contract is not None:
                    mini_lot = mini_contract.get("lotsize", 1) or 1
                    mini_ltp = self.broker.ws_get_ltp(
                        mini_contract["tradingsymbol"],
                        mini_contract["symboltoken"],
                        mini_contract["exchange"],
                    )
                    if mini_ltp <= 0:
                        mini_ltp = real_ltp
                    mini_one_lot = mini_lot * mini_ltp
                    if mini_one_lot <= available_balance:
                        logger.info(
                            f"   🔄 MINI fallback: {symbol}→{mini_symbol} "
                            f"(lot {real_lot_size}→{mini_lot}, "
                            f"cost ₹{one_lot_cost:,.0f}→₹{mini_one_lot:,.0f})")
                        contract = mini_contract
                        real_lot_size = mini_lot
                        real_ltp = mini_ltp
                        one_lot_cost = mini_one_lot

            # 🔥 FUND BRAIN LIVE SIZING — real balance + real LTP + real lot
            re_size = self._live_re_size(
                real_balance=available_balance,
                real_ltp=real_ltp,
                real_lot_size=real_lot_size,
                is_delivery=is_delivery,
                current_exposure=current_exposure,
            )
            quantity = re_size["quantity"]
            trade_cost = re_size["allocated_capital"]
            sizing_reason = re_size["reason"]

            logger.info("-" * 60)
            logger.info(f"📊 {symbol} {strike}{option_type} ({direction})")
            logger.info(f"   Lot Size:      {real_lot_size}")
            logger.info(f"   Sim Premium:   ₹{sim_premium:.2f} (backtest)")
            logger.info(f"   REAL LTP:      ₹{real_ltp:.2f} (market)")
            logger.info(f"   Fund Brain:    {quantity} qty ({quantity // real_lot_size if real_lot_size else 0} lots) [{sizing_reason}]")
            logger.info(f"   Real Cost:     ₹{trade_cost:,.0f} ({quantity} × ₹{real_ltp:.2f})")
            logger.info(f"   1 Lot Cost:    ₹{one_lot_cost:,.0f}")
            logger.info(f"   Balance:       ₹{available_balance:,.0f}")

            # Step 5: Affordability gate — quantity 0 means SKIP
            if quantity <= 0:
                logger.info(
                    f"   ❌ SKIP {symbol} {strike}{option_type} — "
                    f"{sizing_reason} (balance ₹{available_balance:,.0f})")
                self._order_log.append({
                    "time": datetime.now().isoformat(),
                    "symbol": symbol, "strike": strike,
                    "option_type": option_type,
                    "tradingsymbol": contract["tradingsymbol"],
                    "real_ltp": real_ltp, "one_lot_cost": one_lot_cost,
                    "balance": available_balance,
                    "success": False, "error": sizing_reason,
                })
                continue

            # Final safety: trade_cost must fit balance
            if trade_cost > available_balance:
                logger.info(
                    f"   ❌ SKIP — cost ₹{trade_cost:,.0f} > balance "
                    f"₹{available_balance:,.0f} (safety gate)")
                continue

            # === DYNAMIC CAPITAL MANAGEMENT (Mandate 2) ===
            # Pre-order RMS check: block if insufficient free disposable
            # margin. Conviction-based dynamic allocation from 7-brain
            # alignment. Prevents margin rejection before order hits RMS.
            setup_score = t.get("setup_score", 0.0)
            brain_alignment = count_aligned_brains(t)

            cap_check = CapitalManager(self.broker).check_and_allocate(
                setup_score=setup_score,
                brain_alignment=brain_alignment,
                trade_cost_estimate=trade_cost,
                open_positions_cost=current_exposure,
                min_allocation=one_lot_cost,
            )
            if not cap_check.allowed:
                logger.info(
                    f"   🛑 CAPITAL BLOCK: {symbol} {strike}{option_type} — "
                    f"{cap_check.reason}")
                logger.info(
                    f"      Available: ₹{cap_check.available_funds:,.0f} | "
                    f"Deployed: ₹{cap_check.deployed_capital:,.0f} | "
                    f"Free: ₹{cap_check.free_disposable:,.0f}")
                self._order_log.append({
                    "time": datetime.now().isoformat(),
                    "symbol": symbol, "strike": strike,
                    "option_type": option_type,
                    "tradingsymbol": contract["tradingsymbol"],
                    "quantity": quantity, "real_ltp": real_ltp,
                    "trade_cost": trade_cost,
                    "balance": available_balance,
                    "success": False,
                    "error": cap_check.reason,
                    "capital_blocked": True,
                })
                continue

            logger.info(
                f"   💰 Capital tier: {cap_check.conviction_tier} "
                f"({cap_check.conviction_multiplier:.0%} of margin) | "
                f"Allocated: ₹{cap_check.allocated_capital:,.0f}")

            # Step 6: Place REAL BUY order (Tiger always buys options)
            # Delivery = CARRYFORWARD (overnight), Intraday = INTRADAY
            transaction_type = "BUY"
            product_type = "CARRYFORWARD" if is_delivery else "INTRADAY"
            result = self.broker.place_option_order(
                tradingsymbol=contract["tradingsymbol"],
                symboltoken=contract["symboltoken"],
                exchange=contract["exchange"],
                transaction_type=transaction_type,
                quantity=quantity,
                product_type=product_type,
                order_type="MARKET",
            )

            if result.get("success"):
                # Step 7: Check order STATUS — rejected or not?
                import time as _time
                _time.sleep(2)  # Allow RMS to process
                status = self.broker.get_order_status(result["order_id"])
                order_status = status.get("status", "").lower()
                reject_reason = status.get("reject_reason")

                if "reject" in order_status or reject_reason:
                    logger.error(
                        f"   ❌ ORDER REJECTED by RMS: {reject_reason}")
                    logger.error(
                        f"   ❌ {transaction_type} {quantity} "
                        f"{contract['tradingsymbol']} REJECTED")
                    self._order_log.append({
                        "time": datetime.now().isoformat(),
                        "symbol": symbol, "strike": strike,
                        "option_type": option_type,
                        "tradingsymbol": contract["tradingsymbol"],
                        "quantity": quantity, "real_ltp": real_ltp,
                        "trade_cost": trade_cost,
                        "order_id": result["order_id"],
                        "success": False, "error": f"REJECTED: {reject_reason}",
                        "reject_reason": reject_reason,
                    })
                    continue

                # Order accepted!
                placed_count += 1
                self._placed_order_keys.add(order_key)
                self._save_order_keys()  # save to disk — restart-safe
                available_balance -= trade_cost
                self.capital_after_entry = available_balance

                # Track scalper positions for special exit rules
                is_scalper = t.get("is_scalper", False)
                if is_scalper:
                    self._scalper_positions.add(contract["tradingsymbol"])
                    self._save_scalper_positions()
                    logger.info(f"   🐅 SCALPER position tracked: {contract['tradingsymbol']}")

                logger.info(
                    f"   ✅ Order accepted: {order_status}")
                logger.info(
                    f"   🔥 REAL ORDER: BUY {quantity} "
                    f"{contract['tradingsymbol']} ({option_type}) "
                    f"cost ₹{trade_cost:,.0f} → order_id={result['order_id']}"
                    f"{' [SCALPER]' if is_scalper else ''}")
                logger.info(
                    f"   💰 Remaining balance: ₹{available_balance:,.0f}")
            else:
                logger.error(
                    f"   ❌ Order fail: BUY {quantity} "
                    f"{contract['tradingsymbol']} — {result.get('error', '?')}")

            self._order_log.append({
                "time": datetime.now().isoformat(),
                "symbol": symbol, "strike": strike,
                "option_type": option_type, "direction": direction,
                "transaction_type": transaction_type,
                "quantity": quantity,
                "lot_size": real_lot_size,
                "sim_premium": sim_premium,
                "real_ltp": real_ltp,
                "trade_cost": trade_cost,
                "balance": available_balance,
                "tradingsymbol": contract["tradingsymbol"],
                "order_id": result.get("order_id"),
                "success": result.get("success", False),
                "error": result.get("error"),
            })

        # Update capital lifecycle
        self.capital_after_entry = available_balance
        logger.info("=" * 60)
        logger.info("💰 Capital: START ₹%.0f → AFTER ENTRY ₹%.0f → "
                    "orders placed: %d",
                    self.capital_start, available_balance, placed_count)
        logger.info("=" * 60)
        return placed_count

    def _place_exit_orders(self, exit_trades: list[dict]) -> int:
        """Backtest exit signals → REAL SELL orders (close positions).

        The backtest generated a PREMIUM EXIT / stop-loss / square-off signal.
        Now find the open position on the real broker and place a SELL order.

        Returns:
            int: how many positions were successfully closed
        """
        if not exit_trades or self.broker is None:
            return 0

        # Fetch real broker positions (what we actually hold)
        try:
            open_positions = self.broker.get_positions()
        except Exception as exc:
            logger.error("❌ Exit orders: position fetch fail: %s", exc)
            return 0

        # Build map: tradingsymbol → net quantity (from real broker)
        broker_positions = {}
        for p in open_positions:
            tsym = p.get("tradingsymbol", "")
            qty = int(p.get("netqty", 0) or 0)
            if qty > 0 and tsym:
                broker_positions[tsym] = p

        if not broker_positions:
            logger.info("📤 No open positions to exit — skip sell orders.")
            return 0

        closed_count = 0

        for t in exit_trades:
            entry_ts = t.get("entry_ts")
            if entry_ts is None:
                continue

            symbol = t.get("symbol", "")
            strike = t.get("strike", 0)
            option_type = t.get("option_type", "")
            exit_reason = t.get("exit_reason", "unknown")

            # Resolve contract to get tradingsymbol
            contract = resolve_option_contract(symbol, strike, option_type)
            if contract is None:
                logger.warning(
                    f"   ⚠️ Exit skip: {symbol} {strike}{option_type} "
                    f"contract not found")
                continue

            tsym = contract["tradingsymbol"]
            pos = broker_positions.get(tsym)
            if pos is None:
                logger.info(
                    f"   ⏭️ Exit skip: {tsym} not in open positions "
                    f"(already closed or not held)")
                continue

            qty = int(pos.get("netqty", 0) or 0)
            if qty <= 0:
                continue

            # Product type MUST match the entry order's product type.
            # Delivery (CARRYFORWARD) positions can be from a previous day —
            # the exit must also be CARRYFORWARD, otherwise Angel rejects an
            # INTRADAY exit.
            pos_product = pos.get("producttype", "INTRADAY")
            if pos_product not in ("INTRADAY", "CARRYFORWARD"):
                pos_product = "INTRADAY"

            # Place SELL order to close
            result = self.broker.place_option_order(
                tradingsymbol=tsym,
                symboltoken=contract["symboltoken"],
                exchange=contract["exchange"],
                transaction_type="SELL",
                quantity=qty,
                product_type=pos_product,
                order_type="MARKET",
            )

            if result.get("success"):
                import time as _time
                _time.sleep(2)
                status = self.broker.get_order_status(result["order_id"])
                order_status = status.get("status", "").lower()
                reject_reason = status.get("reject_reason")
                if "reject" in order_status or reject_reason:
                    logger.error(
                        f"   ❌ SELL REJECTED: {tsym} qty={qty} "
                        f"reason={reject_reason}")
                else:
                    closed_count += 1
                    logger.info(
                        f"   🔥 SELL ORDER: {qty} {tsym} "
                        f"reason={exit_reason} → order_id={result['order_id']}")
            else:
                logger.error(
                    f"   ❌ SELL fail: {qty} {tsym} — "
                    f"{result.get('error', '?')}")

        if closed_count > 0:
            self._log_capital_after_exit(f"Exit orders ({closed_count} closed)")
        return closed_count

    def delivery_snapshot(self):
        """3:00 PM — Decide next-day direction and place delivery orders.

        Tiger decides based on the market at EOD:
        - Gap-up likely → BUY CE (call option) delivery
        - Gap-down likely → BUY PE (put option) delivery

        Delivery = CARRYFORWARD (overnight hold), square-off next day.

        After 3 PM, new intraday orders are blocked — only this delivery + exits.
        """
        logger.info("=" * 60)
        logger.info("🐅 DELIVERY SNAPSHOT (3:00 PM) — Next-day direction")
        logger.info("=" * 60)
        if self.broker is None:
            logger.info("No broker — skip delivery.")
            return
        try:
            # === REAL-TIME LIVE SCANNER (for delivery too) ===
            # Previously a backtest simulation ran — now a real-time scan.
            # Delivery requires high-score signals (90+ score), so min_score
            # is raised to DELIVERY_ROCKET_MIN_SCORE.
            from automation.live_scanner import scan_live_signals
            from backtest.run_tiger_brain_backtest import DELIVERY_ROCKET_MIN_SCORE

            today = datetime.now().date()
            daily_entries = self._daily_entries_taken.get(today, 0)

            signals = scan_live_signals(
                self.data_map,
                self.data_map_1m if self.data_map_1m else None,
                self.broker,
                now=datetime.now(),
                daily_entries_taken=daily_entries,
            )
            # Only ultra-high-conviction signals for delivery
            delivery_trades = [s for s in signals
                               if s.get("setup_score", 0) >= DELIVERY_ROCKET_MIN_SCORE]
            for s in delivery_trades:
                s["is_delivery"] = True

            logger.info("Delivery signals: %d (score ≥ %d)",
                        len(delivery_trades), DELIVERY_ROCKET_MIN_SCORE)
            if delivery_trades:
                placed = self._place_live_orders(delivery_trades)
                self._daily_entries_taken[today] = daily_entries + placed
                logger.info("Delivery orders placed: %d", placed)
            else:
                logger.info("No delivery signal — no overnight today.")
        except Exception as exc:
            logger.error("Delivery snapshot error: %s", exc)

    # ============================================================
    # MARKET CLOSE — NSE 15:15 square-off + MCX 23:15 square-off
    # ============================================================
    def nse_square_off(self):
        """Close NSE/NFO positions (15:15 IST).

        Closes only NFO positions — MCX positions stay open because
        MCX is open until 23:30.
        """
        logger.info("=" * 60)
        logger.info("🐅 NSE SQUARE-OFF (15:15) — NFO positions close")
        logger.info("=" * 60)
        if self.broker is None:
            logger.info("No broker — nothing to close.")
            return
        try:
            closed = self.broker.square_off_all(exchange="NFO")
            logger.info("✅ NSE square-off: %d positions closed.", closed)
        except Exception as exc:
            logger.error("❌ NSE square-off error: %s", exc)
        # Update capital after exits
        self._log_capital_after_exit("NSE square-off")

    def mcx_square_off(self):
        """Close MCX positions (23:15 IST).

        Commodity positions close at 23:15 — MCX is open until 23:30,
        so it closes at a separate time.
        """
        logger.info("=" * 60)
        logger.info("🐅 MCX SQUARE-OFF (23:15) — MCX positions close")
        logger.info("=" * 60)
        if self.broker is None:
            logger.info("No broker — nothing to close.")
            return
        try:
            closed = self.broker.square_off_all(exchange="MCX")
            logger.info("✅ MCX square-off: %d positions closed.", closed)
        except Exception as exc:
            logger.error("❌ MCX square-off error: %s", exc)
        # Update capital after exits + logout
        self._log_capital_after_exit("MCX square-off")
        try:
            self.broker.logout()
            logger.info("✅ Broker logged out (end of trading day).")
        except Exception as exc:
            logger.warning("Logout warning: %s", exc)

    def _log_capital_after_exit(self, label: str):
        """Fetch real balance after exit + calculate P&L.

        Capital lifecycle complete:
          START (pre-market) → AFTER ENTRY (orders placed) → AFTER EXIT
        """
        if self.broker is None:
            return
        try:
            self.capital_after_exit = self.broker.get_balance()
        except Exception:
            pass
        pnl = self.capital_after_exit - self.capital_start
        pnl_pct = (pnl / self.capital_start * 100) if self.capital_start > 0 else 0
        logger.info("=" * 60)
        logger.info("💰 CAPITAL LIFECYCLE — %s", label)
        logger.info("   START:        ₹%.0f", self.capital_start)
        logger.info("   AFTER ENTRY:  ₹%.0f", self.capital_after_entry)
        logger.info("   AFTER EXIT:   ₹%.0f", self.capital_after_exit)
        logger.info("   P&L:          ₹%+.0f (%+.2f%%)", pnl, pnl_pct)
        logger.info("=" * 60)

    def market_close(self):
        """NSE market close (15:30) — NSE session ended, MCX continues.

        NSE square-off already happened at 15:15. This is just a log marker.
        MCX positions are NOT closed here — MCX runs till 23:15.
        NO logout — Tiger needs broker session for MCX scanning.
        """
        logger.info("=" * 60)
        logger.info("🐅 NSE MARKET CLOSE (15:30) — NSE session ended. MCX continues till 23:15.")
        logger.info("=" * 60)
        if self.broker is None:
            logger.info("No broker.")
            return
        # Do NOT square_off_all() here — MCX positions must stay open.
        # NSE square-off already ran at 15:15 (nse_square_off).
        # MCX square-off will run at 23:15 (mcx_square_off).
        self._log_capital_after_exit("NSE close 15:30 (MCX continues)")

    # ============================================================
    # NIGHTLY REPLAY (00:00) — audit + pattern tracking
    # ============================================================
    def nightly_replay(self):
        """Nightly audit of today's trades + pattern tracking."""
        logger.info("=" * 60)
        logger.info("🐅 NIGHTLY REPLAY — %s", datetime.now().strftime("%Y-%m-%d"))
        logger.info("=" * 60)
        try:
            from replay.nightly_replay import run_nightly_replay, load_trade_log
            records = load_trade_log()
            result = run_nightly_replay(records)
            audit = result.get("audit", {})
            logger.info("Replay: %d trades, win-rate %.1f%%, PnL ₹%.0f",
                        audit.get("total_trades", 0),
                        audit.get("win_rate_pct", 0),
                        audit.get("total_pnl", 0))
            for note in result.get("notes", []):
                logger.info("  → %s", note)
        except Exception as exc:
            logger.error("Nightly replay error: %s", exc)

    # ============================================================
    # DAILY CLEANUP (08:55) — purge stale logs + refresh scrip master
    # ============================================================
    def daily_cleanup(self):
        """Morning disk cleanup: delete old logs, refresh Scrip Master."""
        try:
            broker = getattr(self, "broker", None)
            run_daily_cleanup(broker=broker)
        except Exception as exc:
            logger.error("Daily cleanup error: %s", exc)

    # ============================================================
    # START — wire all jobs + run scheduler
    # ============================================================
    def start(self):
        """Wire all trading functions to the scheduler + run 24x7."""
        logger.info("=" * 60)
        logger.info("🐅  TIGER V19 — LIVE AUTOMATION STARTING")
        logger.info("🐅  24x7 cycle: Mon-Fri trading, Sat watch, Sun OFF")
        logger.info("=" * 60)

        self.scheduler = TigerBrainScheduler()
        self.scheduler.setup_jobs(
            pre_market_fn=self.pre_market_wake,
            market_open_fn=self.market_open,
            intraday_fn=self.intraday_scan,
            market_close_fn=self.market_close,
            nightly_replay_fn=self.nightly_replay,
            nse_square_off_fn=self.nse_square_off,
            mcx_square_off_fn=self.mcx_square_off,
            delivery_snapshot_fn=self.delivery_snapshot,
            mcx_market_open_fn=self.mcx_market_open,
            daily_cleanup_fn=self.daily_cleanup,
        )
        self.scheduler.start()
        self._running = True
        logger.info("✅ Tiger scheduler STARTED. 24x7 cycle active.")
        logger.info("   Pre-market:     09:00 (login + NSE data fetch)")
        logger.info("   NSE open:       09:15 (scan 4 INDEX + up to 50 liquid STOCKS)")
        logger.info("   Intraday:       every 1 min (active market only, data refresh 5 min)")
        logger.info("   Delivery:       15:00 (overnight direction)")
        logger.info("   NSE square-off: 15:15 (close NSE positions)")
        logger.info("   NSE close:      15:30 (NSE session end)")
        logger.info("   MCX open:       15:30 (scan 4 MCX symbols)")
        logger.info("   MCX square-off: 23:15 (close MCX positions + logout)")
        logger.info("   Nightly:        00:00")
        logger.info("")
        logger.info("🐅 Tiger is live. Ctrl+C will shut it down.")

        # Mid-market startup: if the market is already open, log in immediately
        if get_day_mode() == "TRADING" and is_market_hours():
            market = get_active_market()
            logger.info("🐅 %s market already open — immediate broker login + scan start.", market)
            # pre_market_wake fetches ACTIVE market data (NSE or MCX depending on time)
            self.pre_market_wake()
            self.monitor_open_positions()

        # Graceful shutdown
        def _shutdown(signum, frame):
            logger.info("🛑 Shutdown signal received — Tiger stopping...")
            self.stop()

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        # Keep main thread alive
        try:
            while self._running:
                time.sleep(60)
                mode = get_day_mode()
                if mode == "TRADING" and is_market_hours():
                    logger.debug("Tiger alive — TRADING mode (%s)",
                                 datetime.now().strftime("%H:%M"))
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        """Scheduler shutdown."""
        if self.scheduler:
            self.scheduler.shutdown()
        self._running = False
        logger.info("🛑 Tiger scheduler stopped. Goodbye 🐯")


# ============================================================
# ENTRY POINT — python3 -m automation.tiger_live
# ============================================================
if __name__ == "__main__":
    runner = TigerLiveRunner()
    runner.start()
