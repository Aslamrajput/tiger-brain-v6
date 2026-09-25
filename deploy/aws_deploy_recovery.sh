#!/bin/bash
# ============================================================
# Tiger V19 — CLEAN DEPLOY of recover-003a9ff-life to AWS EC2
# ============================================================
# Deploys branch: recover-003a9ff-life (commit 57b52e4, child of 003a9ff)
#   - MCX options ON (spread gate 1.5%)
#   - Watchdog service installed
#   - Old processes/logs cleaned
#   - main branch NEVER touched
#
# Usage (on EC2 as ec2-user):
#   cd /home/ec2-user/tiger-brain-v6
#   bash deploy/aws_deploy_recovery.sh
# ============================================================
set -euo pipefail

REPO_DIR="/home/ec2-user/tiger-brain-v6"
BRANCH="recover-003a9ff-life"
LOG="/home/ec2-user/tiger_v19.log"
WDOG_LOG="/home/ec2-user/tiger_watchdog.log"
SERVICE_SRC="$REPO_DIR/config/tiger_brain.service"
SERVICE_DST="/etc/systemd/system/tiger-brain.service"
WDOG_DST="/etc/systemd/system/tiger-watchdog.service"
BACKUP_DIR="/home/ec2-user/tiger_backup_$(date +%Y%m%d_%H%M%S)"

echo "🐅 TIGER V19 — CLEAN RECOVERY DEPLOY"
echo "   Branch: $BRANCH"
echo "   Target: $(date)"
echo "========================================"
echo ""

# 1. Timezone IST
echo "1. Timezone IST..."
sudo timedatectl set-timezone Asia/Kolkata
echo "   ✅ Asia/Kolkata ($(date))"
echo ""

# 2. Stop old services cleanly
echo "2. Stop old services..."
for svc in tiger-watchdog tiger-brain; do
    if sudo systemctl is-active --quiet "$svc" 2>/dev/null; then
        sudo systemctl stop "$svc"
        echo "   ✅ Stopped $svc"
    else
        echo "   • $svc not running"
    fi
done
echo ""

# 3. Kill any stray Tiger processes
echo "3. Kill stray Tiger processes..."
for pat in "automation.scheduler" "automation.watchdog" "automation.tiger_live"; do
    PID=$(pgrep -f "$pat" 2>/dev/null || true)
    if [ -n "$PID" ]; then
        kill -9 $PID 2>/dev/null || true
        echo "   ✅ Killed $pat (PID $PID)"
    else
        echo "   • No $pat process"
    fi
done
echo ""

# 4. Backup old logs (don't destroy — keep history)
echo "4. Backup old logs..."
mkdir -p "$BACKUP_DIR"
[ -f "$LOG" ] && cp "$LOG" "$BACKUP_DIR/tiger_v19.log.old" 2>/dev/null || true
[ -f "$WDOG_LOG" ] && cp "$WDOG_LOG" "$BACKUP_DIR/tiger_watchdog.log.old" 2>/dev/null || true
echo "   ✅ Backup: $BACKUP_DIR"
echo ""

# 5. Pull recovery branch (NOT main)
echo "5. Fetch + checkout $BRANCH..."
cd "$REPO_DIR"
git fetch origin
git checkout "$BRANCH"
git reset --hard "origin/$BRANCH"
echo "   ✅ HEAD: $(git log --oneline -1)"
echo "   ✅ main untouched: $(git rev-parse --short origin/main)"
echo ""

# 6. Install dependencies
echo "6. Install Python deps..."
pip install -q -r requirements.txt 2>&1 | tail -3 || true
echo "   ✅ Dependencies installed"
echo ""

# 7. Verify .env exists (DO NOT overwrite — secrets)
echo "7. Verify .env..."
if [ ! -f "$REPO_DIR/.env" ]; then
    echo "   ❌ .env MISSING — copy .env.example and fill credentials BEFORE start!"
    echo "      cp .env.example .env && nano .env"
    exit 1
fi
echo "   ✅ .env present (secrets intact)"
echo ""

