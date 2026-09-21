#!/bin/bash
# ============================================================
# Tiger Brain V19 — AWS Deploy Script
# ============================================================
# Ye script Tiger ko systemd service ke saath install karta hai
# Taaki server reboot/crash hone pe Tiger apne aap restart ho.
#
# Usage: bash deploy/aws_deploy.sh [branch]
#   branch — deploy karne wali git branch (default: main)
# (AWS pe ec2-user se chalao, repo root se)
# ============================================================

set -e

REPO_DIR="/home/ec2-user/tiger-brain-v6"
SERVICE_SRC="$REPO_DIR/deploy/tiger-brain.service"
SERVICE_DST="/etc/systemd/system/tiger-brain.service"
BRANCH="${1:-main}"

echo "🐅 Tiger Brain V19 — Deploy Script"
echo "=================================="
echo ""

# 1. Timezone IST set karo
echo "1. Timezone IST..."
sudo timedatectl set-timezone Asia/Kolkata
echo "   ✅ Asia/Kolkata"
echo ""

# 2. NTP time sync — TOTP + WebSocket ke liye MANDATORY.
#    Agar clock drift ho (EC2 pe aam), Angel One TOTP reject karta hai
#    (har 60s badalta hai, drift > ~30s = invalid) aur WS ticks freeze
#    ho jaate hain. chrony install + force step karo.
echo "2. NTP time sync (chrony)..."
if ! command -v chronyc >/dev/null 2>&1; then
    sudo dnf install -y chrony 2>/dev/null || sudo yum install -y chrony || \
        sudo apt-get install -y chrony
fi
sudo systemctl enable chronyd 2>/dev/null || true
sudo systemctl restart chronyd 2>/dev/null || true
sleep 3
# Drift ko turant zero karo (slew nahi — instant step).
sudo chronyc makestep 2>/dev/null || true
echo "   Time : $(date '+%Y-%m-%d %H:%M:%S %Z')"
chronyc tracking 2>/dev/null | grep -E "System time|Last offset|Leap" || true
echo "   ✅ Clock synced"
echo ""

# 3. Python dependencies (ML ensemble included)
echo "3. Python dependencies..."
cd "$REPO_DIR"
/usr/bin/python3 -m pip install --quiet --upgrade pip 2>/dev/null || true
/usr/bin/python3 -m pip install --quiet -r requirements.txt
echo "   ✅ requirements.txt installed"
echo ""

# 4. Latest code pull karo (deploy branch)
echo "4. Latest code pull (branch: $BRANCH)..."
git fetch origin "$BRANCH"
git reset --hard "origin/$BRANCH"
echo "   ✅ $(git log --oneline -1)"
echo ""

# 5. Service file install karo
echo "5. systemd service install..."
sudo cp "$SERVICE_SRC" "$SERVICE_DST"
sudo systemctl daemon-reload
sudo systemctl enable tiger-brain
echo "   ✅ tiger-brain.service enabled (auto-start on boot)"
echo ""

# 6. Purani manual Tiger process kill karo (agar chal raha hai)
echo "6. Purani Tiger process kill (agar hai)..."
OLD_PID=$(pgrep -f "automation.scheduler" || true)
if [ -n "$OLD_PID" ]; then
    kill -9 $OLD_PID 2>/dev/null || true
    echo "   ✅ Killed PID $OLD_PID"
else
    echo "   ✅ Koi purana process nahi"
fi
echo ""

# 7. Tiger start karo via systemd
echo "7. Tiger start via systemd..."
sudo systemctl restart tiger-brain
sleep 5
STATUS=$(sudo systemctl is-active tiger-brain)
if [ "$STATUS" = "active" ]; then
    echo "   ✅ Tiger running (systemd managed)"
else
    echo "   ❌ Tiger failed to start — check: sudo journalctl -u tiger-brain -f"
    exit 1
fi
echo ""

# 8. Verify
echo "8. Verify..."
echo "   Time: $(date)"
echo "   PID:  $(pgrep -f 'automation.scheduler')"
echo "   Log:  /home/ec2-user/tiger_v19.log"
echo "   Branch: $(git rev-parse --abbrev-ref HEAD) @ $(git rev-parse --short HEAD)"
echo ""
echo "=================================="
echo "🐅 TIGER V19 DEPLOYED!"
echo "=================================="
echo ""
echo "Commands:"
echo "  Status:  sudo systemctl status tiger-brain"
echo "  Stop:    sudo systemctl stop tiger-brain"
echo "  Start:   sudo systemctl start tiger-brain"
echo "  Restart: sudo systemctl restart tiger-brain"
echo "  Logs:    tail -f /home/ec2-user/tiger_v19.log"
echo ""
echo "⚠️  Tiger ab systemd-managed hai:"
echo "   - Server reboot → Tiger apne aap start"
echo "   - Crash → 10s me auto-restart"
echo "   - Pre-market 09:00 kabhi nahi chhootega"

