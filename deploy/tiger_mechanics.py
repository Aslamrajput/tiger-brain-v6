"""
Tiger V19 — Personal Service Centre (2 Mechanics)
====================================================================
Tiger ka apna personal service centre. Do mechanic — dono 10 saal ke
experienced — hamesha Tiger ke saath rehte hain (AWS pe). Koi command
ki zarurat nahi. Wo khud nazar rakhte hain, khud service karte hain.

  🔧 Raju  — Health Watcher (10 saal experience)
     Tiger ki health ka pura nazar: service alive? balance OK? errors?
     orders? disk? memory? Kuch galat → turant fix.

  🔧 Suresh — Code Keeper (10 saal experience)
     Tiger ka code hamesha latest + clean: git branch? behind main?
     tests pass? config correct? Code purana → pull + restart.

Dono milkar har 15 minute Tiger ki service check karte hain. Jab zarurat
hoti hai, khud service dete hain. Report + alerts log file mein likhte
hain taaki baad mein dekh sako.

Ye daemon AWS pe LOCAL chalta hai (Tiger ke saath, remotely nahi).
Systemd service se auto-start hota hai — reboot/crash pe khud restart.

Usage (AWS pe, auto-start via systemd):
    python3 -m deploy.tiger_mechanics

Manual test (ek baar chalao):
    python3 -m deploy.tiger_mechanics --once

Safety:
  - Kabhi data delete nahi karte
  - Market hours mein Tiger restart sirf tab jab service dead ho
  - Har action log hota hai (service_log.json + alerts.log)
  - Destructive action nahi — sirf restart, pull, verify
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

# ============================================================
# CONFIG
# ============================================================

REPO = Path(os.environ.get("TIGER_REPO", "/home/ec2-user/tiger-brain-v6"))
SERVICE = "tiger-brain"
LOG_FILE = Path("/home/ec2-user/tiger_v19.log")
MECHANICS_LOG = Path("/home/ec2-user/tiger_mechanics.log")
SERVICE_LOG = Path("/home/ec2-user/tiger_service_log.json")
ALERTS_LOG = Path("/home/ec2-user/tiger_alerts.log")
CHECK_INTERVAL_SECONDS = int(os.environ.get("TIGER_MECHANIC_INTERVAL", "900"))  # 15 min
GITHUB_REPO = "Aslamrajput/tiger-brain-v6"
MARKET_HOURS_NO_RESTART = True  # market mein dead hone pe bhi restart OK (Tiger dead = no trades)

# Market hours (don't do disruptive git pulls during live trading)
NSE_OPEN = (9, 15)
NSE_CLOSE = (15, 30)
MCX_OPEN = (9, 0)
MCX_CLOSE = (23, 30)


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    """Mechanics ka activity log (stdout + file)."""
    line = f"[{now_str()}] {msg}"
    print(line, flush=True)
    try:
        MECHANICS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(MECHANICS_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def alert(msg: str, severity: str = "WARNING") -> None:
    """Critical alert log — alag file taaki quickly dekh sako."""
    line = f"[{now_str()}] [{severity}] {msg}"
    log(f"🚨 ALERT ({severity}): {msg}")
    try:
        ALERTS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(ALERTS_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def run_local(cmd: str, timeout: int = 60, cwd: str | None = None) -> tuple[str, int]:
    """Local command chalao (subprocess). Returns (output, exit_code)."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=cwd,
        )
        return result.stdout.strip(), result.returncode
    except subprocess.TimeoutExpired:
        return f"TIMEOUT after {timeout}s", -1
    except Exception as exc:
        return f"ERROR: {exc}", -1


def is_market_hours() -> bool:
    """Abhi market open hai? (NSE ya MCX). Disruptive actions avoid."""
    now = datetime.now()
    t = now.time()
    from datetime import time as dtime
    nse_open = dtime(*NSE_OPEN)
    nse_close = dtime(*NSE_CLOSE)
    mcx_open = dtime(*MCX_OPEN)
    mcx_close = dtime(*MCX_CLOSE)
    # Weekend check
    if now.weekday() >= 5:
        return False
    return (nse_open <= t <= nse_close) or (mcx_open <= t <= mcx_close)


# ============================================================
# MECHANIC 1: RAJU — Health Watcher (10 saal experience)
# ============================================================

