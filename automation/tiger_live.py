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

import pandas as pd

logger = logging.getLogger("tiger_brain.tiger_live")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from broker.angel_connect import AngelBroker, AngelConnectionError
from automation.scheduler import (
    TigerBrainScheduler, get_day_mode, is_market_hours, is_opening_range_period,
    is_mcx_hours,
)
from data.loader import resolve_option_contract
from config.thresholds import AUTOMATION, MARKET_CATEGORIES


def resolve_exchange_for_symbol(symbol: str) -> str:
    """Symbol se exchange guess karo (MCX commodity vs NFO equity/index)."""
    if not symbol:
        return "NFO"
    upper = symbol.upper()
    mcx_commodities = {"CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI",
                       "GOLD", "GOLDM", "SILVER", "SILVERM", "COPPER", "ZINC"}
    if upper in mcx_commodities:
        return "MCX"
    return "NFO"


class TigerLiveRunner:
    """Poora live trading cycle manage karta hai — broker + scheduler + data."""

    # Persist placed order keys to disk — restart pe duplicates nahi honge
    _ORDER_KEYS_FILE = "/tmp/tiger_placed_orders.json"

    def __init__(self):
        self.broker: AngelBroker | None = None
        self.scheduler: TigerBrainScheduler | None = None
        self.data_map: dict = {}
        self.data_map_1m: dict = {}
        self.instrument_master = None
        self._running = False
        # Track placed order keys — disk se load, restart pe safe
        self._placed_order_keys: set[str] = self._load_order_keys()
        self._order_log: list[dict] = []
        # Real account capital — Angel One se fetch hota hai pre-market
        self.account_capital: float = 0.0
        # Capital lifecycle: start → after_entry → after_exit
        self.capital_start: float = 0.0
        self.capital_after_entry: float = 0.0
        self.capital_after_exit: float = 0.0

    def _load_order_keys(self) -> set[str]:
        """Disk se placed order keys load karo (restart-safe)."""
        import json
        try:
            with open(self._ORDER_KEYS_FILE) as f:
                return set(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return set()

    def _save_order_keys(self):
        """Placed order keys disk pe save karo."""
        import json
        try:
            with open(self._ORDER_KEYS_FILE, "w") as f:
                json.dump(sorted(self._placed_order_keys), f)
        except OSError as exc:
            logger.warning(f"Order keys save fail: {exc}")

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

        # 2b. REAL account balance — Angel One se fetch
        try:
            self.account_capital = self.broker.get_balance()
            if self.account_capital <= 0:
                logger.warning("⚠️ Balance ₹0 — rmsLimit() fail. "
                               "Fallback ₹10,000.")
                self.account_capital = 10000.0
            self.capital_start = self.account_capital
            self.capital_after_entry = self.account_capital
            self.capital_after_exit = self.account_capital
            logger.info("💰 Trading capital: ₹%.0f (100%% of Angel One balance)",
                        self.account_capital)
            logger.info("💰 Capital lifecycle START: ₹%.0f", self.capital_start)
        except Exception as exc:
            logger.error("❌ Balance fetch fail: %s — fallback ₹10,000", exc)
            self.account_capital = 10000.0

        # 3. Fetch fresh data
        try:
            from backtest.run_tiger_brain_backtest import fetch_angel_data
            self.data_map, self.data_map_1m, failed = fetch_angel_data(
                self.broker, days_15m=30, days_1m=7, use_scan_universe=True)
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
        """Har 20 min pe zones scan karo, entry signal dhoondo, exit check karo.

        Backtest engine strategy signals generate karta hai. Phir _place_live_orders()
        un signals ko REAL Angel One orders mein convert karta hai.
        """
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
        logger.info("   ℹ️ Zone data: MCX symbols use US futures proxy (CL=F/GC=F/SI=F/NG=F), not live MCX chain — 003a9ff original design.")
        try:
            from backtest.run_tiger_brain_backtest import run_tiger_brain_backtest
            capital = self.account_capital if self.account_capital > 0 else 10000.0
            combined = run_tiger_brain_backtest(
                self.data_map, start_capital=capital,
                data_map_1m=self.data_map_1m if self.data_map_1m else None,
                broker=self.broker)
            trades = combined.get("trades", [])
            totals = combined.get("totals", {})

            # === LIVE EXECUTION BRIDGE ===
            # Backtest ne signals generate kiye. Ab unhe REAL orders mein
            # convert karo — sirf aaj ke + abhi tak place na hue trades.
            # Exits (PREMIUM EXIT / stop-loss / square-off) → SELL orders.
            exits = [t for t in trades if t.get("exit_ts") is not None]
            entries = [t for t in trades if t.get("exit_ts") is None]
            placed = self._place_live_orders(entries)
            closed = self._place_exit_orders(exits)

            logger.info("Scan done: %d signals (%d entries, %d exits), "
                        "%d buy orders placed, %d sell orders placed, "
                        "equity ₹%.0f, return %.2f%%",
                        len(trades), len(entries), len(exits),
                        placed, closed,
                        totals.get("final_equity", 0),
                        totals.get("total_return_pct", 0))
        except Exception as exc:
            import traceback as _tb
            logger.error("Intraday scan error: %s\n%s", exc, _tb.format_exc())

    def _place_live_orders(self, trades: list[dict]) -> int:
        """Backtest signals → REAL Angel One orders.

        Har trade se pehle Tiger khud ye check karta hai:
          1. Real Angel One balance fetch (₹)
          2. Option contract ka real lot_size (instrument master se)
          3. REAL market LTP fetch (ltpData API se — NOT simulated premium)
          4. Real trade cost = quantity × real_ltp
          5. Affordable? real_cost ≤ available balance
             → YES: order place karo
             → NO:  skip (1 lot bhi fit nahi hua to)
          6. Order place karne ke baad STATUS check (rejected to nahi?)
          7. Full capital lifecycle log: start → after_entry → remaining

        Returns:
            int: kitne real orders successfully placed + accepted
        """
        if not trades or self.broker is None:
            return 0

        today = datetime.now().date()
        placed_count = 0
        now = datetime.now()

        # Step 1: Real balance fetch (har scan pe fresh)
        try:
            available_balance = self.broker.get_balance()
        except Exception:
            available_balance = self.account_capital
        if available_balance <= 0:
            logger.warning("⚠️ Balance ₹0 — koi order nahi place hoga.")
            return 0

        # Capital lifecycle
        if self.capital_after_exit > 0:
            available_balance = max(available_balance, self.capital_after_exit)

        # 3 PM cutoff check — 3:00 PM ke baad NO new NSE/equity intraday
        # entries. MCX commodity entries EXEMPT (evening session 17:00-23:30).
        cutoff_str = AUTOMATION.get("INTRADAY_ENTRY_CUTOFF_TIME", "15:00")
        cutoff_h, cutoff_m = map(int, cutoff_str.split(":"))
        intraday_cutoff = now.replace(hour=cutoff_h, minute=cutoff_m,
                                      second=0, microsecond=0)
        is_after_cutoff = now >= intraday_cutoff

        if is_after_cutoff:
            logger.info("=" * 60)
            logger.info("⏰ 3 PM CUTOFF — NSE intraday bandh. "
                        "MCX commodity entries EXEMPT (evening session). "
                        "Sirf profit booking (exits) for NSE.")
            logger.info("💰 Available balance: ₹%.0f", available_balance)
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
            quantity = t.get("quantity", 0)
            is_delivery = t.get("is_delivery", False)

            # 3 PM cutoff: NSE/equity intraday bandh after 3PM.
            # MCX commodity EXEMPT — evening session (17:00-23:30) entries allowed.
            is_mcx_commodity = MARKET_CATEGORIES.get(
                resolve_exchange_for_symbol(symbol), "") == "commodity"
            if is_after_cutoff and not is_delivery and not is_mcx_commodity:
                logger.info(
                    f"   ⏰ SKIP {symbol} {strike}{option_type} — "
                    f"NSE intraday bandh after 3PM, sirf profit booking")
                continue

            # Duplicate check
            order_key = f"{symbol}_{strike}_{option_type}_{trade_date}"
            if order_key in self._placed_order_keys:
                continue

            # Step 2: Resolve contract with real lot_size
            contract = resolve_option_contract(symbol, strike, option_type)
            if contract is None:
                logger.warning(
                    f"⚠️ Order skip: {symbol} {strike}{option_type} token nahi mila")
                continue

            real_lot_size = contract.get("lotsize", 1)
            if real_lot_size <= 0:
                real_lot_size = 1

            # Step 3: REAL LTP fetch (NOT simulated premium!)
            sim_premium = t.get("entry_premium", 0.0)
            real_ltp = self.broker.get_ltp(
                contract["tradingsymbol"],
                contract["symboltoken"],
                contract["exchange"],
            )
            # Agar LTP fetch fail, simulated se fallback (with warning)
            if real_ltp <= 0:
                logger.warning(
                    f"   ⚠️ LTP fetch fail — simulated premium ₹{sim_premium:.2f} use")
                real_ltp = sim_premium if sim_premium > 0 else 0.5

            # Step 4: Real trade cost with REAL market price
            trade_cost = quantity * real_ltp
            one_lot_cost = real_lot_size * real_ltp

            # MCX MINI fallback — agar full-size MCX contract afford nahi
            # hota, to MINI variant try karo (chhota lot = kam capital).
            from data.loader import MCX_MINI_FALLBACK
            if (one_lot_cost > available_balance
                    and symbol in MCX_MINI_FALLBACK):
                mini_symbol = MCX_MINI_FALLBACK[symbol]
                mini_contract = resolve_option_contract(
                    mini_symbol, strike, option_type)
                if mini_contract is not None:
                    mini_lot = mini_contract.get("lotsize", 1) or 1
                    mini_ltp = self.broker.get_ltp(
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
                        trade_cost = quantity * real_ltp
                        one_lot_cost = mini_one_lot

            logger.info("-" * 60)
            logger.info(f"📊 {symbol} {strike}{option_type} ({direction})")
            logger.info(f"   Lot Size:      {real_lot_size}")
            logger.info(f"   Sim Premium:   ₹{sim_premium:.2f} (backtest)")
            logger.info(f"   REAL LTP:      ₹{real_ltp:.2f} (market)")
            logger.info(f"   Quantity:      {quantity} ({quantity // real_lot_size} lots)")
            logger.info(f"   Real Cost:     ₹{trade_cost:,.0f} ({quantity} × ₹{real_ltp:.2f})")
            logger.info(f"   1 Lot Cost:    ₹{one_lot_cost:,.0f}")
            logger.info(f"   Balance:       ₹{available_balance:,.0f}")

            # Step 5: Affordability check against REAL cost + REAL balance
            if one_lot_cost > available_balance:
                logger.info(
                    f"   ❌ SKIP — 1 lot (₹{one_lot_cost:,.0f}) > balance "
                    f"(₹{available_balance:,.0f}) — afford nahi hota")
                self._order_log.append({
                    "time": datetime.now().isoformat(),
                    "symbol": symbol, "strike": strike,
                    "option_type": option_type,
                    "tradingsymbol": contract["tradingsymbol"],
                    "real_ltp": real_ltp, "one_lot_cost": one_lot_cost,
                    "balance": available_balance,
                    "success": False, "error": "not affordable — 1 lot > balance",
                })
                continue

            if trade_cost > available_balance:
                # Reduce lots to fit
                affordable_lots = int(available_balance // one_lot_cost)
                if affordable_lots >= 1:
                    quantity = affordable_lots * real_lot_size
                    trade_cost = quantity * real_ltp
                    logger.info(
                        f"   ⚠️ Reduced to {affordable_lots} lots = {quantity} qty "
                        f"= ₹{trade_cost:,.0f} (fit balance)")
                else:
                    logger.info(f"   ❌ SKIP — can't fit any lot in balance")
                    continue

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
                # Step 7: Check order STATUS — rejected to nahi?
                import time as _time
                _time.sleep(2)  # RMS ko process karne do
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
                self._save_order_keys()  # disk pe save — restart-safe
                available_balance -= trade_cost
                self.capital_after_entry = available_balance
                logger.info(
                    f"   ✅ Order accepted: {order_status}")
                logger.info(
                    f"   🔥 REAL ORDER: BUY {quantity} "
                    f"{contract['tradingsymbol']} ({option_type}) "
                    f"cost ₹{trade_cost:,.0f} → order_id={result['order_id']}")
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

        Backtest ne PREMIUM EXIT / stop-loss / square-off signal diya.
        Ab real broker se open position dhundh ke SELL order place karo.

        Returns:
            int: kitne positions successfully closed
        """
        if not exit_trades or self.broker is None:
            return 0

        # Real broker positions fetch (kya actually hold kar rahe hain)
        try:
            open_positions = self.broker.get_positions()
        except Exception as exc:
            logger.error("❌ Exit orders: position fetch fail: %s", exc)
            return 0

        # Angel One sometimes returns None instead of [] — guard against it.
        if open_positions is None:
            logger.info("📤 Broker returned no positions (None) — skip sell orders.")
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
        today = datetime.now().date()

        for t in exit_trades:
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
            exit_reason = t.get("exit_reason", "unknown")

            # Resolve contract to get tradingsymbol
            contract = resolve_option_contract(symbol, strike, option_type)
            if contract is None:
                logger.warning(
                    f"   ⚠️ Exit skip: {symbol} {strike}{option_type} "
                    f"contract nahi mila")
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

            # Place SELL order to close
            result = self.broker.place_option_order(
                tradingsymbol=tsym,
                symboltoken=contract["symboltoken"],
                exchange=contract["exchange"],
                transaction_type="SELL",
                quantity=qty,
                product_type="INTRADAY",
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
        """3:00 PM — Tiger next-day direction decide karke delivery orders.

        Tiger EOD pe market dekh ke decide karta hai:
        - Gup-up likely → BUY CE (call option) delivery
        - Gup-down likely → BUY PE (put option) delivery

        Delivery = CARRYFORWARD (overnight hold), next day square-off.

        3 PM ke baad intraday new orders bandh, sirf ye delivery + exits.
        """
        logger.info("=" * 60)
        logger.info("🐅 DELIVERY SNAPSHOT (3:00 PM) — Next-day direction")
        logger.info("=" * 60)
        if self.broker is None:
            logger.info("Broker nahi hai — delivery skip.")
            return
        try:
            from backtest.run_tiger_brain_backtest import run_tiger_brain_backtest
            combined = run_tiger_brain_backtest(
                self.data_map, start_capital=self.account_capital,
                data_map_1m=self.data_map_1m if self.data_map_1m else None,
                broker=self.broker)
            trades = combined.get("trades", [])
            # Filter sirf delivery trades
            delivery_trades = [t for t in trades
                               if t.get("is_delivery", False)]
            today_str = datetime.now().strftime("%Y-%m-%d")
            delivery_trades = [t for t in delivery_trades
                               if str(t.get("entry_ts", ""))[:10] == today_str]
            logger.info("Delivery signals: %d / %d total trades",
                        len(delivery_trades), len(trades))
            if delivery_trades:
                placed = self._place_live_orders(delivery_trades)
                logger.info("Delivery orders placed: %d", placed)
            else:
                logger.info("Koi delivery signal nahi — aaj overnight nahi.")
        except Exception as exc:
            logger.error("Delivery snapshot error: %s", exc)

    # ============================================================
    # MARKET CLOSE — NSE 15:15 square-off + MCX 23:15 square-off
    # ============================================================
    def nse_square_off(self):
        """NSE/NFO positions close karo (15:15 IST).

        Sirf NFO positions close karta hai — MCX positions open rehte
        hain kyunki MCX 23:30 tak khulta hai.
        """
        logger.info("=" * 60)
        logger.info("🐅 NSE SQUARE-OFF (15:15) — NFO positions close")
        logger.info("=" * 60)
        if self.broker is None:
            logger.info("Broker nahi hai — kuch close nahi karna.")
            return
        try:
            closed = self.broker.square_off_all(exchange="NFO")
            logger.info("✅ NSE square-off: %d positions closed.", closed)
        except Exception as exc:
            logger.error("❌ NSE square-off error: %s", exc)
        # Exit ke baad capital update
        self._log_capital_after_exit("NSE square-off")

    def mcx_square_off(self):
        """MCX positions close karo (23:15 IST).

        Commodity positions 23:15 pe close — MCX 23:30 tak khulta
        hai isliye alag time pe close hota hai.
        """
        logger.info("=" * 60)
        logger.info("🐅 MCX SQUARE-OFF (23:15) — MCX positions close")
        logger.info("=" * 60)
        if self.broker is None:
            logger.info("Broker nahi hai — kuch close nahi karna.")
            return
        try:
            closed = self.broker.square_off_all(exchange="MCX")
            logger.info("✅ MCX square-off: %d positions closed.", closed)
        except Exception as exc:
            logger.error("❌ MCX square-off error: %s", exc)
        # Exit ke baad capital update + logout
        self._log_capital_after_exit("MCX square-off")
        try:
            self.broker.logout()
            logger.info("✅ Broker logged out (end of trading day).")
        except Exception as exc:
            logger.warning("Logout warning: %s", exc)

    def _log_capital_after_exit(self, label: str):
        """Exit ke baad real balance fetch + P&L calculate karo.

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
        """Legacy market close — NSE + MCX sab close + logout.

        15:30 pe NSE positions close + MCX bhi close (fallback).
        23:15 pe alag se MCX-only close bhi scheduled hai.
        """
        logger.info("=" * 60)
        logger.info("🐅 MARKET CLOSE (15:30) — Square-off + cleanup")
        logger.info("=" * 60)
        if self.broker is None:
            logger.info("Broker nahi hai — kuch close nahi karna.")
            return
        try:
            closed = self.broker.square_off_all()
            logger.info("✅ Square-off done: %d positions closed.", closed)
        except Exception as exc:
            logger.error("❌ Square-off error: %s", exc)
        self._log_capital_after_exit("Market close 15:30")
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

        # DUTY LOCK — override schedule values at runtime (config/thresholds.py untouched).
        # 09:15 wakeup, every 2 min scan, NSE square-off 15:30, MCX 15:30->23:30.
        AUTOMATION["MARKET_OPEN_TIME"] = "09:15"
        AUTOMATION["PRE_MARKET_WAKE_TIME"] = "09:15"
        AUTOMATION["RESCAN_INTERVAL_MINUTES"] = 2
        AUTOMATION["NSE_SQUARE_OFF_TIME"] = "15:30"
        AUTOMATION["MCX_OPEN_TIME"] = "15:30"
        AUTOMATION["MCX_SQUARE_OFF_TIME"] = "23:30"
        AUTOMATION["MCX_CLOSE_TIME"] = "23:30"

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
        )
        self.scheduler.start()
        self._running = True
        logger.info("✅ Tiger scheduler STARTED. 24x7 cycle active.")
        logger.info("   Wakeup:      09:15")
        logger.info("   Intraday:    every 2 min (NSE 09:15-15:30, MCX 15:30-23:30)")
        logger.info("   NSE square-off: 15:30")
        logger.info("   MCX square-off: 23:30")
        logger.info("   Nightly:     00:00")
        logger.info("")
        logger.info("🐅 Tiger live hai. Ctrl+C pe shutdown hoga.")

        # Mid-market startup: agar market pehle se open hai, turant login karo
        if get_day_mode() == "TRADING" and is_market_hours():
            logger.info("🐅 Market pehle se open hai — turant broker login + scan start.")
            self.pre_market_wake()
            self.market_open()

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
