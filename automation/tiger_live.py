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
from data.loader import resolve_option_contract, OPTION_INSTRUMENT_TYPE
from config.thresholds import AUTOMATION, MARKET_CATEGORIES
from backtest.tiger_fund_brain import announce_fund_plan, size_trade_with_fund_brain


def resolve_exchange_for_symbol(symbol: str) -> str:
    """Symbol se exchange resolve karo (MCX commodity vs NFO equity/index).

    OPTION_INSTRUMENT_TYPE (data/loader.py) se authoritative lookup —
    hardcoded set nahi, taaki naye MCX commodities (ALUMINIUM, MENTHAOIL,
    etc) automatically detect ho jayein.
    """
    if not symbol:
        return "NFO"
    upper = symbol.upper()
    # Authoritative: OPTION_INSTRUMENT_TYPE has exchange for every symbol
    if upper in OPTION_INSTRUMENT_TYPE:
        return OPTION_INSTRUMENT_TYPE[upper][1]
    # MINI variants strip suffix pe parent symbol check
    for suffix in ("M", "MINI"):
        if upper.endswith(suffix):
            parent = upper[:-len(suffix)]
            if parent in OPTION_INSTRUMENT_TYPE:
                return OPTION_INSTRUMENT_TYPE[parent][1]
    return "NFO"