# 8. Truncate live logs (fresh start, backup already taken)
echo "8. Truncate live logs..."
: > "$LOG" 2>/dev/null || sudo tee "$LOG" </dev/null >/dev/null || true
: > "$WDOG_LOG" 2>/dev/null || true
echo "   ✅ Fresh logs"
echo ""

# 9. Install service files (split the combined config/tiger_brain.service)
echo "9. Install systemd units..."
# The config/tiger_brain.service has TWO [Unit] blocks (tiger-brain + tiger-watchdog).
# Split them into separate unit files.
awk '/^# --- split: tiger-brain ---/{f="'"$SERVICE_DST"'"} /^# --- split: tiger-watchdog ---/{f="'"$WDOG_DST"'"} f{print>f}' "$SERVICE_SRC" 2>/dev/null || true
# If split markers absent, fallback: copy whole file to tiger-brain, and watchdog block separately
if [ ! -s "$SERVICE_DST" ] || [ ! -s "$WDOG_DST" ]; then
    echo "   ⚠️ Split markers not found — installing manually..."
    # tiger-brain: first block up to second [Unit]
    sed -n '1,/^$/p; /^# --- /q' "$SERVICE_SRC" 2>/dev/null | sudo tee "$SERVICE_DST" >/dev/null || true
fi
# Robust fallback: ensure watchdog unit exists with correct ExecStart
if [ ! -s "$WDOG_DST" ]; then
    sudo tee "$WDOG_DST" >/dev/null <<'WDOG'
[Unit]
Description=Tiger Watchdog — monitors tiger-brain liveness + restarts on stall
After=tiger-brain.service
Wants=tiger-brain.service

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/home/ec2-user/tiger-brain-v6
Environment=TZ=Asia/Kolkata
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 automation/watchdog.py
Restart=always
RestartSec=10
StandardOutput=append:/home/ec2-user/tiger_watchdog.log
StandardError=append:/home/ec2-user/tiger_watchdog.log

[Install]
WantedBy=multi-user.target
WDOG
fi
# tiger-brain unit: ensure it exists (from config file first block)
if [ ! -s "$SERVICE_DST" ]; then
    sudo tee "$SERVICE_DST" >/dev/null <<'TBR'
[Unit]
Description=Tiger Brain V19 — Live Algorithmic Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/home/ec2-user/tiger-brain-v6
Environment=TZ=Asia/Kolkata
Environment=PYTHONUNBUFFERED=1
Environment=DRY_RUN=false
ExecStart=/usr/bin/python3 -m automation.scheduler
Restart=always
RestartSec=10
StandardOutput=append:/home/ec2-user/tiger_v19.log
StandardError=append:/home/ec2-user/tiger_v19.log
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
TBR
fi
sudo systemctl daemon-reload
sudo systemctl enable tiger-brain tiger-watchdog
echo "   ✅ tiger-brain.service enabled"
echo "   ✅ tiger-watchdog.service enabled"
echo ""

# 10. Start Tiger
echo "10. Start Tiger..."
sudo systemctl start tiger-brain
sleep 4
sudo systemctl start tiger-watchdog
sleep 2
echo ""

# 11. Verify
echo "11. Verify..."
echo "   tiger-brain   : $(sudo systemctl is-active tiger-brain)"
echo "   tiger-watchdog: $(sudo systemctl is-active tiger-watchdog)"
echo "   Tiger PID     : $(pgrep -f 'automation.scheduler' || echo NONE)"
echo "   Watchdog PID  : $(pgrep -f 'automation/watchdog' || echo NONE)"
echo "   HEAD          : $(git log --oneline -1)"
echo "   Log           : $LOG"
echo ""
echo "========================================"
echo "🐅 TIGER V19 RECOVER-003a9ff-LIFE DEPLOYED!"
echo "========================================"
echo ""
echo "Commands:"
echo "  Status:  sudo systemctl status tiger-brain"
echo "  Logs:    tail -f $LOG"
echo "  Watchdog: sudo systemctl status tiger-watchdog"
echo "  Restart: sudo systemctl restart tiger-brain"
echo ""
echo "⚠️ Verify first scan at 09:15 IST + watchdog 5-min checks."