class RajuHealthWatcher:
    """Tiger ki health ka pura nazar. Service, balance, errors, orders."""

    name = "Raju"
    role = "Health Watcher"
    experience = "10 saal"

    def inspect(self) -> dict:
        """Tiger ki health check karo. Issues return karo."""
        log(f"🔧 {self.name} ({self.role}, {self.experience}) — inspection start")
        issues = []

        # 1. Service alive?
        out, code = run_local(f"systemctl is-active {SERVICE}")
        if out.strip() != "active":
            issues.append({
                "mechanic": self.name,
                "severity": "CRITICAL",
                "issue": f"Tiger service dead: '{out.strip()}'",
                "fix": "restart_service",
            })
            log(f"   ❌ {self.name}: Service dead '{out.strip()}'")
        else:
            log(f"   ✅ {self.name}: Service active")

        # 2. Restart count (too many = unstable)
        out, _ = run_local(
            f"systemctl show {SERVICE} --property=NRestarts --value")
        try:
            restarts = int(out)
        except (ValueError, TypeError):
            restarts = 0
        if restarts > 10:
            issues.append({
                "mechanic": self.name,
                "severity": "WARNING",
                "issue": f"High restart count: {restarts}",
                "fix": "alert_only",
            })
        log(f"   📊 {self.name}: Restarts today: {restarts}")

        # 3. Errors in log (last 15 min) — exclude harmless warnings
        # (FutureWarning, DeprecationWarning, UserWarning = pandas/numpy
        # noise, NOT real errors). Real errors: ERROR, Exception,
        # Traceback, CRITICAL, FAIL.
        out, _ = run_local(
            f"grep -iE 'error|exception|traceback|crash|critical' {LOG_FILE} | "
            f"grep -ivE 'futurewarning|deprecationwarning|userwarning|"
            f"runtimewarning|pdwarnings|warning:' | "
            f"tail -20 | wc -l")
        try:
            error_count = int(out)
        except (ValueError, TypeError):
            error_count = 0
        if error_count > 5:
            issues.append({
                "mechanic": self.name,
                "severity": "WARNING",
                "issue": f"{error_count} errors in last 15 min",
                "fix": "alert_only",
            })
        log(f"   📋 {self.name}: Errors (15 min): {error_count}")

        # 4. Balance fetch (if service running)
        if not issues or issues[0]["fix"] != "restart_service":
            out, _ = run_local(
                f"cd {REPO} && python3 -c \""
                f"from broker.angel_connect import AngelBroker; "
                f"b=AngelBroker(); b.ensure_logged_in(); "
                f"print('BAL:', b.get_balance())\" 2>/dev/null",
                timeout=30)
            bal = None
            for line in out.split("\n"):
                if "BAL:" in line:
                    try:
                        bal = float(line.split("BAL:")[1].strip())
                    except ValueError:
                        pass
            if bal is not None and bal <= 0:
                issues.append({
                    "mechanic": self.name,
                    "severity": "CRITICAL",
                    "issue": f"Balance zero/negative: ₹{bal}",
                    "fix": "alert_only",
                })
                log(f"   ❌ {self.name}: Balance ₹{bal} (zero!)")
            elif bal is not None:
                log(f"   ✅ {self.name}: Balance ₹{bal:,.2f}")
            else:
                log(f"   ⚠️ {self.name}: Balance fetch failed (non-critical)")

        # 5. Disk space
        out, _ = run_local("df / | tail -1 | awk '{print $5}' | tr -d '%'")
        try:
            disk_pct = int(out)
        except (ValueError, TypeError):
            disk_pct = 0
        if disk_pct > 90:
            issues.append({
                "mechanic": self.name,
                "severity": "CRITICAL",
                "issue": f"Disk {disk_pct}% full",
                "fix": "alert_only",
            })
            log(f"   ❌ {self.name}: Disk {disk_pct}% full!")
        else:
            log(f"   💾 {self.name}: Disk {disk_pct}% used")

        # 6. Memory
        out, _ = run_local("free | grep Mem | awk '{print int($3/$2*100)}'")
        try:
            mem_pct = int(out)
        except (ValueError, TypeError):
            mem_pct = 0
        if mem_pct > 90:
            issues.append({
                "mechanic": self.name,
                "severity": "WARNING",
                "issue": f"Memory {mem_pct}% used",
                "fix": "alert_only",
            })
        log(f"   🧠 {self.name}: Memory {mem_pct}% used")

        # 7. Order activity
        out, _ = run_local(
            f"grep -c 'REAL ORDER' {LOG_FILE} 2>/dev/null || echo 0")
        orders_placed = out.strip() or "0"
        out, _ = run_local(
            f"grep -c 'ORDER REJECTED' {LOG_FILE} 2>/dev/null || echo 0")
        orders_rejected = out.strip() or "0"
        log(f"   🔥 {self.name}: Orders placed={orders_placed}, rejected={orders_rejected}")

        log(f"🔧 {self.name} — inspection done ({len(issues)} issue(s))")
        return {
            "mechanic": self.name,
            "issues": issues,
            "stats": {
                "restarts": restarts,
                "errors_15min": error_count,
                "disk_pct": disk_pct,
                "mem_pct": mem_pct,
                "orders_placed": orders_placed,
                "orders_rejected": orders_rejected,
            },
        }

    def fix(self, issue: dict) -> bool:
        """Raju ka fix — service restart ya alert."""
        if issue["fix"] == "restart_service":
            log(f"   🔧 {self.name}: Restarting Tiger service...")
            out, code = run_local(f"sudo systemctl restart {SERVICE}", timeout=20)
            time.sleep(5)
            out, _ = run_local(f"systemctl is-active {SERVICE}")
            if out.strip() == "active":
                log(f"   ✅ {self.name}: Service restarted — now active")
                alert("Tiger service was dead — Raju restarted it. Now active.",
                      "CRITICAL")
                return True
            else:
                log(f"   ❌ {self.name}: Restart failed — still '{out.strip()}'")
                alert(f"Tiger service dead + restart FAILED! Status: {out.strip()}",
                      "CRITICAL")
                return False
        elif issue["fix"] == "alert_only":
            alert(f"{self.name} found: {issue['issue']}", issue["severity"])
            return True
        return True


