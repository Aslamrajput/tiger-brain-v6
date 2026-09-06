"""Tiger V19 — Live Trading Runner (24x7 Automation)

Ye module Tiger ko LIVE mode mein chalata hai AWS pe. Poora cycle:
  09:00  Pre-market wake  → broker login + instrument master + data fetch
  09:15  Market open      → intraday scanning start
  20min  Intraday scan    → zone detect → entry signal → real order
  15:15  NSE square-off   → close all NSE positions (smart)
  23:15  MCX square-off   → close all MCX positions (smart)
  00:00  Nightly replay   → audit day + pattern tracking

Chalane ke liye (AWS pe):
  python3 -m automation.tiger_live

Ya scheduler se:
  python3 -m automation.scheduler
"""
from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import datetime

logger = logging.getLogger("tiger_brain.tiger_live")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from broker.angel_connect import AngelBroker, AngelConnectionError
from automation.scheduler import (
    TigerBrainScheduler, get_day_mode, is_market_hours, is_opening_range_period,
)


class TigerLiveRunner:
    """Poora live trading cycle manage karta hai — broker + scheduler + data."""

    def __init__(self):
        self.broker: AngelBroker | None = None
        self.scheduler: TigerBrainScheduler | None = None
        self.data_map: dict = {}
        self.data_map_1m: dict = {}
        self.instrument_master = None
        self._running = False

    # ============================================================
    # PRE-MARKET (09:00) — login + data load
    # ============================================================
    def pre_market_wake(self):
        """Broker login + instrument master + fresh data fetch."""
        logger.info("=" * 60)
        logger.info("🐅 TIGER PRE-MARKET WAKE — %s", datetime.now().strftime("%A %Y-%m-%d"))
        logger.info("=" * 60)

        if get_day_mode() != "TRADING":
            logger.info("Aaj TRADING day nahi — pre-market skip.")
            return

        # 1. Broker login
        try:
            self.broker = AngelBroker()
            self.broker.ensure_logged_in()
            logger.info("✅ Angel One broker logged in.")
        except AngelConnectionError as exc:
            logger.error("❌ Broker login fail: %s — Tiger aaj trade nahi karega.", exc)
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

        # 3. Fetch fresh data
        try:
            from backtest.run_tiger_brain_backtest import fetch_angel_data
            self.data_map, self.data_map_1m, failed = fetch_angel_data(
                self.broker, use_scan_universe=True)
            logger.info("✅ Data fetched: %d symbols (15m), %d symbols (1m). Failed: %d",
                        len(self.data_map), len(self.data_map_1m), len(failed))
        except Exception as exc:
            logger.error("❌ Data fetch fail: %s", exc)
            self.data_map, self.data_map_1m = {}, {}

    # ============================================================
    # MARKET OPEN (09:15) — ready signal
    # ============================================================
    def market_open(self):
        """Market open — Tiger ready for intraday scanning."""
        logger.info("🐅 MARKET OPEN — Tiger ready for intraday scanning.")
        if self.broker is None or not self.broker.is_session_valid():
            logger.warning("⚠️ Broker session invalid — pre-market login nahi hua tha.")
            self.pre_market_wake()

    # ============================================================
    # INTRADAY SCAN (every 20 min) — entry signals + exits
    # ============================================================
    def intraday_scan(self):
        """Har 20 min pe zones scan karo, entry signal dhoondo, exit check karo."""
        if get_day_mode() != "TRADING":
            return
        if not is_market_hours():
            logger.info("Intraday scan: market band hai, skip.")
            return
        if is_opening_range_period():
            logger.info("Intraday scan: opening range period, skip (15 min wait).")
            return
        if self.broker is None:
            logger.warning("Intraday scan: broker nahi hai, skip.")
            return

        logger.info("🐅 INTRADAY SCAN — %s", datetime.now().strftime("%H:%M"))
        try:
            from backtest.run_tiger_brain_backtest import run_tiger_brain_backtest
            combined = run_tiger_brain_backtest(
                self.data_map, start_capital=150000.0,
                data_map_1m=self.data_map_1m if self.data_map_1m else None,
                broker=self.broker)
            trades = combined.get("trades", [])
            totals = combined.get("totals", {})
            logger.info("Scan done: %d trades today, equity ₹%.0f, return %.2f%%",
                        len(trades),
                        totals.get("final_equity", 0),
                        totals.get("total_return_pct", 0))
        except Exception as exc:
            logger.error("Intraday scan error: %s", exc)

    # ============================================================
    # MARKET CLOSE (15:30) — square-off + logout
    # ============================================================
    def market_close(self):
        """Market close — sab positions square-off + broker logout."""
        logger.info("=" * 60)
        logger.info("🐅 MARKET CLOSE — Square-off + cleanup")
        logger.info("=" * 60)
        if self.broker is None:
            logger.info("Broker nahi hai — kuch close nahi karna.")
            return
        try:
            closed = self.broker.square_off_all()
            logger.info("✅ Square-off done: %d positions closed.", closed)
        except Exception as exc:
            logger.error("❌ Square-off error: %s", exc)
        try:
            self.broker.logout()
            logger.info("✅ Broker logged out.")
        except Exception as exc:
            logger.warning("Logout warning: %s", exc)

    # ============================================================
    # NIGHTLY REPLAY (00:00) — audit + pattern tracking
    # ============================================================
    def nightly_replay(self):
        """Raat ko aaj ke trades ka audit + pattern tracking."""
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
    # START — wire all jobs + run scheduler
    # ============================================================
    def start(self):
        """Sab trading functions scheduler pe wire karo + 24x7 chalu karo."""
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
        )
        self.scheduler.start()
        self._running = True
        logger.info("✅ Tiger scheduler STARTED. 24x7 cycle active.")
        logger.info("   Pre-market:  09:00")
        logger.info("   Market open: 09:15")
        logger.info("   Intraday:    every 20 min")
        logger.info("   Market close:15:30 (square-off 15:15 NSE / 23:15 MCX)")
        logger.info("   Nightly:     00:00")
        logger.info("")
        logger.info("🐅 Tiger live hai. Ctrl+C pe shutdown hoga.")

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