class TigerLiveRunner:
    """Poora live trading cycle manage karta hai — broker + scheduler + data."""

    # Persist placed order keys to disk — restart pe duplicates nahi honge
    _ORDER_KEYS_FILE = "/tmp/tiger_placed_orders.json"
    # Persist per-position peak + target_booked — Tiger ki eyes restart pe bhi open
    _POSITION_TRACK_FILE = "/tmp/tiger_position_peaks.json"

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
        # Open position tracker — Tiger ki eyes hamesha broker positions pe
        # {tsym: {"peak": float, "target_booked": bool, "entry": float}}
        self._position_peaks: dict = self._load_position_peaks()

    def _live_re_size(
        self, real_balance: float, real_ltp: float, real_lot_size: int,
        is_delivery: bool, current_exposure: float = 0.0,
    ) -> dict:
        """REAL balance + REAL LTP + REAL lot size se quantity nikaalo.

        Backtest simulated premium se quantity nikaalta tha — woh GALAT
        ho sakta hai. Live order pe FUND BRAIN se actual capital ke
        hisaab se re-size karte hain. Quantity hamesha lot size ke
        multiple mein hoti hai (Angel One reject nahi karega).

        Returns:
            dict: {quantity, lots, allocated_capital, reason}
            quantity=0 means SKIP (afford nahi hota ya fund plan fail)
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

    def _load_position_peaks(self) -> dict:
        """Disk se per-position peak + target_booked load karo (restart-safe).

        Tiger restart hone pe bhi open positions ka peak yaad rahe —
        trail locking break nahi hogi.
        """
        import json
        try:
            with open(self._POSITION_TRACK_FILE) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _save_position_peaks(self):
        """Per-position peak + target_booked disk pe save karo."""
        import json
        try:
            with open(self._POSITION_TRACK_FILE, "w") as f:
                json.dump(self._position_peaks, f)
        except OSError as exc:
            logger.warning(f"Position peaks save fail: {exc}")

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
        # ❗ ₹10,000 fallback HATA DIYA. Balance nahi mila to account_capital=0,
        # backtest simulation ₹10,000 se chalegi (scan ke liye) LEKIN real
        # orders place nahi honge (_place_live_orders me get_balance() fail = return 0).
        try:
            self.account_capital = self.broker.get_balance()
            if self.account_capital <= 0:
                logger.warning("⚠️ Balance ₹0 — rmsLimit() fail. "
                               "Real orders BANDH. Scan simulation ₹10,000 se.")
                self.account_capital = 0.0
            self.capital_start = self.account_capital
            self.capital_after_entry = self.account_capital
            self.capital_after_exit = self.account_capital
            logger.info("💰 Trading capital: ₹%.0f (100%% of Angel One balance)",
                        self.account_capital)
            logger.info("💰 Capital lifecycle START: ₹%.0f", self.capital_start)
        except Exception as exc:
            logger.error("❌ Balance fetch fail: %s — real orders BANDH.", exc)
            self.account_capital = 0.0

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
        # Tiger ki eyes turant open positions pe — kal ka trade bhool naye
        self.monitor_open_positions()

    # ============================================================
    # POSITION MONITOR — Tiger ki eyes hamesha open positions pe
    # ============================================================
    def monitor_open_positions(self) -> int:
        """Real broker positions + LTP se V19 exit logic seedha apply karo.

        Tiger restart hone pe bhi broker se open positions fetch karke
        unka profit/loss track karta hai. Backtest engine se INDEPENDENT —
        agar backtest position track nahi kar raha (restart bhoola), tab bhi
        Tiger real broker data se exit decision leta hai.

        Har open position pe:
          1. Real LTP fetch (Angel One ltpData)
          2. gain_pct = (ltp - entry) / entry * 100
          3. Stop-loss: ltp ≤ entry × 0.60 → EXIT
          4. Trail (active at +5%): ltp ≤ peak_lock → EXIT
          5. Fixed target (+50%): book 40% quantity
          6. Runaway safety (+250%): full exit
          7. Peak update + disk save (restart-safe)

        Returns:
            int: kitne exit orders place kiye
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

            # Real LTP — broker se fresh
            ltp = self.broker.get_ltp(tsym, token, exch)
            if ltp <= 0:
                ltp = float(p.get("ltp", 0) or 0)
            if ltp <= 0:
                logger.warning(f"👁️ {tsym}: LTP nahi mila — skip.")
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
                f"{'[target_booked]' if target_booked else ''}")

            # === V19 EXIT LOGIC (real broker data pe) ===
            exit_reason = None
            exit_qty = qty

            # 1. Stop-loss (60% of entry)
            stop_premium = entry_price * 0.60
            if ltp <= stop_premium:
                exit_reason = "stop_loss_60pct"

            # 2. Trail (active at +5%) — peak se 30% give back pe exit
            elif gain_pct >= V19_TRAIL_ACTIVATE_PCT:
                peak_gain = (peak - entry_price) / entry_price
                trail_floor = entry_price * (1 + peak_gain * V19_TRAIL_LOCK_PCT / 100)
                if ltp <= trail_floor:
                    exit_reason = "trail_lock_70pct"

            # 3. Fixed target — +50% pe 40% quantity book (first time only)
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
            # Agar position CARRYFORWARD (delivery) pe khuli thi, to exit
            # bhi CARRYFORWARD hona chahiye — INTRADAY exit Angel reject
            # karega (product type mismatch).
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

        # Cleanup: broker pe closed positions tracker se hatao
        for tsym in list(self._position_peaks.keys()):
            if tsym not in active_tsyms:
                del self._position_peaks[tsym]
                logger.info(f"👁️ {tsym}: position closed — tracker cleanup.")

        self._save_position_peaks()
        if closed:
            logger.info(f"👁️ Position monitor: {closed} exit orders placed.")
        return closed

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

        # === TIGER KI EYES — pehle open positions monitor karo ===
        # Broker se real LTP fetch + V19 exit logic. Backtest se INDEPENDENT.
        # Kal ka trade bhool na jaye — har scan pe positions check.
        try:
            monitored = self.monitor_open_positions()
        except Exception as exc:
            logger.error("👁️ Position monitor error: %s", exc)
            monitored = 0

        try:
            from backtest.run_tiger_brain_backtest import run_tiger_brain_backtest
            # Real balance → backtest simulation capital. Agar balance 0
            # (fetch fail) to simulation ₹10,000 se chalegi — LEKIN real
            # orders _place_live_orders me get_balance() se check honge.
            capital = self.account_capital if self.account_capital > 0 else 10000.0
            if self.account_capital <= 0:
                logger.warning("⚠️ Simulation ₹10,000 se — real balance nahi mila. "
                               "Real orders BANDH (capital check fail).")
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
                        "%d monitored exits, "
                        "equity ₹%.0f, return %.2f%%",
                        len(trades), len(entries), len(exits),
                        placed, closed, monitored,
                        totals.get("final_equity", 0),
                        totals.get("total_return_pct", 0))
        except Exception as exc:
            logger.error("Intraday scan error: %s", exc)

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
        # ❗ FAIL = NO orders. ₹10,000 fallback HATA DIYA — Tiger galat
        # balance se order place na kare. Balance nahi mila to band.
        # 2 retries (get_balance internal + yahan se): transient fail
        # (rate limit, session expire) handle, genuine fail = no orders.
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
                logger.warning("⚠️ Balance 0 — 2s ruko, retry...")
                time.sleep(2)
        if available_balance <= 0:
            logger.error("❌ Balance fetch 2 retries mein FAIL — koi order nahi.")
            return 0

        # Capital lifecycle
        if self.capital_after_exit > 0:
            available_balance = max(available_balance, self.capital_after_exit)

        # NSE hard cutoff — 15:30 (MARKET_CLOSE_TIME) ke baad NSE ka KOI
        # order nahi (intraday ya delivery — kuch bhi nahi). Sirf MCX
        # commodity allowed (MCX 09:00-23:30 chalta hai).
        # 15:00-15:30 beech: NSE delivery allowed, intraday bandh (strategy).
        nse_close_str = AUTOMATION.get("MARKET_CLOSE_TIME", "15:30")
        nse_close_h, nse_close_m = map(int, nse_close_str.split(":"))
        nse_hard_cutoff = now.replace(
            hour=nse_close_h, minute=nse_close_m, second=0, microsecond=0)
        is_nse_closed = now >= nse_hard_cutoff

        # Intraday strategy cutoff — 15:00 ke baad naya intraday bandh,
        # sirf delivery + MCX.
        cutoff_str = AUTOMATION.get("INTRADAY_ENTRY_CUTOFF_TIME", "15:00")
        cutoff_h, cutoff_m = map(int, cutoff_str.split(":"))
        intraday_cutoff = now.replace(hour=cutoff_h, minute=cutoff_m,
                                      second=0, microsecond=0)
        is_after_intraday_cutoff = now >= intraday_cutoff

        if is_nse_closed:
            logger.info("=" * 60)
            logger.info("🌙 NSE BANDH (15:30) — sirf MCX commodity allowed. "
                        "NSE ka koi order nahi (intraday/delivery dono bandh).")
            logger.info("💰 Available balance: ₹%.0f", available_balance)
            logger.info("=" * 60)
        elif is_after_intraday_cutoff:
            logger.info("=" * 60)
            logger.info("⏰ 3 PM CUTOFF — NSE intraday bandh. "
                        "Sirf NSE delivery + MCX commodity allowed.")
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
            # NOTE: backtest ki quantity IGNORE — Fund Brain se REAL
            # balance + REAL LTP + REAL lot size se re-size hota hai
            is_delivery = t.get("is_delivery", False)

            # NSE hard cutoff: 15:30 ke baad NSE ka KOI order nahi
            # (intraday ya delivery — dono bandh). Sirf MCX commodity.
            # 15:00-15:30: NSE delivery allowed, intraday bandh.
            is_mcx_commodity = MARKET_CATEGORIES.get(
                resolve_exchange_for_symbol(symbol), "") == "commodity"
            if is_nse_closed and not is_mcx_commodity:
                logger.info(
                    f"   🌙 SKIP {symbol} {strike}{option_type} — "
                    f"NSE bandh (15:30), sirf MCX commodity allowed")
                continue
            if is_after_intraday_cutoff and not is_delivery and not is_mcx_commodity:
                logger.info(
                    f"   ⏰ SKIP {symbol} {strike}{option_type} — "
                    f"NSE intraday bandh after 3PM, sirf delivery/MCX")
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

            # Step 4: REAL re-size with FUND BRAIN — not backtest's qty!
            # Backtest ne simulated premium se qty nikaali thi — woh GALAT
            # ho sakti hai. Ab REAL balance + REAL LTP + REAL lot size se
            # Fund Brain se proper sizing karte hain. Quantity hamesha
            # lot size ke multiple mein hoti hai (P2 fix).
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

            one_lot_cost = real_lot_size * real_ltp

            # Current exposure: total deployed capital in open positions
            current_exposure = sum(
                float(o.get("trade_cost", 0)) for o in self._order_log
                if o.get("success")
            )

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

            # Product type MUST match the entry order's product type.
            # Delivery (CARRYFORWARD) positions can be from a previous day —
            # exit bhi CARRYFORWARD hona chahiye, INTRADAY se Angel reject karega.
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
        logger.info("   Pre-market:  09:00")
        logger.info("   Market open: 09:15")
        logger.info("   Intraday:    every 20 min (entry bandh 3PM)")
        logger.info("   Delivery:    15:00 (overnight direction)")
        logger.info("   NSE close:   15:15 (NFO square-off)")
        logger.info("   Market close:15:30 (fallback square-off)")
        logger.info("   MCX close:   23:15 (MCX square-off + logout)")
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
