"""
Tiger Brain V6+V7 — Automation Scheduler (Section 34)
========================================================
24x7 cycle: Mon-Fri poora trading cycle khud chalta hai, Saturday
low-power "watch mode" (koi trading nahi, sirf background monitoring),
Sunday poori tarah OFF (deep sleep).

⚠️ IMPORTANT — apscheduler is sandbox mein install nahi hai (internet
disabled), isliye is file ka actual scheduling part (TigerBrainScheduler
class) yahan TEST NAHI hua hai. Apne server pe `pip install -r
requirements.txt` chalane ke baad ye khud verify karna.

Jo cheez yahan properly test hui hai: din ka "mode" decide karne wala
logic (is_trading_day, get_day_mode, etc.) — ye pure Python hai, koi
extra dependency nahi chahiye, aur neeche test bhi hua hai.
"""

import sys
import logging
from datetime import datetime, time

logger = logging.getLogger("tiger_brain.scheduler")

try:
    from config.thresholds import AUTOMATION
    from automation.holidays import is_market_holiday
except ImportError:
    raise ImportError("Repo ROOT se chalao, 'automation/' ke andar se nahi.")


WEEKDAY_MAP = {0: "MON", 1: "TUE", 2: "WED", 3: "THU", 4: "FRI", 5: "SAT", 6: "SUN"}


# ============================================================
# DAY-MODE LOGIC (koi extra dependency nahi, pure Python)
# ============================================================

def get_day_mode(check_date: datetime = None) -> str:
    """
    Section 34 ka schedule decide karta hai — ab weekday RULE ke saath
    saath declared NSE holidays bhi check karta hai (automation/holidays.py).

    Returns:
        'TRADING' | 'WATCH_ONLY' | 'OFF'
    """
    check_date = check_date or datetime.now()
    day_name = WEEKDAY_MAP[check_date.weekday()]

    # Pehle holiday check — agar declared holiday hai, chahe weekday
    # "TRADING" list mein ho, phir bhi OFF treat karna hai
    is_holiday, holiday_name = is_market_holiday(check_date)
    if is_holiday:
        return "OFF"

    if day_name in AUTOMATION["TRADING_DAYS"]:
        return "TRADING"
    elif day_name in AUTOMATION["WATCH_ONLY_DAYS"]:
        return "WATCH_ONLY"
    elif day_name in AUTOMATION["OFF_DAYS"]:
        return "OFF"
    else:
        return "OFF"


def is_trading_day(check_date: datetime = None) -> bool:
    return get_day_mode(check_date) == "TRADING"


def is_market_hours(check_time: datetime = None) -> bool:
    """Market open hai ya nahi — NSE (09:15-15:15) YA MCX (09:00-23:15).

    MCX opens at 09:00 (before NSE). Both markets overlap during
    09:15-15:15 — Tiger scans NSE + MCX simultaneously.
    """
    check_time = check_time or datetime.now()
    current = check_time.time()

    nse_open_h, nse_open_m = map(int, AUTOMATION["MARKET_OPEN_TIME"].split(":"))
    nse_off_h, nse_off_m = map(int, AUTOMATION["NSE_SQUARE_OFF_TIME"].split(":"))
    nse_open = time(nse_open_h, nse_open_m)
    nse_close = time(nse_off_h, nse_off_m)

    mcx_open_h, mcx_open_m = map(int, AUTOMATION["MCX_OPEN_TIME"].split(":"))
    mcx_close_h, mcx_close_m = map(int, AUTOMATION["MCX_CLOSE_TIME"].split(":"))
    mcx_open = time(mcx_open_h, mcx_open_m)
    mcx_close = time(mcx_close_h, mcx_close_m)

    nse_active = nse_open <= current <= nse_close
    mcx_active = mcx_open <= current <= mcx_close
    return nse_active or mcx_active


def get_active_market(check_time: datetime = None) -> str:
    """Return 'NSE+MCX', 'NSE', 'MCX', or 'CLOSED' for the current time.

    NSE+MCX:  09:15 - 15:15 (both markets active, simultaneous scan)
    MCX:      09:00 - 09:15 (MCX only, before NSE opens)
    MCX:      15:15 - 23:15 (MCX only, after NSE square-off)
    Else:     CLOSED
    """
    check_time = check_time or datetime.now()
    current = check_time.time()

    nse_open_h, nse_open_m = map(int, AUTOMATION["MARKET_OPEN_TIME"].split(":"))
    nse_off_h, nse_off_m = map(int, AUTOMATION["NSE_SQUARE_OFF_TIME"].split(":"))
    mcx_open_h, mcx_open_m = map(int, AUTOMATION["MCX_OPEN_TIME"].split(":"))
    mcx_close_h, mcx_close_m = map(int, AUTOMATION["MCX_CLOSE_TIME"].split(":"))

    nse_active = time(nse_open_h, nse_open_m) <= current <= time(nse_off_h, nse_off_m)
    mcx_active = time(mcx_open_h, mcx_open_m) <= current <= time(mcx_close_h, mcx_close_m)

    if nse_active and mcx_active:
        return "NSE+MCX"
    if nse_active:
        return "NSE"
    if mcx_active:
        return "MCX"
    return "CLOSED"


