#!/bin/bash
# ============================================================
# Tiger Brain V19 — AWS Deploy Script
# ============================================================
# Ye script Tiger ko systemd service ke saath install karta hai
# Taaki server reboot/crash hone pe Tiger apne aap restart ho.
#
# Usage: bash deploy/aws_deploy.sh
# (AWS pe ec2-user se chalao, repo root se)
# ============================================================

set -e

REPO_DIR="/home/ec2-user/tiger-brain-v6"
SERVICE_SRC="$REPO_DIR/deploy/tiger-brain.service"
SERVICE_DST="/etc/systemd/system/tiger-brain.service"

echo "🐅 Tiger Brain V19 — Deploy Script"
echo "=================================="
echo ""

# 1. Timezone IST set karo
echo "1. Timezone IST..."
sudo timedatectl set-timezone Asia/Kolkata
echo "   ✅ Asia/Kolkata"
echo ""

# 2. Latest code pull karo
echo "2. Latest code pull..."
cd "$REPO_DIR"
git fetch origin main
git reset --hard origin/main
echo "   ✅ $(git log --oneline -1)"
echo ""

# 3. Service file install karo
echo "3. systemd service install..."
sudo cp "$SERVICE_SRC" "$SERVICE_DST"
sudo systemctl daemon-reload
sudo systemctl enable tiger-brain
echo "   ✅ tiger-brain.service enabled (auto-start on boot)"
echo ""

# 4. Purani manual Tiger process kill karo (agar chal raha hai)
echo "4. Purani Tiger process kill (agar hai)..."
OLD_PID=$(pgrep -f "automation.scheduler" || true)
if [ -n "$OLD_PID" ]; then
    kill -9 $OLD_PID 2>/dev/null || true
    echo "   ✅ Killed PID $OLD_PID"
else
    echo "   ✅ Koi purana process nahi"
fi
echo ""

# 5. Tiger start karo via systemd
echo "5. Tiger start via systemd..."
sudo systemctl start tiger-brain
sleep 5
STATUS=$(sudo systemctl is-active tiger-brain)
if [ "$STATUS" = "active" ]; then
    echo "   ✅ Tiger running (systemd managed)"
else
    echo "   ❌ Tiger failed to start — check: sudo journalctl -u tiger-brain -f"
    exit 1
fi
echo ""

# 6. Verify
echo "6. Verify..."
echo "   Time: $(date)"
echo "   PID:  $(pgrep -f 'automation.scheduler')"
echo "   Log:  /home/ec2-user/tiger_v19.log"
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
