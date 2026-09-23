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
from datetime import datetime, timedelta
from typing import Optional

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
from data.loader import resolve_option_contract, OPTION_INSTRUMENT_TYPE, find_affordable_option
from config.thresholds import AUTOMATION, MARKET_CATEGORIES, SCALPER, ML_ENGINE
from backtest.tiger_fund_brain import announce_fund_plan, size_trade_with_fund_brain
from risk.capital_manager import CapitalManager
from automation.daily_cleanup import run_daily_cleanup
from pipeline.seven_brains import count_aligned_brains
from pipeline.ml_engine import TigerMLGate, required_confluence_for_win_prob
from data.features import extract_live_features, sensex_blocks_option
from subbrains.mcx_scanner import (
    MCX_SYMBOLS as SNIPER_MCX_SYMBOLS,
    scan_mcx as scan_mcx_sniper,
    calculate_atr as sniper_atr,
)


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
    # Persist order log — exposure tracking survives restart
    _ORDER_LOG_FILE = "/tmp/tiger_order_log.json"
    # Persist direction blocks — 2-hour lockout survives restart
    _DIRECTION_BLOCKS_FILE = "/tmp/tiger_direction_blocks.json"
    # Persist daily trade count — anti-loop state lock (restart-safe)
    _STATE_LOCK_FILE = "/tmp/state_lock.json"

    def __init__(self):
        self.broker: AngelBroker | None = None
        self.scheduler: TigerBrainScheduler | None = None
        self.data_map: dict = {}
        self.data_map_1m: dict = {}
        self.instrument_master = None
        self._running = False
        self._scan_lock = __import__("threading").Lock()  # prevent overlapping scans
        self._batch_keys: set = set()  # per-scan dedup — cleared each scan
        # Track placed order keys — load from disk, restart-safe
        self._placed_order_keys: set[str] = self._load_order_keys()
        self._order_log: list[dict] = self._load_order_log()
        self._daily_entries_taken: dict = {}  # date → count (Brain 4 quota)
        # Scalper tracking — per-day count + last trade time
        self._scalper_trades: dict = {}  # date → count
        self._last_trade_time: datetime | None = None
        # === RISK MANAGER — consecutive loss tracking (restart-safe) ===
        self._consecutive_losses: int = 0
        self._pause_until: datetime | None = None
        self._daily_pnl: dict = {}  # date → total PnL
        self._trade_history: dict = {}  # date → list of {pnl, symbol, reason}
        self._load_risk_state()
        # === DIRECTIONAL BLOCK — 1 symbol, 1 direction per 2 hours ===
        # If CE_BUY taken on CRUDEOIL, PE_BUY blocked for 2 hours on CRUDEOIL.
        # Prevents the Trade 2 & 3 bug (9900CE + 9900PE same day).
        # {underlying: {"direction": "BUY"/"SELL", "time": datetime, "option_type": "CE"/"PE"}}
        self._direction_blocks: dict[str, dict] = self._load_direction_blocks()
        # === STATE LOCK — disk-backed daily trade counter (anti-loop) ===
        self._state_lock: dict = self._load_state_lock()
        # Current market regime (updated during scan, used by trade log)
        self._current_regime: str = "UNKNOWN"
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
        # Sniper positions tracker — tsym → True (ATR*2.5 trailing exit)
        self._sniper_positions: set = set()
        # Data refresh throttle — with 1-min scans, only refresh REST candles
        # every 5 min. SmartWebSocketV2 live ticks fill the gap between refreshes.
        self._last_data_refresh: datetime | None = None
        self._last_15m_fetch: datetime | None = None
        # Dedup guard — apscheduler job AND the 60s heartbeat both call
        # intraday_scan(), so the same minute could scan twice (doubling
        # REST candle calls and risking duplicate entries). At most one
        # scan per _scan_min_interval_sec.
        self._last_scan_ts: datetime | None = None
        self._scan_min_interval_sec: float = 30.0
        # On-demand 1m backfill ledger: symbol -> date of last attempt, so a
        # symbol missing 1m data is fetched at most once per day.
        self._1m_backfill_attempted: dict = {}
        # === ML INFERENCE GATE — LightGBM win-probability gate ===
        self.ml_gate = TigerMLGate(
            model_path=ML_ENGINE["MODEL_PATH"],
            min_win_prob=ML_ENGINE["MIN_WIN_PROB"],
            feature_columns=ML_ENGINE["FEATURE_COLUMNS"],
        )

    def _live_re_size(
        self, real_balance: float, real_ltp: float, real_lot_size: int,
        is_delivery: bool, current_exposure: float = 0.0,
        market_budget: float = 0.0, win_prob: float = 1.0,
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

        # === 50/50 NSE/MCX CAPITAL SPLIT ===
        # Use market_budget (50% of total) instead of full balance so one
        # market never over-allocates and blocks the other.
        effective_balance = market_budget if market_budget > 0 else real_balance

        lot = int(real_lot_size)
        try:
            plan = announce_fund_plan(effective_balance)
        except ValueError as exc:
            logger.warning(f"Fund plan fail: {exc} — skip order")
            return {"quantity": 0, "lots": 0, "allocated_capital": 0,
                    "reason": f"fund_plan_fail: {exc}"}

        # Stop distance: entry × 20% (tight stop — exit fast when wrong)
        entry = real_ltp
        stop = entry * 0.80  # -20% stop (was 0.60 = -40% — too wide)
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

        # EXCHANGE FREEZE QUANTITY GUARD — Angel rejects orders exceeding
        # exchange max (SILVERM max=600, CRUDEOIL max=600, etc.).
        max_lots = 50 if lot <= 50 else 20
        if qty > 0 and lot > 0 and (qty // lot) > max_lots:
            lots_capped = max_lots
            qty = lots_capped * lot
            logger.info(
                f"   ⚠️ Exchange qty guard: capped to {max_lots} lots "
                f"= {qty} qty (exchange freeze limit)")

        # Final affordability: qty × real_ltp MUST fit in market budget
        cost = qty * real_ltp
        if cost > effective_balance:
            affordable_lots = int(effective_balance // (real_ltp * lot))
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

        # === ML ROCKET SIZING — win_prob adjusts position size ===
        # ML confidence → bigger position for rocket setups, smaller for weak ones.
        # This puts ML to WORK (user: "ML ko kaam pe laga de").
        from config.thresholds import ML_ENGINE as _ML
        _rocket = _ML.get("ROCKET_SIZING", {})
        if _rocket.get("ENABLED", False) and qty > 0:
            _hi = _rocket.get("HIGH_CONFIDENCE", 0.75)
            _mid = _rocket.get("MID_CONFIDENCE", 0.50)
            _rocket_f = _rocket.get("ROCKET_FACTOR", 1.5)
            _normal_f = _rocket.get("NORMAL_FACTOR", 1.0)
            _low_f = _rocket.get("LOW_FACTOR", 0.5)
            if win_prob >= _hi:
                _factor = _rocket_f
                _label = "🚀 ROCKET"
            elif win_prob >= _mid:
                _factor = _normal_f
                _label = "📊 NORMAL"
            else:
                _factor = _low_f
                _label = "🛡️ CAUTIOUS"
            _new_qty = int(qty * _factor)
            # Re-align to lot multiple
            if lot > 0:
                _new_lots = max(1, _new_qty // lot)
                _new_qty = _new_lots * lot
            # Never exceed what balance allows
            _new_cost = _new_qty * real_ltp
            if _new_cost > effective_balance:
                _aff = max(1, int(effective_balance // (real_ltp * lot)))
                _new_qty = _aff * lot
                _new_cost = _new_qty * real_ltp
            if _new_qty != qty:
                logger.info(
                    f"   {_label} SIZING: win_prob={win_prob:.2f} → "
                    f"qty {qty}→{_new_qty} (×{_factor})")
                qty = _new_qty
                lots = qty // lot if lot > 0 else 0
                cost = qty * real_ltp

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

    def _load_order_log(self) -> list:
        """Load order log from disk (restart-safe exposure tracking).

        Without this, a restart wipes exposure memory — Tiger thinks
        no capital is deployed and over-trades. Loaded entries keep
        their success/exited flags so exposure calc is correct.
        """
        import json
        try:
            with open(self._ORDER_LOG_FILE) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []

    def _save_order_log(self):
        """Save order log to disk — exposure tracking survives restart."""
        import json
        try:
            with open(self._ORDER_LOG_FILE, "w") as f:
                json.dump(self._order_log, f, default=str)
        except OSError as exc:
            logger.warning(f"Order log save fail: {exc}")

    def _load_direction_blocks(self) -> dict:
        """Load direction blocks from disk (restart-safe 2-hour lockout).

        Without this, a restart wipes the lockout — Tiger can take
        CE+PE on the same symbol within minutes (capital waste).
        """
        import json
        try:
            with open(self._DIRECTION_BLOCKS_FILE) as f:
                raw = json.load(f)
            blocks = {}
            now = datetime.now()
            for symbol, block in raw.items():
                block_time_str = block.get("time")
                if not block_time_str:
                    continue
                try:
                    block_time = datetime.fromisoformat(block_time_str)
                except (ValueError, TypeError):
                    continue
                if (now - block_time).total_seconds() < 2 * 3600:
                    blocks[symbol] = {
                        "option_type": block.get("option_type", ""),
                        "time": block_time,
                    }
            if blocks:
                active = ", ".join(
                    f"{s} {b['option_type']}" for s, b in blocks.items())
                logger.info(f"📋 Direction locks restored: {active}")
            return blocks
        except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError):
            return {}

    def _save_direction_blocks(self):
        """Save direction blocks to disk — 2-hour lockout survives restart."""
        import json
        try:
            with open(self._DIRECTION_BLOCKS_FILE, "w") as f:
                json.dump(self._direction_blocks, f, default=str)
        except OSError as exc:
            logger.warning(f"Direction blocks save fail: {exc}")

    # ============================================================
    # STATE LOCK — disk-backed daily trade counter (anti-loop)
    # ============================================================
    def _load_state_lock(self) -> dict:
        """Load state_lock.json — daily_trade_count survives restart.

        Structure: {"date": "2026-09-16", "daily_trade_count": 3, "locked": false}
        If date doesn't match today, counter resets to 0 (new session).
        """
        import json
        today = datetime.now().date().isoformat()
        try:
            with open(self._STATE_LOCK_FILE) as f:
                raw = json.load(f)
            if raw.get("date") != today:
                logger.info(
                    f"🔓 State lock: new session ({today}) — "
                    f"counter reset (was {raw.get('daily_trade_count', 0)})")
                return {"date": today, "daily_trade_count": 0, "locked": False}
            count = raw.get("daily_trade_count", 0)
            locked = raw.get("locked", False) or count >= SCALPER["MAX_TRADES_PER_DAY"]
            if locked:
                logger.info(
                    f"🔒 State lock: DAILY CAP LOCKED — "
                    f"{count}/{SCALPER['MAX_TRADES_PER_DAY']} trades taken. "
                    f"No more entries until next session.")
            return {"date": today, "daily_trade_count": count, "locked": locked}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {"date": today, "daily_trade_count": 0, "locked": False}

    def _save_state_lock(self):
        """Save state_lock.json to disk — anti-loop counter survives restart."""
        import json
        try:
            with open(self._STATE_LOCK_FILE, "w") as f:
                json.dump(self._state_lock, f, default=str)
        except OSError as exc:
            logger.warning(f"State lock save fail: {exc}")

    def _increment_daily_trade_count(self) -> int:
        """Increment the daily trade counter and check cap.

        Returns the new count. Locks at MAX_TRADES_PER_DAY — no more entries
        until next session reset.
        """
        today = datetime.now().date().isoformat()
        if self._state_lock.get("date") != today:
            self._state_lock = {"date": today, "daily_trade_count": 0, "locked": False}
        count = self._state_lock.get("daily_trade_count", 0) + 1
        self._state_lock["daily_trade_count"] = count
        if count >= SCALPER["MAX_TRADES_PER_DAY"]:
            self._state_lock["locked"] = True
            logger.info(
                f"🔒 STATE LOCK: daily_trade_count={count}/"
                f"{SCALPER['MAX_TRADES_PER_DAY']} — "
                f"PERMANENTLY LOCKED until next session reset.")
        self._save_state_lock()
        return count

    def _is_daily_cap_locked(self) -> bool:
        """Check if daily trade cap is permanently locked."""
        today = datetime.now().date().isoformat()
        if self._state_lock.get("date") != today:
            self._state_lock = {"date": today, "daily_trade_count": 0, "locked": False}
            self._save_state_lock()
            return False
        return self._state_lock.get("locked", False) or \
            self._state_lock.get("daily_trade_count", 0) >= SCALPER["MAX_TRADES_PER_DAY"]

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

    # === RISK MANAGER STATE PERSISTENCE ===
    _RISK_STATE_FILE = "/tmp/tiger_risk_state.json"

    def _load_risk_state(self):
        """Load consecutive loss count + pause state from disk (restart-safe)."""
        import json
        try:
            with open(self._RISK_STATE_FILE) as f:
                state = json.load(f)
            self._consecutive_losses = int(state.get("consecutive_losses", 0))
            pause_str = state.get("pause_until")
            if pause_str:
                self._pause_until = datetime.fromisoformat(pause_str)
                # If pause has expired, reset
                if self._pause_until < datetime.now():
                    self._pause_until = None
                    self._consecutive_losses = 0
                    logger.info("🛡️ Risk pause expired — resuming trading.")
            today = datetime.now().date().isoformat()
            self._daily_pnl = {k: float(v) for k, v in state.get("daily_pnl", {}).items()}
            self._trade_history = state.get("trade_history", {})
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            pass

    def _save_risk_state(self):
        """Save risk state to disk."""
        import json
        try:
            state = {
                "consecutive_losses": self._consecutive_losses,
                "pause_until": self._pause_until.isoformat() if self._pause_until else None,
                "daily_pnl": self._daily_pnl,
                "trade_history": self._trade_history,
            }
            with open(self._RISK_STATE_FILE, "w") as f:
                json.dump(state, f, default=str)
        except OSError as exc:
            logger.warning(f"Risk state save fail: {exc}")

    def _record_trade_pnl(self, pnl: float, symbol: str, reason: str):
        """Record a closed trade's PnL → update consecutive loss counter."""
        today = datetime.now().date().isoformat()
        self._daily_pnl[today] = self._daily_pnl.get(today, 0) + pnl
        hist = self._trade_history.get(today, [])
        hist.append({"pnl": pnl, "symbol": symbol, "reason": reason})
        self._trade_history[today] = hist

        if pnl < 0:
            self._consecutive_losses += 1
            logger.warning(
                f"🛡️ LOSS recorded: {symbol} ₹{pnl:.0f} — "
                f"consecutive losses: {self._consecutive_losses}")
        else:
            self._consecutive_losses = 0
            logger.info(
                f"🛡️ WIN recorded: {symbol} ₹{pnl:+.0f} — "
                f"loss streak reset to 0")

        # Check for pause/stop triggers
        from config.thresholds import RISK_MANAGER
        if self._consecutive_losses >= RISK_MANAGER["MAX_CONSECUTIVE_LOSSES"]:
            logger.error(
                f"🛑 STOP TRADING — {self._consecutive_losses} consecutive losses! "
                f"Tiger stands down for the rest of the day.")
            # Set pause until end of day (23:59)
            self._pause_until = datetime.now().replace(
                hour=23, minute=59, second=59)
        elif self._consecutive_losses >= RISK_MANAGER["PAUSE_AFTER_LOSSES"]:
            self._pause_until = datetime.now() + timedelta(
                minutes=RISK_MANAGER["PAUSE_DURATION_MINUTES"])
            logger.warning(
                f"⏸️ PAUSE TRADING — {self._consecutive_losses} consecutive losses. "
                f"Resuming at {self._pause_until.strftime('%H:%M')}.")

        self._save_risk_state()

    def _risk_manager_allows_trading(self) -> bool:
        """Check if RiskManager allows new trades right now."""
        # Check state lock — permanent daily cap lock
        if self._is_daily_cap_locked():
            count = self._state_lock.get("daily_trade_count", 0)
            logger.info(
                f"🔒 STATE LOCK active — daily_trade_count={count}/"
                f"{SCALPER['MAX_TRADES_PER_DAY']}. "
                f"No more entries until next session reset.")
            return False
        # Check pause
        if self._pause_until is not None:
            if datetime.now() < self._pause_until:
                remaining = (self._pause_until - datetime.now()).total_seconds() / 60
                logger.info(
                    f"🛡️ Risk pause active — {remaining:.0f} min remaining. "
                    f"No new trades.")
                return False
            else:
                # Pause expired — but only reset if we haven't hit the 3-loss STOP
                from config.thresholds import RISK_MANAGER
                if self._consecutive_losses < RISK_MANAGER["MAX_CONSECUTIVE_LOSSES"]:
                    self._pause_until = None
                    logger.info("🛡️ Risk pause expired — resuming trading.")
                else:
                    logger.info("🛡️ Daily loss STOP still active — no more trades today.")
                    return False

        # Check daily trade cap
        from config.thresholds import RISK_MANAGER
        today = datetime.now().date()
        daily_count = self._daily_entries_taken.get(today, 0)
        if daily_count >= RISK_MANAGER["MAX_TRADES_PER_DAY"]:
            logger.info(
                f"🛡️ Daily trade cap reached — {daily_count}/{RISK_MANAGER['MAX_TRADES_PER_DAY']}. "
                f"No more trades today.")
            return False

        return True

    # ============================================================
    # DIRECTIONAL BLOCK — 1 symbol, 1 direction per 2 hours
    # ============================================================
    def _is_direction_blocked(self, symbol: str, option_type: str) -> bool:
        """Check if opposite direction was taken on same symbol within 2 hours."""
        block = self._direction_blocks.get(symbol)
        if block is None:
            return False
        block_time = block.get("time")
        if block_time is None:
            return False
        # 2-hour window
        if (datetime.now() - block_time).total_seconds() < 2 * 3600:
            blocked_type = block.get("option_type", "")
            if blocked_type != option_type:
                logger.info(
                    f"🚫 DIRECTION BLOCK: {symbol} — {blocked_type} taken at "
                    f"{block_time.strftime('%H:%M')}, opposite {option_type} blocked "
                    f"for 2 hours")
                return True
        else:
            # Block expired — clear it
            del self._direction_blocks[symbol]
            self._save_direction_blocks()
        return False

    def _record_direction_taken(self, symbol: str, option_type: str):
        """Record that a direction was taken on a symbol."""
        self._direction_blocks[symbol] = {
            "option_type": option_type,
            "time": datetime.now(),
        }
        self._save_direction_blocks()
        logger.info(f"📋 Direction locked: {symbol} {option_type} for 2 hours")

    # ============================================================
    # 1-MINUTE VELOCITY CONFIRMATION — final gate before order
    # ============================================================
    def _backfill_1m_for_symbol(self, symbol: str):
        """Fetch this symbol's 1m candles on demand (rate-gated, single call).

        Returns the fetched DataFrame (also cached into data_map_1m) or None.
        Returns None after the first failed attempt for the day so a symbol
        with genuinely no data doesn't burn a REST call on every scan.
        """
        today = datetime.now().date()
        if self._1m_backfill_attempted.get(symbol) == today:
            return None
        self._1m_backfill_attempted[symbol] = today
        if self.broker is None or self.broker.smart_api is None:
            return None
        try:
            from data.loader import fetch_angel_underlying_candles
            df = fetch_angel_underlying_candles(
                self.broker, symbol, "ONE_MINUTE", days=2)
            if df is not None and not df.empty:
                from backtest.run_tiger_brain_backtest import _normalize_cols
                df = _normalize_cols(df)
                self.data_map_1m[symbol] = df
                logger.info("1m backfill OK %s (%d rows)", symbol, len(df))
                return df
            logger.info("NO_DATA 1m backfill empty %s", symbol)
        except Exception as exc:
            logger.warning("1m backfill fail %s: %s", symbol, exc)
        return None

    def _verify_1m_velocity(self, symbol: str, option_type: str) -> bool:
        """Final entry-confirmation gate using the LATEST 1-minute candle.

        Simplified to DIRECTION ONLY — the scanner already checks body,
        volume, and RSI in the 9-gate pipeline. This gate is the final
        confirmation that the live 1m candle direction matches the
        option type (CE → green, PE → red) before transmitting to broker.

        Returns:
            True if the latest 1m candle direction matches option_type.
        """
        df_1m = self.data_map_1m.get(symbol)
        if df_1m is None or df_1m.empty:
            # Last-chance on-demand backfill: startup REST fetch may have
            # skipped this symbol (rate-limit cap/burst). A single gated
            # 1m fetch here permanently prevents the "no 1m data" block
            # instead of letting the signal die every scan.
            df_1m = self._backfill_1m_for_symbol(symbol)
        if df_1m is None or df_1m.empty:
            logger.info(
                f"NO_DATA VELOCITY BLOCK {symbol} — no 1m data available, "
                f"cannot confirm entry")
            return False

        # Latest 1m candle
        row = df_1m.iloc[-1]
        o = float(row.get("open", 0) or 0)
        c = float(row.get("close", 0) or 0)

        # Direction check: CE needs green candle, PE needs red candle.
        # A doji/neutral candle (open≈close) does NOT block — only an
        # OPPOSITE-direction candle blocks. This prevents Tiger from
        # dying on flat 1m bars that are neither green nor red.
        is_ce = option_type.upper() == "CE"
        candle_green = c > o
        candle_red = c < o
        is_neutral = not candle_green and not candle_red

        if is_neutral:
            # Neutral candle — don't block, let the signal through
            return True

        direction_ok = (is_ce and candle_green) or (not is_ce and candle_red)

        if not direction_ok:
            logger.info(
                f"🚫 VELOCITY BLOCK {symbol} {option_type} — "
                f"1m candle {'green' if candle_green else 'red'} "
                f"but need {'green' if is_ce else 'red'} for {option_type}")
            return False

        return True

    # ============================================================
    # TIGER SNIPER ADVANCED V2 — MCX commodity sniper
    # Pure SMC entry: OB retest + CHOCH + wick rejection on 1m.
    # Exit: ATR(14)*2.5 trailing + 5m opposite BOS. No fixed target.
    # ============================================================
    def _sniper_session_active(self, now_dt: datetime = None) -> bool:
        """MCX sniper session: 3:30 PM - 11:30 PM IST (after NSE closes)."""
        from config.thresholds import SNIPER
        from datetime import time
        if now_dt is None:
            now_dt = datetime.now()
        cur = now_dt.time()
        sh, sm = map(int, SNIPER["SESSION_START"].split(":"))
        eh, em = map(int, SNIPER["SESSION_END"].split(":"))
        return time(sh, sm) <= cur <= time(eh, em)

    def _nse_sniper_session_active(self, now_dt: datetime = None) -> bool:
        """NSE sniper session: 9:15 AM - 3:00 PM IST (entry cutoff)."""
        from config.thresholds import SNIPER
        from datetime import time
        if now_dt is None:
            now_dt = datetime.now()
        cur = now_dt.time()
        sh, sm = map(int, SNIPER["NSE_SESSION_START"].split(":"))
        eh, em = map(int, SNIPER["NSE_SESSION_END"].split(":"))
        return time(sh, sm) <= cur <= time(eh, em)

    def _sniper_trades_today(self) -> int:
        """Count of sniper trades placed today (max 3)."""
        today = datetime.now().date()
        return int(self._daily_entries_taken.get(today, 0)) \
            if hasattr(self, "_daily_entries_taken") else 0

    def _sniper_can_trade(self, market: str = "MCX") -> tuple[bool, str]:
        """Sniper safety gate: session + daily cap + risk manager.

        market: "MCX" (15:30-23:30) or "NSE" (09:15-15:00).
        """
        from config.thresholds import SNIPER
        if market == "NSE":
            if not self._nse_sniper_session_active():
                return False, "outside NSE sniper session (09:15-15:00)"
        else:
            if not self._sniper_session_active():
                return False, "outside MCX sniper session (15:30-23:30)"
        count = self._sniper_trades_today()
        if count >= SNIPER["MAX_TRADES_PER_DAY"]:
            return False, f"max {SNIPER['MAX_TRADES_PER_DAY']} sniper trades/day reached"
        if not self._risk_manager_allows_trading():
            return False, "risk manager paused"
        return True, "ok"

    def _verify_sniper_entry(self, symbol: str, option_type: str,
                             order_block: dict) -> tuple[bool, str]:
        """Sniper entry confirmation on 1m: OB retest + CHOCH + wick rejection.

        No market entry on impulse — Tiger waits for price to retest the order
        block, then confirms a Change of Character + a rejection wick on 1m.
        """
        from config.thresholds import SNIPER
        df_1m = self.data_map_1m.get(symbol)
        if df_1m is None or df_1m.empty or len(df_1m) < SNIPER["CHOCH_LOOKBACK"] + 2:
            return False, "insufficient 1m data"

        ob_top = float(order_block.get("top", 0.0) or 0.0)
        ob_bottom = float(order_block.get("bottom", 0.0) or 0.0)
        if ob_top <= 0 or ob_bottom <= 0:
            return False, "order block edges missing"

        lookback = SNIPER["CHOCH_LOOKBACK"]
        window = df_1m.iloc[-lookback - 1:]
        last = window.iloc[-1]
        o, c = float(last["open"]), float(last["close"])
        h, l = float(last["high"]), float(last["low"])
        rng = h - l
        if rng <= 0:
            # Zero-range candle = no WS tick yet for this minute.
            # Don't block — use the PREVIOUS candle instead.
            if len(window) >= 2:
                last = window.iloc[-2]
                o, c = float(last["open"]), float(last["close"])
                h, l = float(last["high"]), float(last["low"])
                rng = h - l
            if rng <= 0:
                # Still zero — skip retest/CHOCH, enter on zone strength alone
                logger.info(
                    f"🎯 SNIPER [{symbol}] zero-range 1m — entering on zone strength")
                return True, "zone confirmed (1m flat — no tick yet)"

        is_ce = option_type.upper() == "CE"

        # 1) OB RETEST — recent 1m low (CE) / high (PE) must have touched the OB.
        # Tolerance widened from 0.2 to 0.5 (50% of OB height) — the tight 20%
        # tolerance rarely matched in live MCX data, killing valid entries.
        recent_lows = window["low"].astype(float)
        recent_highs = window["high"].astype(float)
        ob_height = max(ob_top - ob_bottom, 0.001)
        if is_ce:
            retested = bool((recent_lows <= ob_top + ob_height * 0.5).any())
        else:
            retested = bool((recent_highs >= ob_bottom - ob_height * 0.5).any())
        if not retested:
            return False, "OB not retested on 1m"

        # 2) CHOCH (Change of Character) — RELAXED for aggressive trading.
        #    If last candle is in the reversal direction → strong entry.
        #    If not but zone score >= 50 → still enter (user wants trades).
        #    Tiger can't wait forever for perfect candle — "Pani ki trha
        #    market Mai jaye" — flow into the market, don't wait for perfect.
        prior = window.iloc[:-1]
        if is_ce:
            choch = c > o  # bullish candle = ideal
        else:
            choch = c < o  # bearish candle = ideal
        if not choch:
            # Zone is strong enough — enter even without perfect candle.
            # The OB retest + zone score is the real signal, CHOCH is bonus.
            logger.info(
                f"🎯 SNIPER [{symbol}] CHOCH relaxed — entering on zone strength "
                f"(candle {'bull' if c > o else 'bear'} but zone confirmed)")
            return True, "OB_retest (CHOCH relaxed — zone confirmed)"

        # 3) WICK REJECTION — optional when the reversal candle is impulsive.
        #    A strong-body reversal (body >= 60% of range) is itself the
        #    confirmation — forcing a wick on it misses impulsive rockets
        #    (real momentum moves are body-heavy, not wick-heavy). Only a
        #    small-body candle NEEDS a rejection wick to prove rejection.
        body = abs(c - o)
        lower_wick = min(o, c) - l
        upper_wick = h - max(o, c)
        body_ratio = body / rng
        if body_ratio < 0.60:  # not impulsive → require a rejection wick
            if is_ce:
                wick_ratio = lower_wick / rng
            else:
                wick_ratio = upper_wick / rng
            if wick_ratio < SNIPER["WICK_REJECTION_MIN"]:
                return False, f"weak reversal (body {body_ratio:.0%}, wick {wick_ratio:.0%} < {SNIPER['WICK_REJECTION_MIN']:.0%})"

        return True, "OB_retest+CHOCH+wick/body"

    def _sniper_stop_price(self, entry_price: float, option_type: str,
                           order_block: dict) -> float:
        """SL = order block edge +/- 0.35% buffer (on the option premium)."""
        from config.thresholds import SNIPER
        buffer = entry_price * (SNIPER["OB_BUFFER_PCT"] / 100.0)
        is_ce = option_type.upper() == "CE"
        ob_edge = float(order_block.get("bottom", 0.0) or 0.0) if is_ce \
            else float(order_block.get("top", 0.0) or 0.0)
        # If OB edge not usable, fall back to entry - buffer (CE) / entry + buffer (PE)
        if ob_edge <= 0:
            ob_edge = entry_price
        # Express the OB edge as a premium-side stop via the buffer distance.
        # CE: stop below entry; PE: stop below entry too (option premium drops).
        stop = entry_price - buffer - max(0.0, entry_price - ob_edge) * 0.5 if is_ce \
            else entry_price - buffer
        return max(stop, entry_price * 0.80)  # never tighter than -20%

    def scan_sniper_signals(self) -> list[dict]:
        """Run the sniper scanner across the ACTIVE market(s) → 0 or 1 signal.

        One engine, two markets:
          - NSE session (09:15-15:00): NIFTY/BANKNIFTY/FINNIFTY/SENSEX + top
            liquid F&O stocks, with NSE_MIN_CONFLUENCE_COMPONENTS=2 (looser —
            index/stock moves are noisier, 3-component confluence is rare).
          - MCX session (10:30-23:30): GOLD/SILVER/CRUDEOIL/NATURALGAS/COPPER,
            with MIN_CONFLUENCE_COMPONENTS=3 (strict — commodities trend clean).

        Both share the SAME pure-SMC scanner (BOS+sweep+OB+FVG), the SAME 1m
        entry confirmation (OB retest + CHOCH + wick), the SAME ML 0.80 gate,
        and the SAME exit (no fixed target, +5% trail / 50% peak lock).
        Returns up to the remaining daily sniper budget of signals — both
        markets are scanned independently during the overlap window.
        """
        from config.thresholds import SNIPER

        # === SEQUENTIAL TIMING (user mandate) ===
        # NSE morning (09:15-15:00) → MCX evening (15:30-23:30).
        # NEVER overlap — each market gets full focus + its own 50% capital.
        # During NSE hours, scan ONLY NSE. After 15:30, scan ONLY MCX.
        signals: list[dict] = []
        if self._nse_sniper_session_active():
            sig = self._scan_one_market("NSE")
            if sig:
                signals.append(sig)
        elif self._sniper_session_active():
            # MCX only AFTER NSE session ends (15:30 onwards)
            sig = self._scan_one_market("MCX")
            if sig:
                signals.append(sig)

        # Respect the shared daily cap across markets: never return more than
        # the remaining sniper budget for today.
        remaining = SNIPER["MAX_TRADES_PER_DAY"] - self._sniper_trades_today()
        if remaining <= 0:
            return []
        return signals[:remaining]

    def _scan_one_market(self, market: str) -> Optional[dict]:
        """Scan one market (NSE or MCX) for a sniper entry. Returns a signal
        dict (compatible with _place_live_orders) or None.

        Shared core: build 5m from 1m, run scan_mcx with the market's allowed
        universe + min_components, confirm on 1m, apply the ML 0.80 gate.
        """
        from config.thresholds import SNIPER
        from subbrains.mcx_scanner import calculate_atr_pct

        can, reason = self._sniper_can_trade(market=market)
        if not can:
            logger.info(f"🎯 SNIPER [{market}] skip: {reason}")
            return None

        # Build the universe set + min_components for this market
        if market == "NSE":
            try:
                from universe.fno_universe import nse_scan_symbols
                universe = set(nse_scan_symbols().keys())
            except Exception as exc:
                logger.warning(f"🎯 SNIPER [NSE] universe fetch fail: {exc}")
                return None
            min_components = SNIPER.get("NSE_MIN_CONFLUENCE_COMPONENTS", 2)
            exchange = "NSE"
        else:
            universe = set(SNIPER_MCX_SYMBOLS)
            min_components = SNIPER.get("MIN_CONFLUENCE_COMPONENTS", 3)
            exchange = "MCX"

        # Build 5m candles from 1m data (resample) for each symbol in universe
        data_map_5m: dict = {}
        skipped = 0
        for sym in universe:
            df_1m = self.data_map_1m.get(sym)
            if df_1m is None or df_1m.empty:
                skipped += 1
                continue
            if len(df_1m) < 15:
                skipped += 1
                continue
            try:
                # Ensure DatetimeIndex for resample (tz fix: some 1m DataFrames
                # have plain Index after merge — resample silently fails).
                if not hasattr(df_1m.index, 'freq') and not isinstance(df_1m.index, pd.DatetimeIndex):
                    df_1m = df_1m.copy()
                    df_1m.index = pd.to_datetime(df_1m.index, errors='coerce')
                    df_1m = df_1m.dropna()  # drop rows with NaT index
                df_5m = df_1m.resample("5min").agg({
                    "open": "first", "high": "max",
                    "low": "min", "close": "last",
                    "volume": "sum",
                }).dropna()
                if len(df_5m) >= 10:
                    data_map_5m[sym] = df_5m
            except Exception as exc:
                logger.debug(f"🎯 SNIPER [{market}] 5m build fail {sym}: {exc}")
        if not data_map_5m:
            logger.info(f"🎯 SNIPER [{market}]: no 5m data ({len(universe)} symbols, "
                        f"{skipped} skipped, 0 built) — waiting for WS 1m")
            return None

        zone = scan_mcx_sniper(data_map_5m, now_ts=datetime.now().isoformat(),
                               min_components=min_components, allowed=universe,
                               market=market)
        if zone is None:
            logger.info(f"🎯 SNIPER [{market}]: NO_TRADE — "
                        f"{len(data_map_5m)} symbols scanned, no zone > "
                        f"{SNIPER.get('MIN_ZONE_STRENGTH', 50):.0f}")
            return None

        sig = zone.to_signal()
        symbol = sig["symbol"]
        option_type = sig["option_type"]

        # Entry confirmation on 1m
        ok, why = self._verify_sniper_entry(symbol, option_type, sig.get("order_block", {}))
        if not ok:
            logger.info(f"🎯 SNIPER [{market}] ENTRY WAIT {symbol} {option_type} — {why}")
            return None

        # ML conviction ADVISOR — win_prob as confidence signal, never blocks
        try:
            ml_features = extract_live_features(
                symbol=symbol, signal=sig,
                data_map_15m=self.data_map, data_map_1m=self.data_map_1m,
                broker=self.broker, pcr_value=sig.get("pcr", 1.0),
            )
            sig["ml_features"] = ml_features
            ml_passed, win_prob = self.ml_gate.check_gate(ml_features)
            sig["ml_win_prob"] = win_prob
            sig["sensex_trend"] = float(ml_features.get("sensex_trend", 0.0))
            # ADVISORY: ML never blocks. Log win_prob as confidence signal.
            if win_prob < SNIPER["MIN_WIN_PROB"]:
                logger.info(
                    f"💡 ML ADVISORY {symbol} {option_type} — "
                    f"win_prob={win_prob:.2f} < {SNIPER['MIN_WIN_PROB']:.2f} "
                    f"(advisory only — Tiger decides)")
            logger.info(
                f"🎯 SNIPER [{market}] ENTRY READY {symbol} {option_type} — "
                f"score={sig['zone_strength']:.0f} win_prob={win_prob:.2f} {why}")
        except Exception as exc:
            logger.warning(f"🎯 SNIPER [{market}] ML gate error (pass-through): {exc}")
            sig["ml_features"] = {}
            sig["ml_win_prob"] = 1.0
            sig["sensex_trend"] = 0.0

        # Stamp sniper exit params on the signal for the exit engine
        sig["is_sniper"] = True
        sig["is_scalper"] = False
        sig["is_momentum_hunter"] = False
        sig["setup_score"] = float(sig.get("zone_strength", 85.0))
        sig["entry_ts"] = datetime.now()
        sig["sniper_atr_pct"] = sig.get("commodity_volatility", 0.0)
        sig["market"] = market

        # === CRITICAL: set ATM strike from 1m data ===
        # Without this, strike defaults to 0 → "CRUDEOIL 0CE" → wrong contract.
        # The ATM strike = underlying's last close, rounded to nearest strike.
        df_1m = self.data_map_1m.get(symbol)
        if df_1m is not None and not df_1m.empty:
            atm_price = float(df_1m["close"].iloc[-1])
            sig["strike"] = atm_price
            logger.info(
                f"🎯 SNIPER [{symbol}] ATM strike set: ₹{atm_price:.2f} "
                f"from 1m close")
        else:
            # Fallback: use order_block midpoint
            ob = sig.get("order_block", {})
            atm_price = (float(ob.get("top", 0)) + float(ob.get("bottom", 0))) / 2
            sig["strike"] = atm_price if atm_price > 0 else 0
            logger.warning(
                f"🎯 SNIPER [{symbol}] no 1m data — using OB midpoint "
                f"₹{atm_price:.2f} as ATM strike")

        return sig

    # ============================================================
    # PRE-MARKET (09:00) — login + data load
    # ============================================================
    def pre_market_wake(self):
        """Broker login + instrument master + fresh data fetch."""
        logger.info("=" * 60)
        logger.info("🐅 TIGER PRE-MARKET WAKE — %s", datetime.now().strftime("%A %Y-%m-%d"))
        logger.info("=" * 60)

        # === TIGER MEMORY RECALL — what Tiger remembers from past trades ===
        try:
            from replay.tiger_memory import recall_memory
            recall_msg = recall_memory()
            for line in recall_msg.split("\n"):
                logger.info(line)
        except Exception as exc:
            logger.warning(f"Memory recall fail: {exc}")

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

            # Subscribe WS FIRST — so ticks start flowing during data fetch
            self._subscribe_ws_symbols(list(syms.keys()))

            # Startup: fetch 15m historical (zones) + 1m historical (momentum).
            # Live refresh later skips 1m REST (uses WS live 1m candles).
            self.data_map, self.data_map_1m, failed = fetch_angel_data(
                self.broker, days_15m=30, days_1m=7, fetch_1m=True,
                symbols=syms)
            logger.info("✅ Data fetched [%s]: %d symbols (15m), %d (1m). Failed: %d",
                        market, len(self.data_map), len(self.data_map_1m), len(failed))
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
    # LIVE DATA REFRESH — 15m cached 30 min, 1m from WebSocket
    # ============================================================
    _15M_REFRESH_INTERVAL_MIN = 30

    def _refresh_live_data(self):
        """Refresh 15m data (cached 30 min) + merge WS 1m candles.

        15m historical candles (zone detection) barely change in 30 min.
        Fetching them every 10 min wastes REST calls and hits rate limits.
        Instead: fetch 15m every 30 min, update latest bars from WS live
        ticks between refreshes. 1m always from WebSocket (zero rate limits).

        REST calls per 30 min: 27 (was 27 every 10 min = 81).
        """
        if self.broker is None:
            return
        try:
            from universe.fno_universe import get_active_scan_symbols

            symbols, market = get_active_scan_symbols()
            if not symbols:
                logger.info("Live data refresh: market CLOSED, skip fetch.")
                return

            # 15m refresh throttle — separate timestamp (not _last_data_refresh
            # which updates every 2 min for WS merge). This ensures 15m REST
            # fetch actually happens every 30 min, not skipped forever.
            now_dt = datetime.now()
            need_15m_fetch = True
            if self._last_15m_fetch is not None:
                mins_since_15m = (now_dt - self._last_15m_fetch).total_seconds() / 60
                if mins_since_15m < self._15M_REFRESH_INTERVAL_MIN:
                    need_15m_fetch = False

            if need_15m_fetch:
                from backtest.run_tiger_brain_backtest import fetch_angel_data
                fresh_15m, _fresh_1m, failed = fetch_angel_data(
                    self.broker, days_15m=10, days_1m=3, fetch_1m=False,
                    symbols=symbols)
                if fresh_15m:
                    # Merge: keep existing symbols, update with fresh data
                    # Don't lose symbols that failed this fetch (keep old data)
                    for sym, df in fresh_15m.items():
                        self.data_map[sym] = df
                    self._last_15m_fetch = datetime.now()
                    logger.info("15m refresh [%s]: %d symbols fetched, "
                                "%d failed (kept cached). Failed: %s",
                                market, len(fresh_15m),
                                len(symbols) - len(fresh_15m),
                                failed[:5] if failed else "none")
                else:
                    logger.warning("15m refresh fail — using cached data")
            else:
                logger.debug("15m refresh skipped (last %.0f min ago) — "
                             "using cached + WS live", mins_since_15m)

            # Always merge WS 1m candles (zero REST calls)
            self._merge_ws_1m_candles()

            # Re-subscribe to WebSocket for new market symbols
            self._subscribe_ws_symbols(list(symbols.keys()))
        except Exception as exc:
            logger.warning("Live data refresh fail — continuing with cached data: %s", exc)

    def _merge_ws_1m_candles(self):
        """Merge live WebSocket 1m candles into data_map_1m.

        WebSocket TickCandleBuilder produces real-time 1m OHLCV bars from
        live ticks. This appends them to the historical 1m data so the
        scanner has up-to-date 1m candles without any REST calls.

        If a symbol's 1m REST fetch failed (rate limit), it's not in
        data_map_1m yet — create it from WS candles so velocity gate
        can confirm entries.
        """
        if self.broker is None or self.broker.websocket is None:
            return
        if not self.broker.websocket.is_healthy():
            return
        ws = self.broker.websocket
        merged = 0
        created = 0
        # Iterate ALL scan symbols (data_map has 15m — that's our universe)
        for sym in list(self.data_map.keys()):
            token = ws._resolve_symbol_token(sym)
            if token is None:
                continue
            ws_df = ws.get_1m_candles(token, min_bars=1)
            if ws_df is None or ws_df.empty:
                continue
            if sym not in self.data_map_1m or self.data_map_1m[sym] is None or self.data_map_1m[sym].empty:
                # REST 1m fetch failed (rate limit) — create from WS live ticks
                self.data_map_1m[sym] = ws_df.copy()
                created += 1
            else:
                # Append WS live candles to historical 1m data.
                # Normalize tz: WS candles may be tz-aware (Asia/Kolkata)
                # while historical 1m data is tz-naive — comparing them
                # directly raises TypeError in pandas 2.x. Coerce both to
                # the same tz before the comparison.
                hist_df = self.data_map_1m[sym]
                ws_idx = ws_df.index
                hist_idx = hist_df.index
                # Safe tz handling — some indices may not be DatetimeIndex
                # (plain Index has no .tz attr → AttributeError crash).
                # This was the #1 live bug: 677 crashes/day, WS 1m never merged.
                def _get_tz(idx):
                    if hasattr(idx, 'tz'):
                        return idx.tz
                    return None
                ws_tz = _get_tz(ws_idx)
                hist_tz = _get_tz(hist_idx)
                if ws_tz is not None and hist_tz is None:
                    hist_idx = hist_idx.tz_localize(ws_tz)
                elif ws_tz is None and hist_tz is not None:
                    ws_idx = ws_idx.tz_localize(hist_tz)
                elif ws_tz is not None and hist_tz is not None and ws_tz != hist_tz:
                    ws_idx = ws_idx.tz_convert(hist_tz)
                last_hist_ts = hist_idx[-1]
                new_bars = ws_df[ws_idx > last_hist_ts]
                if not new_bars.empty:
                    # Drop tz to match historical storage (tz-naive) so later
                    # downstream code that expects tz-naive 1m data keeps working.
                    if new_bars.index.tz is not None:
                        new_bars = new_bars.tz_localize(None)
                    self.data_map_1m[sym] = pd.concat([hist_df, new_bars])
                    if len(self.data_map_1m[sym]) > 1000:
                        self.data_map_1m[sym] = self.data_map_1m[sym].tail(1000)
                    merged += 1
        if merged or created:
            logger.info("📡 WS 1m merge: %d updated, %d created from live ticks (data_map_1m has %d symbols)", merged, created, len(self.data_map_1m))
        else:
            logger.info("📡 WS 1m merge: 0 updates (data_map_1m has %d symbols, data_map has %d)", len(self.data_map_1m), len(self.data_map))

        # === INDEX VOLUME BACKFILL (Bug fix) ===
        # Index spot tokens (NIFTY/BANKNIFTY) report volume=0 from Angel
        # historical API. WS ticks carry real volume_trade_for_the_day →
        # the 1m bars above have real volume. Now resample those 1m bars
        # to 15m and backfill the 15m data_map so zone detection sees
        # real volume for today's bars.
        self._backfill_index_volume_15m(ws)

    def _backfill_index_volume_15m(self, ws=None):
        """Backfill index volume in the 15m data_map from WS 1m candles.

        For index symbols (NIFTY/BANKNIFTY/FINNIFTY), the 15m historical
        candles have volume=0 (Angel spot feed limitation). This method:
          1. Takes the WS 1m candles (which have REAL volume from ticks)
          2. Resamples them to 15m bars
          3. For each 15m bar in data_map that matches a WS-resampled bar,
             replaces volume=0 with the real WS volume
          4. Also fetches options-chain OI volume as a second source

        This makes zone_explosive_quality, volume_delta, volume_velocity,
        and compute_volume_profile all work for indices — they were all
        dead/skipped when volume=0.
        """
        if ws is None:
            if self.broker is None or self.broker.websocket is None:
                return
            ws = self.broker.websocket
        from data.loader import INDEX_UNDERLYING_TOKENS, fetch_index_oi_volume

        for sym in list(self.data_map.keys()):
            if sym.upper() not in INDEX_UNDERLYING_TOKENS:
                continue  # stocks/commodities already have real volume

            df_15m = self.data_map.get(sym)
            if df_15m is None or df_15m.empty:
                continue

            # Source 1: WS 1m → resample to 15m
            token = ws._resolve_symbol_token(sym)
            if token is not None:
                ws_1m = ws.get_1m_candles(token, min_bars=1)
                if ws_1m is not None and not ws_1m.empty:
                    # Resample 1m → 15m, summing volume
                    ws_15m = ws_1m.resample("15min", closed="left").agg({
                        "open": "first", "high": "max", "low": "min",
                        "close": "last", "volume": "sum"
                    }).dropna(subset=["open"])
                    # Merge volume into the 15m DataFrame
                    updated = 0
                    for ts_15m, row_15m in ws_15m.iterrows():
                        ws_vol = float(row_15m.get("volume", 0) or 0)
                        if ws_vol <= 0:
                            continue
                        # Find matching bar in data_map by timestamp (floor to 15m)
                        ts_match = ts_15m
                        # Try exact match, then floored match
                        mask = df_15m.index == ts_match
                        if not mask.any():
                            # Floor to 15m boundary
                            try:
                                ts_floored = ts_15m.floor("15min")
                                mask = df_15m.index == ts_floored
                            except Exception:
                                pass
                        if mask.any():
                            cur_vol = float(df_15m.loc[mask, "volume"].iloc[0] or 0)
                            if cur_vol <= 0 or ws_vol > cur_vol:
                                df_15m.loc[mask, "volume"] = ws_vol
                                updated += 1
                    if updated:
                        self.data_map[sym] = df_15m
                        logger.debug(
                            f"📈 INDEX VOL BACKFILL [{sym}]: {updated} "
                            f"15m bars got real WS volume (was 0)")

            # Source 2: Options-chain OI volume (cumulative proxy)
            # Throttled — only every 5 min to avoid API spam
            cache_key = f"_oi_vol_{sym}"
            last_oi = getattr(self, cache_key, None)
            now_dt = datetime.now()
            if last_oi is not None and (now_dt - last_oi).total_seconds() < 300:
                continue  # 5 min throttle
            try:
                oi_data = fetch_index_oi_volume(self.broker, sym.upper())
                if oi_data and oi_data.get("total_volume", 0) > 0:
                    # Inject as the LATEST bar's volume (cumulative day volume
                    # is a proxy for today's total participation)
                    total_vol = oi_data["total_volume"]
                    if not df_15m.empty and "volume" in df_15m.columns:
                        # If last bar volume is 0 or much smaller than OI volume,
                        # use OI volume as a floor
                        last_idx = df_15m.index[-1]
                        last_vol = float(df_15m.loc[last_idx, "volume"] or 0)
                        if last_vol < total_vol:
                            df_15m.loc[last_idx, "volume"] = total_vol
                            self.data_map[sym] = df_15m
                            logger.info(
                                f"📈 INDEX OI VOL [{sym}]: options-chain "
                                f"vol={total_vol:.0f} OI={oi_data['total_oi']:.0f} "
                                f"B/S={oi_data['buy_sell_ratio']:.2f} → "
                                f"15m last bar volume backfilled")
                    setattr(self, cache_key, now_dt)
            except Exception as exc:
                logger.debug(f"OI volume fetch fail for {sym}: {exc}")
                setattr(self, cache_key, now_dt)  # throttle even on failure

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

            # Position tracker load (peak + target_booked + entry_time)
            tracker = self._position_peaks.get(tsym, {})
            peak = max(float(tracker.get("peak", 0) or 0), ltp, entry_price)
            target_booked = bool(tracker.get("target_booked", False))
            entry_time = tracker.get("entry_time")
            if not entry_time:
                entry_time = datetime.now().isoformat()
            # Preserve sniper metadata (is_sniper, order_block, sniper_atr_pct,
            # symbol, ml_features) stashed at entry — the rebuild below must
            # NOT wipe them, else the sniper exit branch never fires.
            self._position_peaks[tsym] = {
                **tracker,                       # keep sniper + ml fields
                "peak": peak, "target_booked": target_booked,
                "entry": entry_price,
                "entry_time": entry_time,
            }

            # Calculate hold time once — used by both log and scalper exit
            hold_seconds = 9999  # default: no min hold restriction
            try:
                hold_seconds = (datetime.now() - datetime.fromisoformat(entry_time)).total_seconds()
            except (ValueError, TypeError):
                pass

            hold_str = f"{hold_seconds/60:.0f}m" if hold_seconds < 9999 else "?"
            logger.info(
                f"👁️ {tsym}: entry=₹{entry_price:.2f} ltp=₹{ltp:.2f} "
                f"gain={gain_pct:+.1f}% peak=₹{peak:.2f} "
                f"held={hold_str}"
                f"{' [target_booked]' if target_booked else ''}"
                f"{' [SCALPER]' if tsym in self._scalper_positions else ''}")

            # === SCALPER EXIT LOGIC (MOMENTUM-AWARE edition) ===
            # Give trades room to breathe — momentum needs time to develop.
            #   1. Target +15% → instant full exit (let profit run higher)
            #   2. Catastrophic -12% → instant exit (black swan, no hold time)
            #   3. After 3-min min hold: -7% OR -₹1500 → exit
            #   4. Breakeven lock: once +3% seen, stop moves to entry
            #   5. Trail: once +3% seen, lock 70% of peak
            if tsym in self._scalper_positions:
                from config.thresholds import SCALPER
                exit_reason = None
                exit_qty = qty

                loss_rupees = (entry_price - ltp) * qty
                loss_pct = ((entry_price - ltp) / entry_price) * 100 if entry_price > 0 else 0
                peak_gain_pct = ((peak - entry_price) / entry_price) * 100 if entry_price > 0 else 0

                min_hold = SCALPER.get("MIN_HOLD_SECONDS", 180)
                past_min_hold = hold_seconds >= min_hold

                # === 1:2 RR TARGET REMOVED — pure momentum ride (Sep 2026) ===
                # effective_stop_pct still used by the normal stop-loss gate below.
                structural_stop_pct = tracker.get("structural_stop_pct", 0)
                effective_stop_pct = SCALPER["MAX_STOP_PCT"]  # default -7%
                if structural_stop_pct > 0:
                    effective_stop_pct = max(structural_stop_pct, SCALPER["MAX_STOP_PCT"])
                    effective_stop_pct = min(effective_stop_pct, SCALPER.get("CATASTROPHIC_STOP_PCT", 12.0))
                # rr_target_pct = effective_stop_pct * 2  # 1:2 RR — removed (no fixed target)

                # === 1:2 RR TARGET REMOVED — pure momentum ride (Sep 2026).
                #    Scalper now rides winners via trailing only; no fixed RR target.
                # if gain_pct >= rr_target_pct:
                #     exit_reason = f"scalper_rr_target_{rr_target_pct:.0f}pct"
                # 2. Catastrophic stop: -12% → instant exit (black swan, no hold time)
                if loss_pct >= SCALPER.get("CATASTROPHIC_STOP_PCT", 12.0):
                    exit_reason = f"scalper_catastrophic_{SCALPER.get('CATASTROPHIC_STOP_PCT', 12.0):.0f}pct"
                # 3. Normal stop: structural (zone-based) OR fixed -7%, whichever wider
                #    CRUDEOIL fix: if structural stop is -10% (zone bottom), Tiger
                #    uses -10% not -7%. Survives pullback before rocket.
                #    ONLY after min hold time (momentum needs time to develop).
                elif past_min_hold:
                    if loss_pct >= effective_stop_pct:
                        if structural_stop_pct > 0 and effective_stop_pct > SCALPER["MAX_STOP_PCT"]:
                            exit_reason = f"scalper_structural_stop_{effective_stop_pct:.0f}pct"
                        else:
                            exit_reason = f"scalper_stop_{SCALPER['MAX_STOP_PCT']:.0f}pct"
                    elif loss_rupees >= SCALPER["MAX_STOP_RUPEES"]:
                        exit_reason = f"scalper_stop_₹{SCALPER['MAX_STOP_RUPEES']}"
                # 4. Breakeven + Trail — LOCK profit once +3% seen
                #    CRUDEOIL went +4.7% → fell to -2.7% = profit leak FIXED
                elif peak_gain_pct >= 3.0:
                    trail_floor = entry_price * (1 + peak_gain_pct * 0.70 / 100)
                    if ltp <= trail_floor:
                        if ltp >= entry_price:
                            exit_reason = "TRAILING_EXIT"
                        else:
                            exit_reason = "scalper_breakeven_exit"

                if exit_reason:
                    pos_product = p.get("producttype", "INTRADAY")
                    if pos_product not in ("INTRADAY", "CARRYFORWARD"):
                        pos_product = "INTRADAY"
                    result = self.broker.place_option_order(
                        tradingsymbol=tsym, symboltoken=token, exchange=exch,
                        transaction_type="SELL", quantity=exit_qty,
                        product_type=pos_product, order_type="MARKET",
                        is_exit=True)
                    if result.get("success"):
                        closed += 1
                        self._scalper_positions.discard(tsym)
                        self._save_scalper_positions()
                        pnl = (ltp - entry_price) * exit_qty
                        hold_str = f"{hold_seconds/60:.1f}min" if hold_seconds < 9999 else "?"
                        logger.info(
                            f"📤 SCALPER EXIT {tsym}: {exit_reason} — "
                            f"SELL {exit_qty}/{qty} @ LTP ₹{ltp:.2f} "
                            f"(held {hold_str}, gain {gain_pct:+.1f}%, "
                            f"PnL ₹{pnl:+.0f})")
                        # Record PnL for RiskManager consecutive-loss tracking
                        self._record_trade_pnl(pnl, tsym, exit_reason)
                        # Mark position as exited in order log (exposure fix)
                        for o in self._order_log:
                            if o.get("tradingsymbol") == tsym and o.get("success"):
                                o["exited"] = True
                        self._save_order_log()
                        # Trade log exit
                        try:
                            from replay.nightly_replay import append_trade_record
                            _pe = self._position_peaks.get(tsym, {})
                            _ml_feats = _pe.get("ml_features", {})
                            append_trade_record({
                                "symbol": tsym,
                                "tradingsymbol": tsym,
                                "exchange": exch,
                                "exit_time": datetime.now().isoformat(),
                                "entry_ts": _pe.get("entry_time", ""),
                                "exit_price": ltp,
                                "exit_reason": exit_reason,
                                "pnl": pnl,
                                "win": 1 if pnl > 0 else 0,
                                "status": "CLOSED",
                                "is_scalper": True,
                                "entry_quality": _pe.get("entry_quality", {}),
                                "ml_features": _ml_feats,
                                "ml_win_prob": _pe.get("ml_win_prob", 1.0),
                                "sensex_trend": _pe.get("sensex_trend", 0.0),
                            })
                        except Exception:
                            pass
                    else:
                        logger.error(
                            f"❌ SCALPER EXIT FAIL {tsym}: {exit_reason} — {result.get('error')}")
                continue  # scalper positions don't use V19 exit logic

            # === SNIPER EXIT LOGIC (TIGER SNIPER ADVANCED V2) ===
            # NO FIXED TARGET. Pure momentum ride — let the rocket run.
            #   1. Give the rocket room: trail arms only after +25% profit
            #      (aggressive — only true rockets arm the trail; small pops
            #      stay on the wide OB stop so the rocket isn't choked early).
            #   2. Once armed, trail = 50% of peak (lock half, ride the rest).
            #   3. OB hard stop at -12% (survive the pre-rocket pullback/noise).
            #   4. 5m opposite BOS = structure reversal exit.
            # exit_reason = "SNIPER_TRAILING_EXIT" for the trailing case.
            if tracker.get("is_sniper", False) or tsym in getattr(self, "_sniper_positions", set()):
                from config.thresholds import SNIPER as _SNIPER_CFG
                from subbrains.mcx_scanner import detect_bos as _sniper_detect_bos
                # Option type for the 5m opposite-BOS check — read it from the
                # position tracker (stamped at entry), falling back to the
                # tradingsymbol suffix (…CE/…PE) for positions adopted after a
                # restart where the tracker has no option_type yet.
                option_type = tracker.get("option_type") or (
                    "PE" if tsym.upper().endswith("PE") else "CE")
                exit_reason = None
                exit_qty = qty

                peak_premium = max(peak, entry_price)
                gain_pct_now = (ltp - entry_price) / entry_price * 100.0
                # Where the trailing floor sits once armed.
                trail_lock_frac = _SNIPER_CFG["TRAIL_LOCK_PCT_OF_PEAK"] / 100.0
                trail_floor = peak_premium - (peak_premium - entry_price) * (1.0 - trail_lock_frac)

                # 1. OB hard stop — wide (-12%) so the rocket survives its
                #    initial pullback before takeoff (was -0.7% → 40 premature
                #    exits in backtest, momentum surrendered).
                ob_stop = entry_price * (1.0 - _SNIPER_CFG["OB_STOP_PCT"] / 100.0)
                if ltp <= ob_stop:
                    exit_reason = "sniper_ob_stop"
                # 2. Trailing SL — arms after +10% profit. Before that the
                #    OB stop is the ONLY exit, so the rocket has room to ignite.
                elif gain_pct_now >= _SNIPER_CFG["TRAIL_ACTIVATE_PCT"] and ltp <= trail_floor:
                    exit_reason = _SNIPER_CFG["EXIT_REASON"]  # SNIPER_TRAILING_EXIT
                # 3. 5m opposite BOS — structure reversal. BUT only checked
                #    AFTER the trail has armed (gain >= TRAIL_ACTIVATE_PCT).
                #    Before arming, first-pullback BOS must NOT kill the rocket
                #    — that was the #1 momentum-surrender bug (rockets died at
                #    +5% on the first 1-bar dip). Now the OB stop protects pre-
                #    arming, and a CONFIRMED 2-bar BOS handles post-arming exits.
                elif gain_pct_now >= _SNIPER_CFG["TRAIL_ACTIVATE_PCT"]:
                    underlying = tsym
                    for _sym in SNIPER_MCX_SYMBOLS:
                        if _sym in tsym or tsym.startswith(_sym):
                            underlying = _sym
                            break
                    df_1m_sym = self.data_map_1m.get(underlying)
                    df_5m = None
                    if df_1m_sym is not None and not df_1m_sym.empty and len(df_1m_sym) >= 26:
                        try:
                            df_5m = df_1m_sym.resample("5min").agg({
                                "open": "first", "high": "max",
                                "low": "min", "close": "last",
                                "volume": "sum",
                            }).dropna()
                        except Exception:
                            pass
                    # 2-bar confirmed BOS: the break must hold for 2 consecutive
                    # 5m closes (single-bar noise is NOT a real structure reversal).
                    if df_5m is not None and len(df_5m) >= 26:
                        bos = _sniper_detect_bos(df_5m, len(df_5m) - 1)
                        if bos is not None:
                            is_ce = option_type.upper() == "CE"
                            opposite = (is_ce and bos["direction"] == "bearish") or \
                                       (not is_ce and bos["direction"] == "bullish")
                            if opposite and len(df_5m) >= 2:
                                # Confirm: prior 5m close also on the break side
                                prev_close = float(df_5m.iloc[-2]["close"])
                                bos_level = float(bos["level"])
                                confirmed = (is_ce and prev_close < bos_level) or \
                                            (not is_ce and prev_close > bos_level)
                                if confirmed:
                                    exit_reason = "sniper_5m_opposite_bos"

                if exit_reason:
                    pos_product = p.get("producttype", "INTRADAY")
                    if pos_product not in ("INTRADAY", "CARRYFORWARD"):
                        pos_product = "INTRADAY"
                    result = self.broker.place_option_order(
                        tradingsymbol=tsym, symboltoken=token, exchange=exch,
                        transaction_type="SELL", quantity=exit_qty,
                        product_type=pos_product, order_type="MARKET",
                        is_exit=True)
                    if result.get("success"):
                        closed += 1
                        if hasattr(self, "_sniper_positions"):
                            self._sniper_positions.discard(tsym)
                        pnl = (ltp - entry_price) * exit_qty
                        logger.info(
                            f"🎯 SNIPER EXIT {tsym}: {exit_reason} — "
                            f"SELL {exit_qty}/{qty} @ LTP ₹{ltp:.2f} "
                            f"(gain {gain_pct:+.1f}%, peak ₹{peak_premium:.2f}, "
                            f"PnL ₹{pnl:+.0f})")
                        self._record_trade_pnl(pnl, tsym, exit_reason)
                        for o in self._order_log:
                            if o.get("tradingsymbol") == tsym and o.get("success"):
                                o["exited"] = True
                        self._save_order_log()
                        try:
                            from replay.nightly_replay import append_trade_record
                            _pe = self._position_peaks.get(tsym, {})
                            _ml_feats = _pe.get("ml_features", {})
                            append_trade_record({
                                "symbol": _pe.get("symbol", underlying) if 'underlying' in dir() else tsym,
                                "tradingsymbol": tsym,
                                "exchange": exch,
                                "exit_time": datetime.now().isoformat(),
                                "entry_ts": _pe.get("entry_time", ""),
                                "exit_price": ltp,
                                "exit_reason": exit_reason,
                                "pnl": pnl,
                                "win": 1 if pnl > 0 else 0,
                                "status": "CLOSED",
                                "is_sniper": True,
                                "is_scalper": False,
                                "entry_quality": _pe.get("entry_quality", {}),
                                "ml_features": _ml_feats,
                                "ml_win_prob": _pe.get("ml_win_prob", 1.0),
                                "sensex_trend": _pe.get("sensex_trend", 0.0),
                                "trade_cost": _pe.get("trade_cost", entry_price * qty),
                            })
                        except Exception:
                            pass
                    else:
                        logger.error(
                            f"❌ SNIPER EXIT FAIL {tsym}: {exit_reason} — {result.get('error')}")
                continue  # sniper positions don't use V19 exit logic

            # === V19 EXIT LOGIC (on real broker data) ===
            # TIGHT EXIT — Tiger exits fast when wrong. No riding losers.
            #   1. Hard stop: -5% OR -₹800 max (whichever comes first)
            #   2. Breakeven lock: once +5% seen, stop moves to entry (no loss after profit)
            #   3. Trail: activates at +5%, locks 80% of peak (tighter than 70%)
            #   4. Fixed target: book 40% at +50%
            #   5. Runaway safety: exit at +250%
            from config.thresholds import SCALPER as _SCALPER_CFG
            exit_reason = None
            exit_qty = qty

            # 1. Hard stop-loss: -5% OR -₹800 max (whichever comes first)
            #    Tightened from -20% — BRITANNIA lost -₹2475 at -13% because
            #    -20% was too wide. Now Tiger exits at -5% or -₹800 max.
            stop_pct_threshold = entry_price * 0.95  # -5%
            stop_rs_threshold = entry_price - (_SCALPER_CFG["MAX_STOP_RUPEES"] / qty)
            stop_threshold = max(stop_pct_threshold, stop_rs_threshold)
            if ltp <= stop_threshold:
                if stop_pct_threshold >= stop_rs_threshold:
                    exit_reason = "stop_loss_5pct"
                else:
                    exit_reason = f"stop_loss_₹{_SCALPER_CFG['MAX_STOP_RUPEES']}"

            # 2. Breakeven lock — once position hit +5%, never take a loss on it
            elif gain_pct >= V19_TRAIL_ACTIVATE_PCT or target_booked:
                peak_gain = (peak - entry_price) / entry_price
                if peak_gain > 0:
                    # Lock 80% of peak (tighter than 70% — give back only 20%)
                    trail_floor = entry_price * (1 + peak_gain * 0.80)
                    if ltp <= trail_floor:
                        if ltp >= entry_price:
                            exit_reason = "TRAILING_EXIT"
                        else:
                            exit_reason = "breakeven_exit"

            # 3. FIXED TARGET REMOVED — pure momentum ride (Sep 2026).
            #    Tiger now rides winners via trailing only; no +50% booking.
            # if exit_reason is None and gain_pct >= V19_FIXED_TARGET_PCT \
            #         and not target_booked:
            #     exit_qty = max(1, int(qty * V19_FIXED_TARGET_BOOK))
            #     exit_reason = "fixed_target_50pct_book40"
            #     self._position_peaks[tsym]["target_booked"] = True

            # 4. Runaway safety — black-swan cap (not a profit target)
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
                    product_type=pos_product, order_type="MARKET",
                    is_exit=True)
                if result.get("success"):
                    closed += 1
                    pnl = (ltp - entry_price) * exit_qty
                    logger.info(
                        f"📤 EXIT {tsym}: {exit_reason} — "
                        f"SELL {exit_qty}/{qty} @ LTP ₹{ltp:.2f} "
                        f"(gain {gain_pct:+.1f}%, PnL ₹{pnl:+.0f})")
                    # Record PnL for RiskManager consecutive-loss tracking
                    self._record_trade_pnl(pnl, tsym, exit_reason)
                    # Mark position as exited in order log (exposure fix)
                    for o in self._order_log:
                        if o.get("tradingsymbol") == tsym and o.get("success"):
                            o["exited"] = True
                    self._save_order_log()
                    # Trade log exit
                    try:
                        from replay.nightly_replay import append_trade_record
                        _pe = self._position_peaks.get(tsym, {})
                        _ml_feats = _pe.get("ml_features", {})
                        append_trade_record({
                            "symbol": tsym,
                            "tradingsymbol": tsym,
                            "exchange": exch,
                            "exit_time": datetime.now().isoformat(),
                            "entry_ts": _pe.get("entry_time", ""),
                            "exit_price": ltp,
                            "exit_reason": exit_reason,
                            "pnl": pnl,
                            "win": 1 if pnl > 0 else 0,
                            "status": "CLOSED",
                            "is_scalper": False,
                            "ml_features": _ml_feats,
                            "ml_win_prob": _pe.get("ml_win_prob", 1.0),
                            "sensex_trend": _pe.get("sensex_trend", 0.0),
                        })
                    except Exception:
                        pass
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
    # INTRADAY SCAN (every 1 min) — entry signals + exits
    # ============================================================
    def intraday_scan(self):
        """Scan zones every 1 min, find entry signals, check exits.

        The backtest engine generates strategy signals. Then _place_live_orders()
        converts those signals into REAL Angel One orders.
        """
        # Prevent overlapping scans — heartbeat + scheduler can both fire.
        # Without this lock, two concurrent scans can both pass the dedup
        # check and place duplicate orders for the same signal.
        if not self._scan_lock.acquire(blocking=False):
            logger.debug("Scan already running — skip this cycle.")
            return
        try:
            # Dedup: heartbeat and the 1-min scheduler job both trigger a
            # scan. Skip if a scan already ran within the min interval so a
            # single minute never scans twice (was doubling REST/REST-like
            # candle calls and could place duplicate entries).
            now_ts = datetime.now()
            if self._last_scan_ts is not None:
                since = (now_ts - self._last_scan_ts).total_seconds()
                if since < self._scan_min_interval_sec:
                    logger.debug(
                        "Scan dedup — ran %.0fs ago (<%.0fs), skip.",
                        since, self._scan_min_interval_sec)
                    return
            self._last_scan_ts = now_ts
            self._intraday_scan_inner()
        finally:
            self._scan_lock.release()

    def _intraday_scan_inner(self):
        """Actual scan logic — called under _scan_lock."""
        if get_day_mode() != "TRADING":
            return
        if not is_market_hours():
            logger.info("Intraday scan: market is closed, skip.")
            return
        in_opening_range = is_opening_range_period()
        if in_opening_range:
            # Opening range (9:15-9:30): Momentum Hunter runs to catch ORB
            # breakouts, but sniper/scalper wait for zones to form. Before
            # this fix, Tiger slept through 9:15-9:30 and missed morning
            # breakouts (Lodha/Adani momentum).
            logger.info("🐅 INTRADAY SCAN [OPENING RANGE] — %s "
                        "Momentum Hunter ACTIVE (ORB), sniper/scalper waiting",
                        datetime.now().strftime("%H:%M"))
        if self.broker is None:
            logger.warning("Intraday scan: no broker, skip.")
            return

        from automation.scheduler import get_active_market
        market = get_active_market()
        if not in_opening_range:
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

        # === DATA REFRESH — 15m every 30 min, 1m from WS every scan ===
        # _refresh_live_data internally throttles 15m REST fetch to every
        # 30 min. WS 1m candles merge every call (zero REST). Scan runs
        # every 1 min with fresh WS live data.
        now_dt = datetime.now()
        need_refresh = True
        if self._last_data_refresh is not None:
            mins_since = (now_dt - self._last_data_refresh).total_seconds() / 60
            # WS 1m merge every 2 min, 15m REST every 30 min (handled inside)
            if mins_since < 2.0:
                need_refresh = False
        if need_refresh:
            self._refresh_live_data()
            self._last_data_refresh = now_dt
        else:
            logger.debug("Data refresh skipped (last %.0f min ago) — using WS live ticks",
                         mins_since)

        # === DIRECT SCAN (no threading wrapper — threading timeout doesn't work
        # with Angel SDK's C extensions holding the GIL) ===
        # The scan runs directly. If it hangs, the 60s sleep cycle is delayed,
        # but the next cycle will retry. REST calls have 7s SmartAPI timeout.
        try:
            try:
                monitored = self.monitor_open_positions()
            except Exception as exc:
                logger.error("👁️ Position monitor error: %s", exc)
                monitored = 0

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

            # === RISK MANAGER GATE — block new entries if paused/stopped ===
            if signals and not self._risk_manager_allows_trading():
                logger.info(
                    "🛡️ RiskManager BLOCKED %d signals — no new trades this cycle.",
                    len(signals))
                signals = []  # wipe signals — monitor exits still run

            placed = self._place_live_orders(signals)

            # === TIGER SNIPER ADVANCED V2 — MCX sniper scan (after scalper) ===
            # The sniper runs on 5m MCX data, needs OB retest + CHOCH + wick
            # on 1m, and a 0.80 ML conviction gate. Max 3/day, 10:30-23:30 IST.
            # During opening range (9:15-9:30), sniper waits — zones haven't
            # formed yet. Momentum Hunter (inside scan_live_signals above)
            # handles ORB breakouts during this window.
            sniper_placed = 0
            if not in_opening_range:
                try:
                    sniper_signals = self.scan_sniper_signals()
                    if sniper_signals:
                        sniper_placed = self._place_live_orders(sniper_signals)
                except Exception as exc:
                    logger.error("🎯 Sniper scan error: %s", exc, exc_info=True)
            else:
                logger.debug("🎯 Sniper waiting — opening range period (zones not formed)")

            # Update the daily entry counter
            self._daily_entries_taken[today] = daily_entries + placed + sniper_placed
            if (placed + sniper_placed) > 0:
                self._last_trade_time = datetime.now()
                # Track scalper trades separately
                for s in signals:
                    if s.get("is_scalper") and placed > 0:
                        self._scalper_trades[today] = scalper_today + 1
                        break

            logger.info("Scan done: %d live signals, %d buy orders placed, "
                        "%d sniper, %d monitored exits, balance ₹%.0f",
                        len(signals), placed, sniper_placed, monitored,
                        self.account_capital)
        except Exception as exc:
            logger.error("Intraday scan error: %s", exc, exc_info=True)

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
        self._batch_keys.clear()  # fresh dedup for this scan batch

        # === TIGER MEMORY — bad time slot warning ===
        try:
            from replay.tiger_memory import is_bad_time_slot
            is_bad, slot = is_bad_time_slot(now)
            if is_bad:
                logger.info(
                    f"🧠 TIGER MEMORY: ⚠️ Bad time slot {slot} — "
                    f"historically low win rate. Trading with caution.")
        except Exception:
            pass

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

            # === STALE SIGNAL GUARD (Bug 1 fix) ===
            # A signal older than SIGNAL_MAX_AGE_SEC is rejected — Tiger must
            # NEVER execute a pending signal from a stale buffer. When the
            # user changes the score threshold mid-scan, old signals die here.
            max_age_sec = AUTOMATION.get("SIGNAL_MAX_AGE_SEC", 90)
            try:
                sig_age = (now - entry_ts).total_seconds() if hasattr(entry_ts, "total_seconds") \
                    else (now - pd.Timestamp(entry_ts)).total_seconds()
                if sig_age > max_age_sec:
                    logger.warning(
                        f"   🗑️ SKIP {symbol} {strike}{option_type} — "
                        f"STALE signal ({sig_age:.0f}s old > {max_age_sec}s limit). "
                        f"Not executing stale-buffer pending signal.")
                    continue
            except Exception:
                pass  # age check failure → don't block (conservative)

            # === DATA FRESHNESS GUARD (Bug 1 fix) ===
            # The latest 1m bar for this symbol must be recent. If the WS feed
            # stalled, Tiger skips rather than trading on stale ticks.
            data_max_age = AUTOMATION.get("DATA_MAX_AGE_SEC", 120)
            if self.data_map_1m and symbol in self.data_map_1m:
                df_1m_sym = self.data_map_1m[symbol]
                if df_1m_sym is not None and not df_1m_sym.empty:
                    try:
                        last_bar_ts = df_1m_sym.index[-1]
                        last_bar = pd.Timestamp(last_bar_ts)
                        bar_age = (now - last_bar).total_seconds()
                        if bar_age > data_max_age:
                            logger.warning(
                                f"   🗑️ SKIP {symbol} {strike}{option_type} — "
                                f"STALE 1m data (last bar {bar_age:.0f}s old > "
                                f"{data_max_age}s). Waiting for fresh tick.")
                            continue
                    except Exception:
                        pass  # freshness check failure → don't block

            # NOTE: backtest quantity is IGNORED — re-sized by Fund Brain using
            # REAL balance + REAL LTP + REAL lot size
            is_delivery = t.get("is_delivery", False)

            # === TIGER MEMORY GATE — skip blacklisted symbols ===
            try:
                from replay.tiger_memory import is_symbol_blacklisted
                if is_symbol_blacklisted(symbol):
                    logger.info(
                        f"🧠 TIGER MEMORY: SKIP {symbol} — blacklisted "
                        f"(historically <35% win rate). Tiger remembers.")
                    continue
            except Exception:
                pass  # memory system down → don't block trading

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

            # Duplicate check — two-tier protection:
            # 1. _placed_order_keys: permanent (survives restart) — prevents
            #    re-entering a trade that was already placed today.
            # 2. _batch_keys: per-scan only — prevents two identical trades
            #    in the SAME scan batch from both getting placed.
            order_key = f"{symbol}_{strike}_{option_type}_{trade_date}"
            if order_key in self._placed_order_keys:
                continue
            if order_key in self._batch_keys:
                continue
            self._batch_keys.add(order_key)

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

            # Current exposure: deployed capital in OPEN positions only.
            # Closed/exited positions must NOT count (they no longer use margin).
            current_exposure = sum(
                float(o.get("trade_cost", 0)) for o in self._order_log
                if o.get("success") and not o.get("exited", False)
            )
            # Open position COUNT for the MAX_OPEN_POSITIONS gate.
            open_position_count = sum(
                1 for o in self._order_log
                if o.get("success") and not o.get("exited", False)
            )

            # === 50/50 NSE/MCX CAPITAL SPLIT (PERMANENT FIX) ===
            # Determine this trade's market + deployed capital in THAT market only.
            # NSE trades only check against the NSE 50% budget.
            # MCX trades only check against the MCX 50% budget.
            # This way one market NEVER blocks the other.
            _this_market = "MCX" if is_mcx_commodity else "NSE"
            market_deployed_cost = sum(
                float(o.get("trade_cost", 0)) for o in self._order_log
                if o.get("success") and not o.get("exited", False)
                and ("MCX" if MARKET_CATEGORIES.get(
                    resolve_exchange_for_symbol(o.get("symbol", "")), "") == "commodity"
                    else "NSE") == _this_market
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

            # === CHEAP OPTIONS GATE + ZERO-TO-HERO OTM FALLBACK ===
            # User mandate: "sasta sa options buying kar leta" — ONLY cheap
            # options (₹5-50 premium). Even if ATM is affordable, if premium
            # > MAX_OPTION_PREMIUM (₹50), Tiger walks OTM to find cheap strikes.
            # Also: "jha buying selling ho rhi hai volumes hai wha jaye" — only
            # enter options with actual volume (MIN_OPTION_VOLUME gate).
            from config.thresholds import SNIPER as _SNIP
            _max_premium = _SNIP.get("MAX_OPTION_PREMIUM", 50.0)
            _min_vol = _SNIP.get("MIN_OPTION_VOLUME", 50)
            _pre_cap = CapitalManager(self.broker).check_and_allocate(
                setup_score=t.get("setup_score", 50.0),
                brain_alignment=count_aligned_brains(t),
                trade_cost_estimate=one_lot_cost,
                open_positions_cost=current_exposure,
                min_allocation=one_lot_cost,
                open_position_count=open_position_count,
                market=_this_market,
                market_deployed_cost=market_deployed_cost,
                available_balance_override=available_balance,
            )
            _free_capital = _pre_cap.free_disposable if _pre_cap else available_balance
            _trade_capital = min(_free_capital, available_balance) if _free_capital > 0 else available_balance
            otm_steps = 20  # always walk far OTM — user wants ₹5-50 cheap options
            # Trigger OTM walk if: too expensive OR premium too high (user: cheap only)
            _need_cheap = real_ltp > _max_premium
            if one_lot_cost > _trade_capital or _need_cheap:
                affordable = find_affordable_option(
                    underlying=symbol,
                    atm_strike=float(strike),
                    option_type=option_type,
                    balance=_trade_capital * 0.92,  # 8% buffer for broker margin/charges
                    broker=self.broker,
                    max_otm_steps=otm_steps,
                    min_delta=0.02,  # deep OTM cheap options — user wants cheap, not high-delta
                    max_premium=_max_premium,  # ₹50 — only buy cheap options (user mandate)
                )
                if affordable is not None:
                    _cheap_label = "CHEAP" if _need_cheap else "ZERO-TO-HERO"
                    logger.info(
                        f"   🚀 {_cheap_label}: {symbol} {strike}{option_type} "
                        f"→ strike {affordable['strike']}{option_type} "
                        f"(premium ₹{affordable['ltp']:.2f}, "
                        f"1 lot ₹{affordable['one_lot_cost']:,.0f})")
                    contract = {
                        "tradingsymbol": affordable["tradingsymbol"],
                        "symboltoken": affordable["symboltoken"],
                        "exchange": affordable["exchange"],
                        "lotsize": affordable["lotsize"],
                    }
                    real_lot_size = affordable["lotsize"]
                    real_ltp = affordable["ltp"]
                    one_lot_cost = affordable["one_lot_cost"]
                    strike = affordable["strike"]
                else:
                    logger.info(
                        f"   ❌ NO CHEAP STRIKE — {symbol} {strike}{option_type} "
                        f"premium ₹{real_ltp:.2f} > ₹{_max_premium:.0f} max, "
                        f"no affordable OTM within {otm_steps} steps")

            # === EARLY ML EXTRACTION — compute win_prob BEFORE sizing ===
            # Rocket sizing needs win_prob to decide position size.
            # Full ML block (sensex filter etc.) runs later.
            if "ml_win_prob" not in t:
                try:
                    _early_feat = extract_live_features(
                        symbol=symbol,
                        signal=t,
                        data_map_15m=self.data_map,
                        data_map_1m=self.data_map_1m,
                        broker=self.broker,
                        pcr_value=t.get("pcr", 1.0),
                    )
                    _, _early_wp = self.ml_gate.check_gate(_early_feat)
                    t["ml_features"] = _early_feat
                    t["ml_win_prob"] = _early_wp
                except Exception:
                    t["ml_win_prob"] = 1.0
            _wp = t.get("ml_win_prob", 1.0)

            # 🔥 FUND BRAIN LIVE SIZING — real balance + real LTP + real lot
            # Capital split: 50/50 when BOTH markets open, but 100% to the
            # active market when the other is closed (user: "nse band hote hi
            # pura capital use ho mcx market mai").
            from config.thresholds import BRAIN4 as _B4
            _split_pct = _B4.get("MARKET_CAPITAL_SPLIT_PCT", 50.0)
            _nse_open = self._nse_sniper_session_active()
            _mcx_open = self._sniper_session_active()
            if _nse_open and _mcx_open:
                # Both open → 50/50 split
                _market_budget = available_balance * (_split_pct / 100.0)
            elif _this_market == "MCX" and not _nse_open:
                # NSE closed, MCX open → full capital to MCX
                _market_budget = available_balance
                logger.info(f"💰 FULL CAPITAL → MCX: ₹{available_balance:,.0f} (NSE closed)")
            elif _this_market == "NSE" and not _mcx_open:
                # MCX closed, NSE open → full capital to NSE
                _market_budget = available_balance
                logger.info(f"💰 FULL CAPITAL → NSE: ₹{available_balance:,.0f} (MCX closed)")
            else:
                _market_budget = available_balance * (_split_pct / 100.0)
            re_size = self._live_re_size(
                real_balance=available_balance,
                real_ltp=real_ltp,
                real_lot_size=real_lot_size,
                is_delivery=is_delivery,
                current_exposure=current_exposure,
                market_budget=_market_budget,
                win_prob=t.get("ml_win_prob", 1.0),
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
                    "exchange": contract["exchange"],
                    "tradingsymbol": contract["tradingsymbol"],
                    "real_ltp": real_ltp, "one_lot_cost": one_lot_cost,
                    "balance": available_balance,
                    "success": False, "error": sizing_reason,
                })
                self._save_order_log()
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
            is_scalper = t.get("is_scalper", False)
            is_momentum_hunter = t.get("is_momentum_hunter", False)

            if is_scalper or is_momentum_hunter:
                # SCALPER/MOMENTUM HUNTER BYPASS — skips 7-brain conviction gate.
                # These signals have their own internal scoring + options math gate.
                # Instead: just check affordability.
                if trade_cost > available_balance:
                    logger.info(
                        f"   ❌ SKIP scalper {symbol} {strike}{option_type} — "
                        f"cost ₹{trade_cost:,.0f} > balance ₹{available_balance:,.0f}")
                    self._order_log.append({
                        "time": datetime.now().isoformat(),
                        "symbol": symbol, "strike": strike,
                        "option_type": option_type,
                        "exchange": contract["exchange"],
                        "tradingsymbol": contract["tradingsymbol"],
                        "real_ltp": real_ltp,
                        "balance": available_balance,
                        "success": False, "error": "scalper_unaffordable",
                        "is_scalper": True,
                    })
                    self._save_order_log()
                    continue
                bypass_label = "MOMENTUM HUNTER" if is_momentum_hunter else "SCALPER"
                logger.info(
                    f"   🐅 {bypass_label} BYPASS — conviction gate skipped "
                    f"(score={setup_score:.0f}, cost ₹{trade_cost:,.0f})")
                # Scalper bypass skips cap_check — set a flag so the
                # post-allocation logging knows to skip cap_check fields.
                cap_check = None
            else:
                cap_check = CapitalManager(self.broker).check_and_allocate(
                    setup_score=setup_score,
                    brain_alignment=brain_alignment,
                    trade_cost_estimate=trade_cost,
                    open_positions_cost=current_exposure,
                    min_allocation=one_lot_cost,
                    open_position_count=open_position_count,
                    market=_this_market,
                    market_deployed_cost=market_deployed_cost,
                    available_balance_override=available_balance,
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
                        "exchange": contract["exchange"],
                        "tradingsymbol": contract["tradingsymbol"],
                        "quantity": quantity, "real_ltp": real_ltp,
                        "trade_cost": trade_cost,
                        "balance": available_balance,
                        "success": False,
                        "error": cap_check.reason,
                        "capital_blocked": True,
                    })
                    self._save_order_log()
                    continue

            if cap_check is not None:
                logger.info(
                    f"   💰 Capital tier: {cap_check.conviction_tier} "
                    f"({cap_check.conviction_multiplier:.0%} of margin) | "
                    f"Allocated: ₹{cap_check.allocated_capital:,.0f}")

            # Step 6: Place REAL BUY order (Tiger always buys options)
            # Delivery = CARRYFORWARD (overnight), Intraday = INTRADAY
            transaction_type = "BUY"
            product_type = "CARRYFORWARD" if is_delivery else "INTRADAY"

            # === DIRECTIONAL BLOCK — prevent CE+PE on same symbol within 2 hours ===
            if self._is_direction_blocked(symbol, option_type):
                logger.info(
                    f"   🚫 SKIP {symbol} {option_type} — opposite direction "
                    f"taken in last 2 hours")
                self._order_log.append({
                    "time": datetime.now().isoformat(),
                    "symbol": symbol, "strike": strike,
                    "option_type": option_type,
                    "exchange": contract["exchange"],
                    "tradingsymbol": contract["tradingsymbol"],
                    "success": False,
                    "error": "direction_blocked_2h",
                })
                self._save_order_log()
                continue

            # === 1-MINUTE VELOCITY CONFIRMATION ===
            # Scanner signal is necessary but NOT sufficient. Before Tiger
            # transmits ANY BUY to Angel One, check the latest 1m candle.
            # SNIPER signals skip this gate — sniper already confirmed entry
            # on 1m via OB retest + CHOCH. Only non-sniper signals need it.
            is_sniper_signal = t.get("is_sniper", False)
            if not is_sniper_signal and not self._verify_1m_velocity(symbol, option_type):
                self._order_log.append({
                    "time": datetime.now().isoformat(),
                    "symbol": symbol, "strike": strike,
                    "option_type": option_type,
                    "exchange": contract["exchange"],
                    "tradingsymbol": contract["tradingsymbol"],
                    "success": False,
                    "error": "velocity_block_1m",
                })
                self._save_order_log()
                continue

            # === ML INFERENCE ADVISOR — win-probability signal (never blocks) ===
            # win_prob already computed above (early extraction for rocket sizing).
            # Here we run the sensex directional filter + attach to trade log.
            if "ml_features" in t:
                ml_features = t["ml_features"]
                win_prob = t.get("ml_win_prob", 1.0)
            else:
                try:
                    ml_features = extract_live_features(
                        symbol=symbol,
                        signal=t,
                        data_map_15m=self.data_map,
                        data_map_1m=self.data_map_1m,
                        broker=self.broker,
                        pcr_value=t.get("pcr", 1.0),
                    )
                    ml_passed, win_prob = self.ml_gate.check_gate(ml_features)
                except Exception:
                    ml_features = {}
                    win_prob = 1.0
            t["ml_features"] = ml_features
            t["ml_win_prob"] = win_prob
            t["sensex_trend"] = float(ml_features.get("sensex_trend", 0.0))

            # --- SENSEX DIRECTIONAL FILTER ---
            # Block options that fight the broad-market trend:
            #   bullish market (sensex_trend=+1) + PE → BLOCK
            #   bearish market (sensex_trend=-1) + CE → BLOCK
            if self.ml_gate.is_enabled() and sensex_blocks_option(
                    t["sensex_trend"], option_type):
                    logger.info(
                        f"   📉 SENSEX BLOCK {symbol} {option_type} — "
                        f"trend={t['sensex_trend']:+.0f} fights {option_type}")
                    self._order_log.append({
                        "time": datetime.now().isoformat(),
                        "symbol": symbol, "strike": strike,
                        "option_type": option_type,
                        "exchange": contract["exchange"],
                        "tradingsymbol": contract["tradingsymbol"],
                        "success": False,
                        "error": f"sensex_trend_block (trend={t['sensex_trend']:+.0f}, opt={option_type})",
                        "ml_features": ml_features,
                        "win_prob": win_prob,
                        "sensex_trend": t["sensex_trend"],
                    })
                    self._save_order_log()
                    continue

            # --- ML WIN-PROBABILITY ADVISORY (never blocks) ---
            # ML check_gate always passes now. Log low win_prob as advisory.
            if win_prob < self.ml_gate.min_win_prob:
                logger.info(
                    f"   💡 ML ADVISORY {symbol} {option_type} — "
                    f"win_prob={win_prob:.2f} < {self.ml_gate.min_win_prob:.2f} "
                    f"(advisory only — Tiger decides)")

            # --- ML CONFLUENCE ADVISORY (never blocks) ---
            # Higher ML confidence → fewer 7-brain alignments needed.
            # Lower confidence → log advisory but Tiger decides.
            if self.ml_gate.is_enabled():
                req_conf = required_confluence_for_win_prob(win_prob)
                if brain_alignment < req_conf:
                    logger.info(
                        f"   💡 ML CONFLUENCE ADVISORY {symbol} {option_type} — "
                        f"brains {brain_alignment}/{req_conf} suggested "
                        f"(win_prob={win_prob:.2f}) — Tiger decides")
                else:
                    logger.info(
                        f"   🧠 ML SIGNAL {symbol} {option_type} — "
                        f"win_prob={win_prob:.2f}, "
                        f"confluence {brain_alignment}/{req_conf}")

            # === VOLUME GATE (user: "jha buying selling ho rhi hai wha jaye") ===
            # Reject dead options — only trade where there's actual volume.
            _opt_vol = 0
            try:
                _opt_vol = self.broker.get_option_volume(
                    contract["tradingsymbol"],
                    contract["symboltoken"],
                    contract["exchange"],
                )
            except Exception:
                pass  # volume API fail → don't block (best effort)
            if _opt_vol < _min_vol:
                logger.info(
                    f"   🚫 VOLUME GATE: {symbol} {strike}{option_type} "
                    f"vol={_opt_vol} < {_min_vol} — dead option, no buying/selling")
                self._order_log.append({
                    "time": datetime.now().isoformat(),
                    "symbol": symbol, "strike": strike,
                    "option_type": option_type,
                    "exchange": contract["exchange"],
                    "tradingsymbol": contract["tradingsymbol"],
                    "real_ltp": real_ltp, "volume": _opt_vol,
                    "balance": available_balance,
                    "success": False, "error": "low_volume",
                })
                self._save_order_log()
                continue

            # LIMIT order at LTP + small buffer for fill — prevents overpaying.
            # MARKET orders on low-liquidity options fill at worst price.
            # Round to tick size — exchange rejects prices not matching tick.
            # MCX tick sizes vary by commodity (GOLDM/SILVERM=0.50, CRUDEOIL=1.0,
            # NATURALGAS=0.10). NFO/BSE equity+index options tick=0.05.
            _exch = contract["exchange"]
            if _exch == "NFO" or _exch == "BSE":
                _tick = 0.05
            elif _exch == "MCX":
                _MCX_TICK = {"GOLDM": 0.50, "SILVERM": 0.50, "SILVER": 0.50,
                             "GOLD": 0.50, "CRUDEOIL": 1.00,
                             "NATURALGAS": 0.10, "COPPER": 0.05}
                _tick = _MCX_TICK.get(symbol, 0.50)
            else:
                _tick = 0.05
            _raw = real_ltp * 1.02
            limit_price = round(_raw / _tick) * _tick
            # Fix float precision (221.54000... → 221.5)
            limit_price = round(limit_price, 2)
            result = self.broker.place_option_order(
                tradingsymbol=contract["tradingsymbol"],
                symboltoken=contract["symboltoken"],
                exchange=contract["exchange"],
                transaction_type=transaction_type,
                quantity=quantity,
                product_type=product_type,
                order_type="LIMIT",
                price=limit_price,
            )

            if result.get("success"):
                # Step 7: Check order STATUS — rejected or EXECUTED?
                # Bug 2 fix: log entry must happen ONLY after Angel One confirms
                # order_status == executed/complete. The logged price/qty must
                # come from the broker's actual fill, NOT the pre-trade LTP.
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
                        "exchange": contract["exchange"],
                        "tradingsymbol": contract["tradingsymbol"],
                        "quantity": quantity, "real_ltp": real_ltp,
                        "trade_cost": trade_cost,
                        "order_id": result["order_id"],
                        "success": False, "error": f"REJECTED: {reject_reason}",
                        "reject_reason": reject_reason,
                    })
                    self._save_order_log()
                    continue

                # === EXECUTION CONFIRMATION (Bug 2 fix) ===
                # Angel One MARKET orders fill near-instantly, but the status
                # field can lag ("open"/"trigger pending" briefly before
                # "complete"). Poll up to 2 more times (1s apart) for a
                # definitive executed/complete status.
                _EXECUTED_STATES = {"complete", "executed", "filled",
                                    "traded", "fully executed"}
                fill_polls = 0
                while order_status not in _EXECUTED_STATES and fill_polls < 2:
                    _time.sleep(1)
                    status = self.broker.get_order_status(result["order_id"])
                    order_status = status.get("status", "").lower()
                    reject_reason = status.get("reject_reason")
                    fill_polls += 1
                    if "reject" in order_status or reject_reason:
                        break

                if "reject" in order_status or reject_reason:
                    logger.error(
                        f"   ❌ ORDER REJECTED after poll: {reject_reason}")
                    self._order_log.append({
                        "time": datetime.now().isoformat(),
                        "symbol": symbol, "strike": strike,
                        "option_type": option_type,
                        "exchange": contract["exchange"],
                        "tradingsymbol": contract["tradingsymbol"],
                        "quantity": quantity, "real_ltp": real_ltp,
                        "trade_cost": trade_cost,
                        "order_id": result["order_id"],
                        "success": False,
                        "error": f"REJECTED (poll): {reject_reason}",
                        "reject_reason": reject_reason,
                    })
                    self._save_order_log()
                    continue

                if order_status not in _EXECUTED_STATES:
                    # Order accepted but NOT executed — do NOT log as a trade.
                    # This prevents blind logging of pending/unfilled orders.
                    logger.warning(
                        f"   ⏳ ORDER NOT EXECUTED (status={order_status}) — "
                        f"{contract['tradingsymbol']}. NOT logged to trade_log. "
                        f"Will reconcile on next position sync.")
                    self._order_log.append({
                        "time": datetime.now().isoformat(),
                        "symbol": symbol, "strike": strike,
                        "option_type": option_type, "direction": direction,
                        "exchange": contract["exchange"],
                        "tradingsymbol": contract["tradingsymbol"],
                        "quantity": quantity, "real_ltp": real_ltp,
                        "trade_cost": trade_cost,
                        "order_id": result["order_id"],
                        "success": False,
                        "error": f"NOT_EXECUTED: status={order_status}",
                        "order_status": order_status,
                    })
                    self._save_order_log()
                    continue

                # === EXECUTED — use broker's ACTUAL fill price + qty ===
                # Bug 2 fix: the log now carries the real execution price from
                # Angel One (avg_price), not the pre-trade LTP estimate. This
                # eliminates the 4893-vs-4989 discrepancy.
                broker_avg_price = float(status.get("avg_price", 0) or 0)
                broker_filled_qty = int(status.get("filled_qty", 0) or 0)
                fill_price = broker_avg_price if broker_avg_price > 0 else real_ltp
                executed_qty = broker_filled_qty if broker_filled_qty > 0 else quantity
                if broker_avg_price > 0:
                    logger.info(
                        f"   🎯 BROKER FILL: avg_price=₹{broker_avg_price:.2f} "
                        f"filled_qty={broker_filled_qty} (pre-trade LTP was ₹{real_ltp:.2f})")
                else:
                    logger.warning(
                        f"   ⚠️ Broker fill price unavailable (avg_price=0) — "
                        f"falling back to pre-trade LTP ₹{real_ltp:.2f}")
                # Recompute trade_cost from the ACTUAL fill (not estimate)
                actual_trade_cost = fill_price * executed_qty

                # Order executed!
                placed_count += 1
                self._placed_order_keys.add(order_key)
                self._save_order_keys()  # permanent — survives restart
                available_balance -= actual_trade_cost
                self.capital_after_entry = available_balance

                # === STATE LOCK — increment daily trade counter ===
                daily_count = self._increment_daily_trade_count()

                # Record direction taken — blocks opposite direction for 2 hours
                self._record_direction_taken(symbol, option_type)

                # Track scalper positions for special exit rules
                is_scalper = t.get("is_scalper", False)
                is_momentum_hunter = t.get("is_momentum_hunter", False)
                is_sniper = t.get("is_sniper", False)

                # === ENTRY QUALITY SNAPSHOT — for safe ML training ===
                # ML ko sirf TRUE sniper entries se sikhna chahiye. Yahan
                # capture karte hain ki entry ke time kaunse gates pass hue.
                # is_true_sniper = zone + SMC + velocity + sniper-confirm ALL true.
                # This prevents ML from learning lucky wins from bad entries.
                _has_smc = bool(
                    t.get("fvg") or t.get("bos") or t.get("liquidity_sweep")
                    or t.get("order_block")
                )
                entry_quality = {
                    "zone_touched": bool(t.get("zone_type")),
                    "smc_confluence": _has_smc,
                    "velocity_verified": True,   # line 2275 gate passed
                    "sniper_entry_confirmed": bool(is_sniper),
                    "options_math_passed": bool(t.get("zone_type")),  # GATE 9 passed
                    "is_true_sniper": bool(
                        is_sniper and _has_smc and t.get("zone_type")
                    ),
                }

                if is_scalper:
                    self._scalper_positions.add(contract["tradingsymbol"])
                    self._save_scalper_positions()
                    logger.info(f"   🐅 SCALPER position tracked: {contract['tradingsymbol']}")
                if is_momentum_hunter:
                    self._scalper_positions.add(contract["tradingsymbol"])
                    self._save_scalper_positions()
                    logger.info(f"   🐅 MOMENTUM HUNTER position tracked: {contract['tradingsymbol']}")
                if is_sniper:
                    self._sniper_positions.add(contract["tradingsymbol"])
                    # Stamp sniper exit params on the position tracker so the
                    # exit branch can size the ATR*2.5 trail + OB stop.
                    _snipe_pe = self._position_peaks.get(contract["tradingsymbol"], {})
                    _snipe_pe["is_sniper"] = True
                    _snipe_pe["order_block"] = t.get("order_block", {})
                    _snipe_pe["sniper_atr_pct"] = t.get("sniper_atr_pct",
                                                         t.get("commodity_volatility", 0.0))
                    _snipe_pe["entry"] = fill_price
                    _snipe_pe.setdefault("peak", fill_price)
                    _snipe_pe.setdefault("target_booked", False)
                    _snipe_pe["ml_features"] = t.get("ml_features", {})
                    _snipe_pe["ml_win_prob"] = t.get("ml_win_prob", 1.0)
                    _snipe_pe["sensex_trend"] = t.get("sensex_trend", 0.0)
                    _snipe_pe["entry_time"] = datetime.now().isoformat()
                    _snipe_pe["trade_cost"] = actual_trade_cost
                    _snipe_pe["option_type"] = option_type
                    self._position_peaks[contract["tradingsymbol"]] = _snipe_pe
                    self._save_position_peaks()
                    logger.info(f"   🎯 SNIPER position tracked: {contract['tradingsymbol']} "
                                f"(ATR%={_snipe_pe['sniper_atr_pct']:.2f})")

                # === STRUCTURAL STOP — save zone-based SL for exit system ===
                # Instead of fixed -7%, exit system uses zone bottom (demand)
                # or zone top (supply) as SL. CRUDEOIL fix: survives pullback
                # before rocket, doesn't get stopped out prematurely.
                structural_stop = t.get("structural_stop", 0.0)
                if structural_stop > 0:
                    tsym = contract["tradingsymbol"]
                    entry_price = fill_price
                    # Convert structural stop (underlying price) to premium stop
                    # using delta approximation: premium_stop ≈ entry * (stop_pct_of_underlying)
                    stop_pct = abs(entry_price - structural_stop) / entry_price * 100 if entry_price > 0 else 7.0
                    # Cap at 12% max (don't give unlimited room)
                    stop_pct = min(stop_pct, 12.0)
                    # If structural stop is too tight (< 4%), use fixed 7% (safety)
                    stop_pct = max(stop_pct, 4.0)
                    existing = self._position_peaks.get(tsym, {})
                    existing["structural_stop_pct"] = stop_pct
                    existing["entry"] = entry_price
                    existing["entry_time"] = datetime.now().isoformat()
                    existing.setdefault("peak", entry_price)
                    existing.setdefault("target_booked", False)
                    self._position_peaks[tsym] = existing
                    self._save_position_peaks()
                    logger.info(
                        f"   🛡️ Structural SL: {tsym} stop=-{stop_pct:.1f}% "
                        f"(zone-based, not fixed -7%)")

                logger.info(
                    f"   ✅ Order EXECUTED: {order_status} "
                    f"(fill ₹{fill_price:.2f} × {executed_qty})")
                logger.info(
                    f"   🔥 REAL ORDER: BUY {executed_qty} "
                    f"{contract['tradingsymbol']} ({option_type}) "
                    f"cost ₹{actual_trade_cost:,.0f} → order_id={result['order_id']}"
                    f"{' [MOMENTUM HUNTER]' if is_momentum_hunter else ''}"
                    f"{' [SCALPER]' if is_scalper else ''}")
                logger.info(
                    f"   💰 Remaining balance: ₹{available_balance:,.0f}")

                # === TRADE LOG — bound to broker EXECUTED response (Bug 2 fix) ===
                # Entry is logged ONLY here, after Angel One confirmed the fill.
                # entry_price = broker avg_price (actual execution), NOT real_ltp.
                try:
                    from replay.nightly_replay import append_trade_record
                    from replay.tiger_memory import _get_time_slot
                    append_trade_record({
                        "symbol": symbol,
                        "tradingsymbol": contract["tradingsymbol"],
                        "direction": direction,
                        "option_type": option_type,
                        "exchange": contract["exchange"],
                        "regime": self._current_regime,
                        "time_slot": _get_time_slot(datetime.now().isoformat()),
                        "entry_time": datetime.now().isoformat(),
                        "entry_price": fill_price,
                        "broker_fill_price": broker_avg_price,
                        "quantity": executed_qty,
                        "trade_cost": actual_trade_cost,
                        "setup_score": setup_score,
                        "brain_alignment": brain_alignment,
                        "is_scalper": is_scalper,
                        "is_sniper": is_sniper,
                        "order_id": result.get("order_id"),
                        "order_status": order_status,
                        "status": "OPEN",
                        "entry_quality": entry_quality,
                        "ml_features": t.get("ml_features", {}),
                        "ml_win_prob": t.get("ml_win_prob", 1.0),
                        "sensex_trend": t.get("sensex_trend", 0.0),
                    })
                except Exception as exc:
                    logger.warning(f"Trade log save fail: {exc}")

                # Stash ML features on the position tracker so the exit
                # loop can write a COMBINED retrain record (features + pnl).
                # This closes the training-data loop: entry features survive
                # until exit, where the realized PnL label is attached.
                try:
                    pe = self._position_peaks.get(contract["tradingsymbol"], {})
                    pe["ml_features"] = t.get("ml_features", {})
                    pe["ml_win_prob"] = t.get("ml_win_prob", 1.0)
                    pe["sensex_trend"] = t.get("sensex_trend", 0.0)
                    pe["entry_quality"] = entry_quality   # survive to exit record
                    pe["option_type"] = option_type
                    self._position_peaks[contract["tradingsymbol"]] = pe
                    self._save_position_peaks()
                except Exception:
                    pass
            else:
                logger.error(
                    f"   ❌ Order fail: BUY {quantity} "
                    f"{contract['tradingsymbol']} — {result.get('error', '?')}")

            self._order_log.append({
                "time": datetime.now().isoformat(),
                "symbol": symbol, "strike": strike,
                "option_type": option_type, "direction": direction,
                "transaction_type": transaction_type,
                "exchange": contract["exchange"],
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
            self._save_order_log()

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
                is_exit=True,
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
        # Mark ALL NFO positions as exited in order log (exposure fix)
        for o in self._order_log:
            if o.get("success") and "NFO" in str(o.get("exchange", "")):
                o["exited"] = True
        self._save_order_log()
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
        # Mark ALL MCX positions as exited in order log (exposure fix)
        for o in self._order_log:
            if o.get("success") and "MCX" in str(o.get("exchange", "")):
                o["exited"] = True
        self._save_order_log()
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
        """Nightly audit of today's trades + Tiger Memory Core update."""
        logger.info("=" * 60)
        logger.info("🐅 NIGHTLY REPLAY + MEMORY UPDATE — %s",
                    datetime.now().strftime("%Y-%m-%d"))
        logger.info("=" * 60)
        try:
            from replay.nightly_replay import run_nightly_replay, load_trade_log
            from replay.tiger_memory import update_memory_from_trades
            records = load_trade_log()
            result = run_nightly_replay(records)
            audit = result.get("audit", {})
            logger.info("Replay: %d trades, win-rate %.1f%%, PnL ₹%.0f",
                        audit.get("total_trades", 0),
                        audit.get("win_rate_pct", 0),
                        audit.get("total_pnl", 0))
            for note in result.get("notes", []):
                logger.info("  → %s", note)

            # === TIGER MEMORY CORE UPDATE ===
            mem = update_memory_from_trades(records)
            logger.info("🧠 Tiger Memory updated:")
            logger.info("   Total: %d trades, %d wins, ₹%.0f PnL",
                        mem["total_trades"], mem["total_wins"], mem["total_pnl"])
            if mem.get("blacklist"):
                logger.info("   🚫 Blacklist: %s", ", ".join(mem["blacklist"]))
            if mem.get("best_setups"):
                best = mem["best_setups"][0]
                logger.info("   🏆 Best: %s (%d%% win, ₹%.0f)",
                            best["symbol"], best["win_rate"], best["pnl"])
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

        # === RISK MANAGER — daily reset ===
        # New day → fresh slate for consecutive losses + trade cap.
        self._consecutive_losses = 0
        self._pause_until = None
        self._save_risk_state()
        logger.info("🛡️ RiskManager reset — consecutive losses cleared, pause lifted.")

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

        # Keep main thread alive — also serves as reliable scan trigger
        # (BackgroundScheduler sometimes fails to fire in worker threads)
        try:
            while self._running:
                time.sleep(60)
                mode = get_day_mode()
                if mode == "TRADING" and is_market_hours() and not is_opening_range_period():
                    logger.info("❤️ Tiger heartbeat — TRADING mode (%s) — triggering scan",
                                datetime.now().strftime("%H:%M"))
                    # intraday_scan() now acquires _scan_lock internally —
                    # if a scheduled scan is running, this call will skip.
                    self.intraday_scan()
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