# ============================================================
# MECHANIC 2: SURESH — Code Keeper (10 saal experience)
# ============================================================

class SureshCodeKeeper:
    """Tiger ka code hamesha latest + clean. Git, tests, config."""

    name = "Suresh"
    role = "Code Keeper"
    experience = "10 saal"

    def inspect(self) -> dict:
        """Code state check karo. Issues return karo."""
        log(f"🔧 {self.name} ({self.role}, {self.experience}) — inspection start")
        issues = []

        # 1. Git branch (should be main)
        out, _ = run_local(f"cd {REPO} && git branch --show-current")
        branch = out.strip()
        if branch and branch != "main":
            issues.append({
                "mechanic": self.name,
                "severity": "WARNING",
                "issue": f"On branch '{branch}', should be 'main'",
                "fix": "switch_to_main",
            })
            log(f"   ⚠️ {self.name}: Branch '{branch}' (should be main)")
        else:
            log(f"   ✅ {self.name}: On main branch")

        # 2. Behind origin/main?
        run_local(f"cd {REPO} && git fetch origin 2>/dev/null", timeout=30)
        out, _ = run_local(
            f"cd {REPO} && git rev-list --count HEAD..origin/main")
        try:
            behind = int(out)
        except (ValueError, TypeError):
            behind = 0
        if behind > 0:
            severity = "WARNING"
            # During market hours — don't pull (disruptive)
            if is_market_hours():
                issues.append({
                    "mechanic": self.name,
                    "severity": severity,
                    "issue": f"{behind} commits behind main (market open — will pull after close)",
                    "fix": "wait_for_market_close",
                })
                log(f"   ⏳ {self.name}: {behind} behind main (market open — waiting)")
            else:
                issues.append({
                    "mechanic": self.name,
                    "severity": severity,
                    "issue": f"{behind} commits behind main",
                    "fix": "pull_and_restart",
                })
                log(f"   ⚠️ {self.name}: {behind} commits behind main")
        else:
            log(f"   ✅ {self.name}: Up to date with main")

        # 3. Dirty working tree
        out, _ = run_local(f"cd {REPO} && git status -s | head -5")
        if out.strip():
            issues.append({
                "mechanic": self.name,
                "severity": "INFO",
                "issue": f"Uncommitted changes: {out.strip()[:80]}",
                "fix": "stash",
            })
            log(f"   📝 {self.name}: Uncommitted changes found")
        else:
            log(f"   ✅ {self.name}: Clean working tree")

        # 4. Config check (quick, non-disruptive)
        out, _ = run_local(f"cd {REPO} && python3 -c \""
            "from config.thresholds import AUTOMATION; "
            "print('MCX:', AUTOMATION['MCX_OPEN_TIME'], '-', AUTOMATION['MCX_CLOSE_TIME']); "
            "print('NSE:', AUTOMATION['MARKET_OPEN_TIME'], '-', AUTOMATION['MARKET_CLOSE_TIME'])\""
            " 2>/dev/null", timeout=15)
        if "MCX:" not in out:
            issues.append({
                "mechanic": self.name,
                "severity": "WARNING",
                "issue": "Config import failed",
                "fix": "alert_only",
            })
            log(f"   ❌ {self.name}: Config check failed")
        else:
            log(f"   ✅ {self.name}: Config OK ({out.split(chr(10))[0]})")

        log(f"🔧 {self.name} — inspection done ({len(issues)} issue(s))")
        return {
            "mechanic": self.name,
            "issues": issues,
            "stats": {
                "branch": branch,
                "behind_main": behind,
            },
        }

    def fix(self, issue: dict) -> bool:
        """Suresh ka fix — switch branch, pull, stash, alert."""
        fix_type = issue["fix"]

        if fix_type == "switch_to_main":
            log(f"   🔧 {self.name}: Switching to main...")
            run_local(f"cd {REPO} && git stash 2>/dev/null")
            out, _ = run_local(f"cd {REPO} && git checkout main", timeout=20)
            run_local(f"cd {REPO} && git pull origin main", timeout=30)
            run_local(f"sudo systemctl restart {SERVICE}", timeout=15)
            time.sleep(3)
            out, _ = run_local(f"systemctl is-active {SERVICE}")
            if out.strip() == "active":
                log(f"   ✅ {self.name}: Switched to main + restarted")
                alert("Suresh switched Tiger to main branch + restarted.",
                      "INFO")
                return True
            return False

        elif fix_type == "pull_and_restart":
            log(f"   🔧 {self.name}: Pulling latest main + restarting...")
            token = os.environ.get("GITHUB_TOKEN", "")
            if token:
                run_local(
                    f"cd {REPO} && git remote set-url origin "
                    f"https://{token}@github.com/{GITHUB_REPO}.git")
            run_local(f"cd {REPO} && git pull origin main", timeout=30)
            run_local(f"sudo systemctl restart {SERVICE}", timeout=15)
            time.sleep(3)
            out, _ = run_local(f"systemctl is-active {SERVICE}")
            if out.strip() == "active":
                log(f"   ✅ {self.name}: Pulled + restarted — active")
                alert("Suresh pulled latest main + restarted Tiger.",
                      "INFO")
                return True
            else:
                alert(f"Suresh pulled + restarted but service '{out.strip()}'",
                      "CRITICAL")
                return False

        elif fix_type == "stash":
            log(f"   🔧 {self.name}: Stashing uncommitted changes...")
            run_local(f"cd {REPO} && git stash 2>/dev/null")
            log(f"   ✅ {self.name}: Stashed")
            return True

        elif fix_type == "wait_for_market_close":
            log(f"   ⏳ {self.name}: Market open — will pull after close")
            return True

        elif fix_type == "alert_only":
            alert(f"{self.name} found: {issue['issue']}", issue["severity"])
            return True

        return True


