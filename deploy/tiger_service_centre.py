"""
Tiger V19 — Service Centre
====================================================================
Ek hi command, Tiger ko full service. Jab bhi Tiger ko service ki
zarurat ho — diagnose → fix → deploy → verify → tests → report — sab
ek flow mein. Koi junk nahi, koi 10 scattered scripts nahi.

Usage (repo ROOT se):
    python3 -m deploy.tiger_service_centre <command>

Commands:
    status        — Tiger ka current health (service, balance, config, logs)
    diagnose      — Deep diagnosis (rejects, errors, MCX, session, data)
    deploy        — Pull latest main + restart + verify
    verify        — Config values + session logic + real balance
    logs          — Filtered log tail (NSE/MCX/orders/errors)
    tests         — pytest suite AWS pe chalao
    fix           — Auto-fix common issues (stale branch, dead service)
    full-service  — SAB KUCH: diagnose → fix → deploy → verify → tests → report

Config (env vars, sab optional — defaults work):
    TIGER_AWS_HOST    — AWS IP (default: 3.108.53.100)
    TIGER_AWS_USER    — SSH user (default: ec2-user)
    TIGER_SSH_KEY     — PEM key path (default: ~/.ssh/aws_tiger.pem)
    TIGER_REPO        — repo path on AWS (default: /home/ec2-user/tiger-brain-v6)
    GITHUB_TOKEN      — git pull ke liye (auto-injected)

⚠️ Ye script TIGER ko "service" deti hai — Tiger khud service nahi leta.
   TigerAWS pe chal raha hai; ye script usse remotely manage karta hai.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

try:
    import paramiko
except ImportError:
    print("❌ paramiko install karo: pip install paramiko")
    sys.exit(2)


# ============================================================
# CONFIG — env vars with sensible defaults (no hardcoding junk)
# ============================================================

HOST = os.environ.get("TIGER_AWS_HOST", "3.108.53.100")
USER = os.environ.get("TIGER_AWS_USER", "ec2-user")
REPO = os.environ.get("TIGER_REPO", "/home/ec2-user/tiger-brain-v6")
KEY_PATH = os.environ.get(
    "TIGER_SSH_KEY",
    os.path.expanduser("~/.ssh/aws_tiger.pem"),
)
LOG_FILE = "/home/ec2-user/tiger_v19.log"
SERVICE = "tiger-brain"
GITHUB_REPO = "Aslamrajput/tiger-brain-v6"

BANNER = "🐅 TIGER V19 — SERVICE CENTRE"
SEP = "=" * 66


# ============================================================
# CONNECTION LAYER
# ============================================================

def connect() -> paramiko.SSHClient:
    """AWS pe SSH connect karo. Key nahi mila to clear error."""
    if not os.path.exists(KEY_PATH):
        print(f"❌ SSH key nahi mili: {KEY_PATH}")
        print(f"   Set karo: export TIGER_SSH_KEY=/path/to/aws_tiger.pem")
        sys.exit(2)
    try:
        key = paramiko.RSAKey.from_private_key_file(KEY_PATH)
    except Exception as exc:
        print(f"❌ SSH key load fail ({KEY_PATH}): {exc}")
        sys.exit(2)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"🔌 Connecting {USER}@{HOST}...")
    client.connect(HOST, username=USER, pkey=key, timeout=25)
    print("✅ Connected\n")
    return client


def run(client: paramiko.SSHClient, cmd: str, timeout: int = 40,
        quiet: bool = False) -> tuple[str, str, int]:
    """Remote command chalao. Returns (stdout, stderr, exit_code)."""
    if not quiet:
        print(f"$ {cmd}")
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()
    code = stdout.channel.recv_exit_status()
    if not quiet:
        if out:
            print(out)
        if err:
            print(f"[stderr] {err}")
        print(f"[exit={code}]")
    return out, err, code


def section(title: str) -> None:
    print(f"\n{'─' * 66}")
    print(f"  {title}")
    print(f"{'─' * 66}")


# ============================================================
# COMMAND: status — Tiger ka current health
# ============================================================

def cmd_status(client: paramiko.SSHClient) -> dict:
    """Full health check. Returns dict of findings."""
    print(SEP)
    print(f"  {BANNER} — STATUS")
    print(SEP)
    findings = {}

    # 1. Service status
    section("1. SERVICE")
    out, _, _ = run(client, f"sudo systemctl is-active {SERVICE}", quiet=True)
    findings["service"] = out
    status_icon = "✅" if out == "active" else "❌"
    print(f"   {status_icon} Service: {out}")

    out, _, _ = run(client,
        f"systemctl show {SERVICE} --property=ActiveEnterTimestamp --value",
        quiet=True)
    findings["started_at"] = out
    print(f"   🕐 Started: {out}")

    out, _, _ = run(client,
        f"systemctl show {SERVICE} --property=NRestarts --value", quiet=True)
    findings["restarts"] = out
    print(f"   🔄 Restarts: {out}")

    # 2. Git state
    section("2. DEPLOYED CODE")
    out, _, _ = run(client, f"cd {REPO} && git log --oneline -1", quiet=True)
    findings["commit"] = out
    print(f"   📝 Commit: {out}")

    out, _, _ = run(client, f"cd {REPO} && git branch --show-current",
                    quiet=True)
    findings["branch"] = out
    print(f"   🌿 Branch: {out}")

    out, _, _ = run(client, f"cd {REPO} && git status -s | head -5",
                    quiet=True)
    findings["dirty"] = bool(out)
    if out:
        print(f"   ⚠️ Uncommitted changes:\n   {out}")
    else:
        print(f"   ✅ Clean working tree")

    # 3. Process
    section("3. PROCESS")
    out, _, _ = run(client,
        f"ps aux | grep -E 'run_tiger|tiger_live|scheduler' | grep -v grep",
        quiet=True)
    findings["process_running"] = bool(out)
    if out:
        print(f"   ✅ Tiger process running")
    else:
        print(f"   ❌ No Tiger process found")

    # 4. Balance (real Angel One)
    section("4. ANGEL ONE BALANCE")
    out, _, _ = run(client, f"cd {REPO} && python3 -c \""
        "from broker.angel_connect import AngelBroker; "
        "b=AngelBroker(); b.ensure_logged_in(); "
        "print('BALANCE:', b.get_balance())\" 2>/dev/null", quiet=True, timeout=30)
    bal = "?"
    for line in out.split("\n"):
        if "BALANCE:" in line:
            bal = line.split("BALANCE:")[1].strip()
    findings["balance"] = bal
    try:
        bal_float = float(bal)
        icon = "✅" if bal_float > 0 else "❌"
        print(f"   {icon} Balance: ₹{bal_float:,.2f}")
    except (ValueError, TypeError):
        print(f"   ❓ Balance: {bal}")

    # 5. Session state (current time)
    section("5. CURRENT SESSION")
    out, _, _ = run(client, f"cd {REPO} && python3 -c \""
        "from datetime import datetime; "
        "from automation.scheduler import is_market_hours, is_mcx_hours; "
        "now=datetime.now(); "
        "print('TIME:', now.strftime('%H:%M:%S')); "
        "print('NSE_OPEN:', is_market_hours(now)); "
        "print('MCX_OPEN:', is_mcx_hours(now))\"", quiet=True)
    findings["session"] = out
    for line in out.split("\n"):
        if line.strip():
            print(f"   {line}")

    # 6. Order activity
    section("6. ORDER ACTIVITY")
    out, _, _ = run(client,
        f"grep -c 'REAL ORDER' {LOG_FILE} 2>/dev/null || echo 0", quiet=True)
    findings["orders_placed"] = out
    print(f"   🔥 Orders placed: {out}")

    out, _, _ = run(client,
        f"grep -c 'ORDER REJECTED' {LOG_FILE} 2>/dev/null || echo 0",
        quiet=True)
    findings["orders_rejected"] = out
    print(f"   ❌ Orders rejected: {out}")

    out, _, _ = run(client,
        f"grep -c 'SKIP' {LOG_FILE} 2>/dev/null || echo 0", quiet=True)
    findings["orders_skipped"] = out
    print(f"   ⏭️  Orders skipped: {out}")

    # 7. Latest log
    section("7. LATEST LOG (last 25 lines)")
    out, _, _ = run(client, f"tail -25 {LOG_FILE}", quiet=True)
    print(out if out else "(log empty)")

    print(f"\n{SEP}")
    return findings


# ============================================================
# COMMAND: diagnose — Deep diagnosis
# ============================================================

def cmd_diagnose(client: paramiko.SSHClient) -> dict:
    """Deep diagnosis — rejects, errors, MCX, data, session."""
    print(SEP)
    print(f"  {BANNER} — DIAGNOSE")
    print(SEP)
    issues = []

    # 1. Errors in log
    section("1. ERRORS (last 24h)")
    out, _, _ = run(client,
        f"grep -iE 'error|exception|traceback|fail|crash' {LOG_FILE} | "
        f"tail -30", quiet=True)
    if out:
        print(out)
        issues.append("Errors found in log")
    else:
        print("   ✅ No errors in log")

    # 2. Rejected orders
    section("2. ORDER REJECTS")
    out, _, _ = run(client,
        f"grep -B2 -A5 'ORDER REJECTED' {LOG_FILE} | tail -40", quiet=True)
    if out:
        print(out)
        issues.append("Order rejects found")
    else:
        print("   ✅ No order rejects")

    # 3. Balance fetch failures
    section("3. BALANCE FETCH FAILURES")
    out, _, _ = run(client,
        f"grep -i 'balance.*fail\\|balance.*0\\|balance fetch' {LOG_FILE} | "
        f"tail -15", quiet=True)
    if out:
        print(out)
        issues.append("Balance fetch issues")
    else:
        print("   ✅ No balance fetch issues")

    # 4. MCX activity
    section("4. MCX ACTIVITY")
    out, _, _ = run(client,
        f"grep -iE 'MCX|CRUDE|GOLD|SILVER|NATURAL|commodity' {LOG_FILE} | "
        f"tail -30", quiet=True)
    if out:
        print(out)
    else:
        print("   ⚠️ No MCX activity in log")
        issues.append("No MCX activity")

    # 5. Session logic check
    section("5. SESSION LOGIC CHECK")
    out, _, _ = run(client, f"cd {REPO} && python3 -c \""
        "from backtest.tiger_session_brain import get_current_session, SESSION_SCHEDULE; "
        "from datetime import datetime; "
        "import pandas as pd; "
        "for h, m, seg in [(10,0,'nse'),(10,0,'mcx'),(14,35,'nse'),(18,0,'mcx'),(21,0,'mcx')]; "
        "  t = pd.Timestamp(f'2026-09-08 {h:02d}:{m:02d}:00', tz='Asia/Kolkata'); "
        "  s = get_current_session(t, seg); "
        "  print(f'{h:02d}:{m:02d} {seg}: {s.name if s else \"None\"}')\"", quiet=True)
    print(out)

    # 6. Config verification
    section("6. CONFIG VERIFICATION")
    out, _, _ = run(client, f"cd {REPO} && python3 -c \""
        "from config.thresholds import AUTOMATION; "
        "print('NSE:', AUTOMATION['MARKET_OPEN_TIME'], '-', AUTOMATION['MARKET_CLOSE_TIME']); "
        "print('MCX:', AUTOMATION['MCX_OPEN_TIME'], '-', AUTOMATION['MCX_CLOSE_TIME']); "
        "print('CUTOFF:', AUTOMATION['INTRADAY_ENTRY_CUTOFF_TIME']); "
        "print('DELIVERY:', AUTOMATION['DELIVERY_SNAPSHOT_TIME'])\"", quiet=True)
    print(out)

    # 7. Disk space
    section("7. DISK SPACE")
    out, _, _ = run(client, "df -h / | tail -1", quiet=True)
    print(f"   {out}")

    # 8. Memory
    section("8. MEMORY")
    out, _, _ = run(client, "free -h | grep Mem", quiet=True)
    print(f"   {out}")

    print(f"\n{SEP}")
    if issues:
        print(f"\n⚠️ ISSUES FOUND ({len(issues)}):")
        for i, issue in enumerate(issues, 1):
            print(f"   {i}. {issue}")
    else:
        print(f"\n✅ NO ISSUES — Tiger healthy")
    print(SEP)
    return {"issues": issues}


# ============================================================
# COMMAND: deploy — Pull latest main + restart + verify
# ============================================================

def cmd_deploy(client: paramiko.SSHClient) -> bool:
    """Pull latest main + restart service + verify."""
    print(SEP)
    print(f"  {BANNER} — DEPLOY")
    print(SEP)

    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        run(client,
            f"cd {REPO} && git remote set-url origin "
            f"https://{token}@github.com/{GITHUB_REPO}.git")

    # 1. Current state
    section("1. BEFORE DEPLOY")
    run(client, f"cd {REPO} && git branch --show-current && git log --oneline -1")

    # 2. Switch to main + pull
    section("2. PULL LATEST MAIN")
    run(client, f"cd {REPO} && git fetch origin", timeout=30)
    run(client, f"cd {REPO} && git checkout main", timeout=20)
    run(client, f"cd {REPO} && git pull origin main", timeout=30)

    # 3. Restart service
    section("3. RESTART SERVICE")
    run(client, f"sudo systemctl restart {SERVICE}", timeout=15)
    time.sleep(3)
    out, _, _ = run(client, f"sudo systemctl is-active {SERVICE}", quiet=True)
    if out == "active":
        print(f"   ✅ Service active")
    else:
        print(f"   ❌ Service NOT active: {out}")
        return False

    # 4. Verify
    section("4. POST-DEPLOY VERIFY")
    run(client, f"cd {REPO} && git log --oneline -3")
    run(client, f"sleep 2 && tail -15 {LOG_FILE}")

    print(f"\n{SEP}")
    print(f"✅ DEPLOY COMPLETE")
    print(SEP)
    return True


# ============================================================
# COMMAND: verify — Config + session + balance
# ============================================================

def cmd_verify(client: paramiko.SSHClient) -> bool:
    """Verify config values, session logic, real balance."""
    print(SEP)
    print(f"  {BANNER} — VERIFY")
    print(SEP)
    ok = True

    # 1. Config values
    section("1. CONFIG VALUES")
    out, _, _ = run(client, f"cd {REPO} && python3 -c \""
        "from config.thresholds import AUTOMATION, BRAIN4; "
        "print('NSE_HOURS:', AUTOMATION['MARKET_OPEN_TIME'], '-', AUTOMATION['MARKET_CLOSE_TIME']); "
        "print('MCX_HOURS:', AUTOMATION['MCX_OPEN_TIME'], '-', AUTOMATION['MCX_CLOSE_TIME']); "
        "print('CUTOFF:', AUTOMATION['INTRADAY_ENTRY_CUTOFF_TIME']); "
        "print('DELIVERY:', AUTOMATION['DELIVERY_SNAPSHOT_TIME']); "
        "print('NSE_SQUAREOFF:', AUTOMATION['NSE_SQUARE_OFF_TIME']); "
        "print('MCX_SQUAREOFF:', AUTOMATION['MCX_SQUARE_OFF_TIME']); "
        "print('MAX_CAPITAL_PCT:', BRAIN4['MAX_CAPITAL_PER_TRADE_PCT'])\"", quiet=True)
    print(out)

    # 2. Session logic (MCX all day fix)
    section("2. SESSION LOGIC (MCX morning fix)")
    out, _, _ = run(client, f"cd {REPO} && python3 -c \""
        "from backtest.tiger_session_brain import get_current_session; "
        "import pandas as pd; "
        "tests = [(10,0,'nse','MORNING_BURST'),(10,0,'mcx','COMMODITY_DAY'),"
        "(14,35,'nse','POWER_HOUR'),(18,0,'mcx','COMMODITY_OPEN'),"
        "(21,0,'mcx','NIGHT_RUSH')]; "
        "for h,m,seg,exp in tests: "
        "  t = pd.Timestamp(f'2026-09-08 {h:02d}:{m:02d}:00', tz='Asia/Kolkata'); "
        "  s = get_current_session(t, seg); "
        "  got = s.name if s else 'None'; "
        "  icon = '✅' if got == exp else '❌'; "
        "  print(f'{icon} {h:02d}:{m:02d} {seg}: got={got} expected={exp}')\"", quiet=True)
    print(out)
    if "❌" in out:
        ok = False

    # 3. Force hunt (dead code fix)
    section("3. FORCE HUNT (dead code fix)")
    out, _, _ = run(client, f"cd {REPO} && python3 -c \""
        "import inspect; from backtest.run_tiger_brain_backtest import find_tiger_brain_entry; "
        "sig = inspect.signature(find_tiger_brain_entry); "
        "params = list(sig.parameters.keys()); "
        "has_min_score = 'min_score' in params; "
        "has_force_hunt = 'force_hunt' in params; "
        "print('min_score param:', '✅' if has_min_score else '❌', has_min_score); "
        "print('force_hunt param:', '✅' if has_force_hunt else '❌', has_force_hunt)\"", quiet=True)
    print(out)
    if "❌" in out:
        ok = False

    # 4. Real balance
    section("4. REAL BALANCE")
    out, _, _ = run(client, f"cd {REPO} && python3 -c \""
        "from broker.angel_connect import AngelBroker; "
        "b=AngelBroker(); b.ensure_logged_in(); "
        "bal=b.get_balance(); print(f'BALANCE: ₹{bal:,.2f}')\" 2>/dev/null",
        quiet=True, timeout=30)
    print(out if out else "   ❌ Balance fetch failed")
    if "❌" in out or not out:
        ok = False

    # 5. Test count
    section("5. TEST SUITE")
    out, _, _ = run(client,
        f"cd {REPO} && python3 -m pytest tests/ -q "
        f"--ignore=tests/test_angel_rate_limit.py 2>&1 | tail -3",
        quiet=True, timeout=120)
    print(out)
    if "failed" in out and "0 failed" not in out:
        ok = False

    print(f"\n{SEP}")
    print(f"{'✅ ALL CHECKS PASSED' if ok else '❌ SOME CHECKS FAILED'}")
    print(SEP)
    return ok


# ============================================================
# COMMAND: logs — Filtered log tail
# ============================================================

def cmd_logs(client: paramiko.SSHClient, args) -> None:
    """Filtered log tail. --filter for keyword, --lines for count."""
    print(SEP)
    print(f"  {BANNER} — LOGS")
    print(SEP)
    lines = args.lines or 50
    filt = args.filter or ""

    if filt:
        cmd = f"grep -iE '{filt}' {LOG_FILE} | tail -{lines}"
        print(f"\n🔍 Filter: '{filt}' (last {lines})\n")
    else:
        cmd = f"tail -{lines} {LOG_FILE}"
        print(f"\n📜 Last {lines} lines\n")

    run(client, cmd, quiet=True)


# ============================================================
# COMMAND: tests — Run pytest on AWS
# ============================================================

def cmd_tests(client: paramiko.SSHClient) -> bool:
    """Run pytest suite on AWS."""
    print(SEP)
    print(f"  {BANNER} — TESTS")
    print(SEP)
    out, _, code = run(client,
        f"cd {REPO} && python3 -m pytest tests/ -q "
        f"--ignore=tests/test_angel_rate_limit.py 2>&1 | tail -10",
        quiet=True, timeout=180)
    print(out)
    print(f"\n[exit={code}]")
    return code == 0


# ============================================================
# COMMAND: fix — Auto-fix common issues
# ============================================================

def cmd_fix(client: paramiko.SSHClient) -> dict:
    """Auto-fix common issues: stale branch, dead service, dirty tree."""
    print(SEP)
    print(f"  {BANNER} — AUTO-FIX")
    print(SEP)
    fixes = []

    # 1. Stale branch (not main)
    section("1. BRANCH CHECK")
    out, _, _ = run(client, f"cd {REPO} && git branch --show-current",
                    quiet=True)
    if out != "main":
        print(f"   ⚠️ On branch '{out}', switching to main...")
        run(client, f"cd {REPO} && git fetch origin", timeout=30)
        run(client, f"cd {REPO} && git checkout main", timeout=20)
        run(client, f"cd {REPO} && git pull origin main", timeout=30)
        fixes.append(f"Switched branch {out} → main")
    else:
        print(f"   ✅ Already on main")

    # 2. Service dead?
    section("2. SERVICE CHECK")
    out, _, _ = run(client, f"sudo systemctl is-active {SERVICE}", quiet=True)
    if out != "active":
        print(f"   ⚠️ Service '{out}', restarting...")
        run(client, f"sudo systemctl restart {SERVICE}", timeout=15)
        time.sleep(3)
        out2, _, _ = run(client, f"sudo systemctl is-active {SERVICE}",
                         quiet=True)
        if out2 == "active":
            print(f"   ✅ Service restarted — now active")
            fixes.append("Restarted dead service")
        else:
            print(f"   ❌ Service still not active: {out2}")
    else:
        print(f"   ✅ Service active")

    # 3. Dirty working tree
    section("3. WORKING TREE CHECK")
    out, _, _ = run(client, f"cd {REPO} && git status -s | head -10",
                    quiet=True)
    if out:
        print(f"   ⚠️ Uncommitted changes:\n   {out}")
        run(client, f"cd {REPO} && git stash")
        fixes.append("Stashed uncommitted changes")
    else:
        print(f"   ✅ Clean working tree")

    # 4. Behind main?
    section("4. UPSTREAM CHECK")
    run(client, f"cd {REPO} && git fetch origin", timeout=30)
    out, _, _ = run(client,
        f"cd {REPO} && git rev-list --count HEAD..origin/main", quiet=True)
    try:
        behind = int(out)
    except ValueError:
        behind = 0
    if behind > 0:
        print(f"   ⚠️ {behind} commits behind origin/main — pulling...")
        run(client, f"cd {REPO} && git pull origin main", timeout=30)
        run(client, f"sudo systemctl restart {SERVICE}", timeout=15)
        fixes.append(f"Pulled {behind} commits + restarted")
    else:
        print(f"   ✅ Up to date with origin/main")

    print(f"\n{SEP}")
    if fixes:
        print(f"🔧 FIXES APPLIED ({len(fixes)}):")
        for i, f in enumerate(fixes, 1):
            print(f"   {i}. {f}")
    else:
        print(f"✅ NOTHING TO FIX — Tiger already healthy")
    print(SEP)
    return {"fixes": fixes}


# ============================================================
# COMMAND: full-service — SAB KUCH
# ============================================================

# ============================================================
# COMMAND: mechanics — Raju + Suresh ka status (autonomous watchdog)
# ============================================================

def cmd_mechanics(client: paramiko.SSHClient) -> dict:
    """Mechanics daemon status — kya Raju + Suresh nazar rakh rahe hain?"""
    print(SEP)
    print(f"  {BANNER} — MECHANICS STATUS")
    print(SEP)

    # 1. Mechanics service running?
    section("1. MECHANICS DAEMON")
    out, _, _ = run(client, "sudo systemctl is-active tiger-mechanics",
                    quiet=True)
    icon = "✅" if out == "active" else "❌"
    print(f"   {icon} tiger-mechanics service: {out}")

    out, _, _ = run(client,
        "systemctl show tiger-mechanics --property=ActiveEnterTimestamp --value",
        quiet=True)
    print(f"   🕐 Running since: {out}")

    out, _, _ = run(client,
        "systemctl show tiger-mechanics --property=NRestarts --value",
        quiet=True)
    print(f"   🔄 Restarts: {out}")

    # 2. Mechanics recent activity (last 30 lines)
    section("2. MECHANICS ACTIVITY (last 30 lines)")
    out, _, _ = run(client,
        "tail -30 /home/ec2-user/tiger_mechanics.log 2>/dev/null",
        quiet=True)
    print(out if out else "(no mechanics log yet — service may not be installed)")

    # 3. Service report (JSON)
    section("3. LAST SERVICE REPORT")
    out, _, _ = run(client,
        "python3 -c \""
        "import json; "
        "d = json.load(open('/home/ec2-user/tiger_service_log.json')); "
        "r = d[-1] if d else {}; "
        "print('Timestamp:', r.get('timestamp','?')); "
        "print('Tiger healthy:', r.get('tiger_healthy','?')); "
        "print('Total issues:', r.get('total_issues','?')); "
        "print('Fixes applied:', len(r.get('fixes_applied',[]))); "
        "raju = r.get('raju',{}); "
        "print('Raju stats:', raju.get('stats',{})); "
        "suresh = r.get('suresh',{}); "
        "print('Suresh stats:', suresh.get('stats',{}))\" 2>/dev/null",
        quiet=True)
    print(out if out else "(no service report yet)")

    # 4. Alerts (if any)
    section("4. RECENT ALERTS (last 10)")
    out, _, _ = run(client,
        "tail -10 /home/ec2-user/tiger_alerts.log 2>/dev/null",
        quiet=True)
    print(out if out else "✅ No alerts — all quiet")

    # 5. Install mechanics (if not running)
    if out != "active":
        section("5. INSTALL MECHANICS")
        print("   Mechanics service not installed. Install with:")
        print(f"     sudo cp {REPO}/deploy/tiger-mechanics.service "
              "/etc/systemd/system/")
        print("     sudo systemctl daemon-reload")
        print("     sudo systemctl enable --now tiger-mechanics")

    print(f"\n{SEP}")
    return {}


def cmd_full_service(client: paramiko.SSHClient) -> dict:
    """Full service: diagnose → fix → deploy → verify → tests → report."""
    print(SEP)
    print(f"  {BANNER} — FULL SERVICE")
    print(f"  Diagnose → Fix → Deploy → Verify → Tests → Report")
    print(SEP)

    report = {
        "started_at": datetime.now().isoformat(),
        "steps": [],
    }

    # Step 1: Diagnose
    print("\n" + "🔵 " * 33)
    print("  STEP 1/5: DIAGNOSE")
    print("🔵 " * 33)
    diag = cmd_diagnose(client)
    report["steps"].append({"step": "diagnose", "issues": diag["issues"]})

    # Step 2: Fix
    print("\n" + "🔧 " * 33)
    print("  STEP 2/5: AUTO-FIX")
    print("🔧 " * 33)
    fix_result = cmd_fix(client)
    report["steps"].append({"step": "fix", "fixes": fix_result["fixes"]})

    # Step 3: Deploy
    print("\n" + "🚀 " * 33)
    print("  STEP 3/5: DEPLOY")
    print("🚀 " * 33)
    deployed = cmd_deploy(client)
    report["steps"].append({"step": "deploy", "success": deployed})

    # Step 4: Verify
    print("\n" + "✅ " * 33)
    print("  STEP 4/5: VERIFY")
    print("✅ " * 33)
    verified = cmd_verify(client)
    report["steps"].append({"step": "verify", "passed": verified})

    # Step 5: Tests
    print("\n" + "🧪 " * 33)
    print("  STEP 5/5: TESTS")
    print("🧪 " * 33)
    tests_ok = cmd_tests(client)
    report["steps"].append({"step": "tests", "passed": tests_ok})

    # Final report
    report["completed_at"] = datetime.now().isoformat()
    print("\n" + SEP)
    print(f"  {BANNER} — SERVICE REPORT")
    print(SEP)
    print(f"  Started:   {report['started_at'][:19]}")
    print(f"  Completed: {report['completed_at'][:19]}")
    print(f"  Duration:  ~{int((datetime.now() - datetime.fromisoformat(report['started_at'])).total_seconds())}s")
    print()
    for step in report["steps"]:
        name = step["step"].upper()
        if "issues" in step:
            n = len(step["issues"])
            icon = "⚠️" if n else "✅"
            print(f"  {icon} {name:10} — {n} issue(s)" if n
                  else f"  {icon} {name:10} — clean")
        elif "fixes" in step:
            n = len(step["fixes"])
            icon = "🔧" if n else "✅"
            print(f"  {icon} {name:10} — {n} fix(es)" if n
                  else f"  {icon} {name:10} — nothing to fix")
        elif "success" in step:
            icon = "✅" if step["success"] else "❌"
            print(f"  {icon} {name:10} — "
                  f"{'deployed' if step['success'] else 'FAILED'}")
        elif "passed" in step:
            icon = "✅" if step["passed"] else "❌"
            print(f"  {icon} {name:10} — "
                  f"{'passed' if step['passed'] else 'FAILED'}")
    print(SEP)

    all_ok = (not diag["issues"] or len(fix_result["fixes"]) > 0) \
        and deployed and verified and tests_ok
    if all_ok:
        print(f"\n🐅 TIGER FULLY SERVICED — READY TO HUNT 🔥")
    else:
        print(f"\n⚠️ SERVICE COMPLETE — some issues remain, review above")
    print(SEP)
    return report


# ============================================================
# CLI
# ============================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m deploy.tiger_service_centre",
        description="Tiger V19 Service Centre — full service in one command",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Tiger ka current health check")
    sub.add_parser("diagnose", help="Deep diagnosis (rejects, errors, MCX)")
    sub.add_parser("deploy", help="Pull latest main + restart + verify")
    sub.add_parser("verify", help="Config + session + balance verification")
    sub.add_parser("tests", help="Run pytest suite on AWS")
    sub.add_parser("mechanics", help="Raju + Suresh ka status (autonomous watchdog)")

    logs_p = sub.add_parser("logs", help="Filtered log tail")
    logs_p.add_argument("--filter", "-f", default="",
                        help="Keyword filter (e.g. MCX, REJECT, error)")
    logs_p.add_argument("--lines", "-n", type=int, default=50,
                        help="Number of lines (default: 50)")

    sub.add_parser("fix", help="Auto-fix common issues")
    sub.add_parser("full-service", help="SAB KUCH: diagnose→fix→deploy→verify→tests")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(f"\n{BANNER}")
    print(f"Host: {HOST} | Repo: {REPO} | Key: {KEY_PATH}")

    client = connect()
    try:
        if args.command == "status":
            cmd_status(client)
        elif args.command == "diagnose":
            cmd_diagnose(client)
        elif args.command == "deploy":
            cmd_deploy(client)
        elif args.command == "verify":
            cmd_verify(client)
        elif args.command == "logs":
            cmd_logs(client, args)
        elif args.command == "tests":
            cmd_tests(client)
        elif args.command == "mechanics":
            cmd_mechanics(client)
        elif args.command == "fix":
            cmd_fix(client)
        elif args.command == "full-service":
            cmd_full_service(client)
        return 0
    except Exception as exc:
        print(f"\n❌ Service Centre error: {exc}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        client.close()
        print(f"\n🔌 Connection closed")


if __name__ == "__main__":
    sys.exit(main())