def is_nse_hours(check_time: datetime = None) -> bool:
    """NSE session active? (09:15-15:15)."""
    market = get_active_market(check_time)
    return market in ("NSE", "NSE+MCX")


def is_mcx_hours(check_time: datetime = None) -> bool:
    """MCX commodity session active hai? (09:00-23:15)."""
    market = get_active_market(check_time)
    return market in ("MCX", "NSE+MCX")


def is_opening_range_period(check_time: datetime = None) -> bool:
    """Opening Range Wait — market open ke pehle N minutes no trade.

    Handles BOTH market opens:
      MCX opens at 09:00 → wait N minutes (09:00-09:15)
      NSE opens at 09:15 → wait N minutes (09:15-09:30)
    Combined: no trades until 09:30 (both opening ranges cleared).
    """
    check_time = check_time or datetime.now()
    wait = AUTOMATION["OPENING_RANGE_WAIT_MINUTES"]

    for open_key in ("MARKET_OPEN_TIME", "MCX_OPEN_TIME"):
        open_h, open_m = map(int, AUTOMATION[open_key].split(":"))
        open_dt = check_time.replace(
            hour=open_h, minute=open_m, second=0, microsecond=0)
        minutes_since_open = (check_time - open_dt).total_seconds() / 60
        if 0 <= minutes_since_open < wait:
            return True
    return False


# ============================================================
# SCHEDULER SETUP (apscheduler chahiye — sandbox mein untested)
# ============================================================

class TigerBrainScheduler:
    """
    Poora 24x7 automation cycle wire karta hai. Har job pehle apna
    day-mode check karta hai — agar aaj TRADING day nahi hai, job khud
    ko skip kar leta hai.

    Usage (apne server pe):
        scheduler = TigerBrainScheduler()
        scheduler.setup_jobs(
            pre_market_fn=my_pre_market_function,
            market_open_fn=my_market_open_function,
            intraday_fn=my_intraday_scan_function,
            market_close_fn=my_market_close_function,
            nightly_replay_fn=my_replay_function,
        )
        scheduler.start()
    """

    def __init__(self):
        # apscheduler ka import yahan (class instantiate hote waqt, module
        # import ke waqt nahi) taaki agar library missing hai to error
        # sirf tab aaye jab scheduler actually use ho — isse upar wale
        # day-logic functions bina apscheduler ke bhi test ho sakte hain
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
        except ImportError:
            raise ImportError(
                "apscheduler install nahi hai. Chalao: pip install apscheduler "
                "(ya poora requirements.txt install karo)"
            )

        self.scheduler = BackgroundScheduler(timezone="Asia/Kolkata")

    def _guarded(self, fn, required_mode="TRADING"):
        """Wrapper — job ke andar day-mode check karta hai pehle."""
        def wrapped():
            current_mode = get_day_mode()
            if current_mode != required_mode:
                return  # aaj ye job chalne wala din nahi hai, silently skip
            fn()
        return wrapped

    def setup_jobs(
        self,
        pre_market_fn=None,
        market_open_fn=None,
        intraday_fn=None,
        market_close_fn=None,
        nightly_replay_fn=None,
        nse_square_off_fn=None,
        mcx_square_off_fn=None,
        delivery_snapshot_fn=None,
        mcx_market_open_fn=None,
        daily_cleanup_fn=None,
    ):
        pre_h, pre_m = map(int, AUTOMATION["PRE_MARKET_WAKE_TIME"].split(":"))
        open_h, open_m = map(int, AUTOMATION["MARKET_OPEN_TIME"].split(":"))
        close_h, close_m = map(int, AUTOMATION["MARKET_CLOSE_TIME"].split(":"))
        replay_h, replay_m = map(int, AUTOMATION["NIGHTLY_REPLAY_TIME"].split(":"))
        nse_h, nse_m = map(int, AUTOMATION.get("NSE_SQUARE_OFF_TIME", "15:15").split(":"))
        mcx_h, mcx_m = map(int, AUTOMATION.get("MCX_SQUARE_OFF_TIME", "23:15").split(":"))
        deliv_h, deliv_m = map(int, AUTOMATION.get("DELIVERY_SNAPSHOT_TIME", "15:00").split(":"))
        mcx_open_h, mcx_open_m = map(int, AUTOMATION.get("MCX_OPEN_TIME", "15:30").split(":"))

        if pre_market_fn:
            self.scheduler.add_job(
                self._guarded(pre_market_fn), "cron",
                hour=pre_h, minute=pre_m, id="pre_market_wakeup",
            )

        # Daily cleanup runs 5 minutes before pre-market wake.
        # Purges stale logs, refreshes Scrip Master, cleans temp files.
        if daily_cleanup_fn:
            cleanup_m = pre_m - 5 if pre_m >= 5 else 55
            cleanup_h = pre_h if pre_m >= 5 else pre_h - 1
            self.scheduler.add_job(
                self._guarded(daily_cleanup_fn), "cron",
                hour=cleanup_h, minute=cleanup_m, id="daily_cleanup",
            )

        if market_open_fn:
            self.scheduler.add_job(
                self._guarded(market_open_fn), "cron",
                hour=open_h, minute=open_m, id="market_open",
            )

        if intraday_fn:
            def intraday_guarded():
                try:
                    mode = get_day_mode()
                    mkt = is_market_hours()
                    opening = is_opening_range_period()
                    logger.info("intraday_guarded fired: mode=%s market=%s opening=%s",
                                mode, mkt, opening)
                    if mode == "TRADING" and mkt and not opening:
                        intraday_fn()
                except Exception as exc:
                    logger.error("intraday_guarded error: %s", exc)

            self.scheduler.add_job(
                intraday_guarded, "interval",
                minutes=1,  # HARDCODED 1-min — never rely on config (20-min bug)
                id="intraday_scan",
                max_instances=1,           # never overlap — if previous scan running, skip
                misfire_grace_time=10,    # tolerate 10s late fires
                coalesce=True,             # merge multiple missed fires into one
            )

        # Delivery snapshot at 3:00 PM — Tiger next-day direction decide
        # karke overnight delivery orders lagata hai
        if delivery_snapshot_fn:
            self.scheduler.add_job(
                self._guarded(delivery_snapshot_fn), "cron",
                hour=deliv_h, minute=deliv_m, id="delivery_snapshot",
            )

        # MCX-only mode starts at NSE square-off (15:15) — Tiger switches
        # from NSE+MCX simultaneous scan to MCX-only.
        # (MCX has been scanned since 09:00 alongside NSE; this handler
        #  ensures broker session + refreshes MCX data after NSE close.)
        if mcx_market_open_fn:
            self.scheduler.add_job(
                self._guarded(mcx_market_open_fn), "cron",
                hour=nse_h, minute=nse_m, id="mcx_market_open",
            )

        # NSE square-off at 15:15 (NSE scanning ends)
        if nse_square_off_fn:
            self.scheduler.add_job(
                self._guarded(nse_square_off_fn), "cron",
                hour=nse_h, minute=nse_m, id="nse_square_off",
            )

        # MCX square-off at 23:15 (15 min before MCX close 23:30)
        if mcx_square_off_fn:
            self.scheduler.add_job(
                self._guarded(mcx_square_off_fn), "cron",
                hour=mcx_h, minute=mcx_m, id="mcx_square_off",
            )

        if market_close_fn:
            self.scheduler.add_job(
                self._guarded(market_close_fn), "cron",
                hour=close_h, minute=close_m, id="market_close",
            )

        if nightly_replay_fn:
            def replay_guarded():
                if get_day_mode() == "TRADING":
                    nightly_replay_fn()

            self.scheduler.add_job(
                replay_guarded, "cron",
                hour=replay_h, minute=replay_m, id="nightly_replay",
            )

    def start(self):
        self.scheduler.start()

    def shutdown(self):
        self.scheduler.shutdown()


