#!/usr/bin/env python3
"""Tiger Watchdog — monitors tiger_v19.log freshness and restarts tiger-brain if it stalls.

No trading logic touched. Only monitors + restarts.
- Every 5 min checks /home/ec2-user/tiger_v19.log last-modified time.
- During 09:15-23:30 IST, if last log line is older than 15 min → restart tiger-brain.
- If intraday_scan hasn't run in 25 min during market hours → restart.
- Alerts written to /tmp/watchdog.log.
"""
import os
import re
import subprocess
import time
from datetime import datetime, timedelta
from typing import Optional

LOG_PATH = "/home/ec2-user/tiger_v19.log"
WATCHDOG_LOG = "/tmp/watchdog.log"
MARKET_OPEN = (9, 15)
MARKET_CLOSE = (23, 30)
LOG_STALE_MIN = 15
SCAN_STALE_MIN = 25
CHECK_INTERVAL_SEC = 300  # 5 min

SCAN_RE = re.compile(r"INTRADAY SCAN")


def _now_ist() -> datetime:
    return datetime.now()


def _in_market_hours() -> bool:
    now = _now_ist()
    t = now.time()
    open_t = datetime.now().replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0).time()
    close_t = datetime.now().replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1], second=0, microsecond=0).time()
    return open_t <= t <= close_t


def _alert(msg: str) -> None:
    line = f"[{_now_ist().isoformat()}] {msg}"
    print(line, flush=True)
    try:
        with open(WATCHDOG_LOG, "a") as f:
            f.write(line + "\n")
    except Exception as exc:
        print(f"watchdog log write fail: {exc}", flush=True)


def _restart_tiger() -> None:
    _alert("[ALERT] Tiger so gaya — systemctl restart tiger-brain")
    try:
        subprocess.run(["sudo", "systemctl", "restart", "tiger-brain"], check=False, timeout=30)
    except Exception as exc:
        _alert(f"restart failed: {exc}")


def _log_mtime() -> Optional[float]:
    try:
        return os.path.getmtime(LOG_PATH)
    except OSError:
        return None


def _last_scan_ts() -> Optional[datetime]:
    try:
        with open(LOG_PATH, "r") as f:
            lines = f.readlines()[-500:]
    except OSError:
        return None
    for line in reversed(lines):
        if SCAN_RE.search(line):
            ts_str = line.split(" ")[0] if " " in line else line[:19]
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%H:%M:%S"):
                try:
                    dt = datetime.strptime(ts_str.strip(), fmt)
                    if fmt == "%H:%M:%S":
                        dt = _now_ist().replace(hour=dt.hour, minute=dt.minute, second=dt.second, microsecond=0)
                    return dt
                except ValueError:
                    continue
    return None


def check_once() -> None:
    if not _in_market_hours():
        return
    mtime = _log_mtime()
    now = _now_ist()
    if mtime is None:
        _alert("[ALERT] Tiger log nahi mila — restart")
        _restart_tiger()
        return
    log_age = now - datetime.fromtimestamp(mtime)
    if log_age > timedelta(minutes=LOG_STALE_MIN):
        _alert(f"[ALERT] Tiger so gaya — last log {log_age} purana hai")
        _restart_tiger()
        return
    last_scan = _last_scan_ts()
    if last_scan is not None:
        scan_age = now - last_scan
        if scan_age > timedelta(minutes=SCAN_STALE_MIN):
            _alert(f"[ALERT] intraday_scan {scan_age} se nahi chala — restart")
            _restart_tiger()


def main() -> None:
    _alert("🐅 Tiger Watchdog STARTED — 5 min interval")
    while True:
        try:
            check_once()
        except Exception as exc:
            _alert(f"watchdog check error: {exc}")
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