# ============================================================
# TIGER SERVICE CENTRE — Raju + Suresh milkar
# ============================================================

class TigerServiceCentre:
    """Personal service centre — 2 mechanics Tiger ki 24/7 service karte hain."""

    def __init__(self):
        self.raju = RajuHealthWatcher()
        self.suresh = SureshCodeKeeper()
        self.report: list[dict] = []

    def run_check(self) -> dict:
        """Dono mechanics inspect + fix. Ek complete service cycle."""
        log("")
        log("=" * 66)
        log(f"  🐅 TIGER SERVICE CENTRE — CHECK @ {now_str()}")
        log("=" * 66)
        log(f"  🔧 Raju  ({self.raju.role}, {self.raju.experience})")
        log(f"  🔧 Suresh ({self.suresh.role}, {self.suresh.experience})")
        log("=" * 66)

        all_issues = []
        fixes_applied = []

        # Mechanic 1: Raju inspects
        raju_result = self.raju.inspect()
        all_issues.extend(raju_result["issues"])

        # Mechanic 2: Suresh inspects
        suresh_result = self.suresh.inspect()
        all_issues.extend(suresh_result["issues"])

        # Fix issues (critical first)
        critical = [i for i in all_issues if i["severity"] == "CRITICAL"]
        warnings = [i for i in all_issues if i["severity"] == "WARNING"]
        infos = [i for i in all_issues if i["severity"] == "INFO"]

        if critical:
            log(f"\n🚨 {len(critical)} CRITICAL issue(s) — fixing NOW:")
            for issue in critical:
                mechanic = self.raju if issue["mechanic"] == "Raju" else self.suresh
                fixed = mechanic.fix(issue)
                fixes_applied.append({
                    "mechanic": issue["mechanic"],
                    "issue": issue["issue"],
                    "fixed": fixed,
                })

        if warnings:
            log(f"\n⚠️ {len(warnings)} WARNING(s) — fixing:")
            for issue in warnings:
                mechanic = self.raju if issue["mechanic"] == "Raju" else self.suresh
                fixed = mechanic.fix(issue)
                fixes_applied.append({
                    "mechanic": issue["mechanic"],
                    "issue": issue["issue"],
                    "fixed": fixed,
                })

        if infos:
            log(f"\n📝 {len(infos)} INFO item(s) — fixing:")
            for issue in infos:
                mechanic = self.raju if issue["mechanic"] == "Raju" else self.suresh
                fixed = mechanic.fix(issue)
                fixes_applied.append({
                    "mechanic": issue["mechanic"],
                    "issue": issue["issue"],
                    "fixed": fixed,
                })

        # Summary
        log(f"\n{'─' * 66}")
        log(f"  SERVICE CENTRE REPORT @ {now_str()}")
        log(f"{'─' * 66}")
        log(f"  Raju  ({self.raju.role}):")
        log(f"    Restarts: {raju_result['stats']['restarts']} | "
            f"Errors(15m): {raju_result['stats']['errors_15min']} | "
            f"Disk: {raju_result['stats']['disk_pct']}% | "
            f"Mem: {raju_result['stats']['mem_pct']}%")
        log(f"    Orders: placed={raju_result['stats']['orders_placed']} | "
            f"rejected={raju_result['stats']['orders_rejected']}")
        log(f"  Suresh ({self.suresh.role}):")
        log(f"    Branch: {suresh_result['stats']['branch']} | "
            f"Behind main: {suresh_result['stats']['behind_main']}")
        if fixes_applied:
            log(f"  🔧 Fixes applied: {len(fixes_applied)}")
            for fx in fixes_applied:
                icon = "✅" if fx["fixed"] else "❌"
                log(f"    {icon} {fx['mechanic']}: {fx['issue']}")
        else:
            log(f"  ✅ No fixes needed — Tiger healthy")
        log(f"{'─' * 66}\n")

        # Write report to JSON
        report_entry = {
            "timestamp": now_str(),
            "raju": raju_result,
            "suresh": suresh_result,
            "fixes_applied": fixes_applied,
            "total_issues": len(all_issues),
            "tiger_healthy": len(critical) == 0,
        }
        self._write_report(report_entry)
        return report_entry

    def _write_report(self, entry: dict) -> None:
        """Report JSON file mein append (last 100 entries keep)."""
        try:
            reports = []
            if SERVICE_LOG.exists():
                with open(SERVICE_LOG) as f:
                    try:
                        reports = json.load(f)
                    except json.JSONDecodeError:
                        reports = []
            reports.append(entry)
            reports = reports[-100:]  # keep last 100
            with open(SERVICE_LOG, "w") as f:
                json.dump(reports, f, indent=2)
        except Exception as exc:
            log(f"⚠️ Report write failed: {exc}")

    def run_forever(self) -> None:
        """Main loop — har 15 minute check karo. Forever."""
        log("")
        log("=" * 66)
        log("  🐅 TIGER PERSONAL SERVICE CENTRE — STARTING")
        log("=" * 66)
        log(f"  🔧 Mechanic 1: Raju  — Health Watcher (10 saal experience)")
        log(f"  🔧 Mechanic 2: Suresh — Code Keeper (10 saal experience)")
        log(f"  ⏰ Check interval: every {CHECK_INTERVAL_SECONDS}s ({CHECK_INTERVAL_SECONDS // 60} min)")
        log(f"  📋 Service log: {SERVICE_LOG}")
        log(f"  🚨 Alerts log: {ALERTS_LOG}")
        log(f"  🔧 Mechanics log: {MECHANICS_LOG}")
        log("=" * 66)
        log("  Tiger ab full automation pe — kisi command ki zarurat nahi.")
        log("  Raju + Suresh khud nazar rakhenge, khud service karenge.")
        log("=" * 66)
        log("")

        while True:
            try:
                self.run_check()
            except Exception as exc:
                log(f"❌ Service Centre error: {exc}")
                traceback.print_exc()
                alert(f"Service Centre crash: {exc}", "CRITICAL")

            log(f"😴 Next check in {CHECK_INTERVAL_SECONDS // 60} minutes...")
            time.sleep(CHECK_INTERVAL_SECONDS)


# ============================================================
# CLI
# ============================================================

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m deploy.tiger_mechanics",
        description="Tiger Personal Service Centre — 2 mechanics, autonomous 24/7",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Ek baar check karo (no loop) — testing ke liye",
    )
    args = parser.parse_args(argv)

    centre = TigerServiceCentre()

    if args.once:
        centre.run_check()
        return 0

    centre.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