# ============================================================
# ENTRY POINT — python3 -m automation.scheduler
# Tiger V19 ko LIVE mode mein start karta hai (24x7 automation).
# Day-logic test ke liye: python3 -m automation.scheduler --test
# ============================================================
if __name__ == "__main__":
    if "--test" in sys.argv:
        from datetime import timedelta

        print("=== Day-Mode Logic Test (pure Python, apscheduler nahi chahiye) ===\n")

        monday = datetime(2025, 1, 6)  # ye ek Monday hai
        for i in range(7):
            test_date = monday + timedelta(days=i)
            day_name = WEEKDAY_MAP[test_date.weekday()]
            mode = get_day_mode(test_date)
            print(f"{day_name} ({test_date.date()}): {mode}")

        print("\n=== Market Hours Test ===")
        trading_day = datetime(2025, 1, 6, 10, 30)
        print(f"Monday 10:30 AM — is_market_hours: {is_market_hours(trading_day)}")

        before_open = datetime(2025, 1, 6, 8, 45)
        print(f"Monday 8:45 AM — is_market_hours: {is_market_hours(before_open)}")

        print("\n=== Opening Range Period Test ===")
        just_after_open = datetime(2025, 1, 6, 9, 20)
        print(f"9:20 AM (5 min after open) — is_opening_range: {is_opening_range_period(just_after_open)}")

        well_into_day = datetime(2025, 1, 6, 11, 0)
        print(f"11:00 AM — is_opening_range: {is_opening_range_period(well_into_day)}")

        print("\n✅ Day-logic test complete — koi crash nahi hua.")
    else:
        # LIVE MODE — Tiger 24x7 automation start karo
        from automation.tiger_live import TigerLiveRunner
        runner = TigerLiveRunner()
        runner.start()
  
