#!/bin/bash
# =============================================================================
# create_teacher_account.sh
# Creates a read-only "ucitel" (teacher) account on the Hetzner server.
#
# What the teacher CAN see:
#   - All Python source code (bot/, scripts/, tests/)
#   - README.md, requirements.txt, docs/
#   - analysis/ notebooks
#   - Systemd service logs (journalctl)
#   - Bot SQLite database (read-only)
#
# What the teacher CANNOT see:
#   - .env file (API keys — Alpaca, Anthropic, Telegram)
#   - Any other secrets
#
# Run as root on the server:
#   sudo bash create_teacher_account.sh
# =============================================================================

set -e

BOT_HOME="/home/botuser/btc-signal-bot"
TEACHER_USER="ucitel"
TEACHER_PASS="JEM207bot2026"   # change if you want

echo "=== Creating teacher account: $TEACHER_USER ==="

# 1. Create user (no sudo, no shell login beyond bash)
if id "$TEACHER_USER" &>/dev/null; then
    echo "User $TEACHER_USER already exists — skipping creation"
else
    useradd -m -s /bin/bash "$TEACHER_USER"
    echo "$TEACHER_USER:$TEACHER_PASS" | chpasswd
    echo "Created user $TEACHER_USER"
fi

# 2. Give teacher read access to the bot directory
#    botuser owns the files, we add ucitel to a shared group.
#    The bot dir is chmod 750 by default; we open it to group read.

# Create shared group if not exists
if ! getent group botreaders &>/dev/null; then
    groupadd botreaders
    echo "Created group: botreaders"
fi

# Add both users to the group
usermod -aG botreaders botuser
usermod -aG botreaders "$TEACHER_USER"

# Make bot directory group-readable (recursively)
chgrp -R botreaders "$BOT_HOME"
chmod -R g+rX "$BOT_HOME"       # r = read files, X = enter dirs (not execute scripts)

# 3. PROTECT .env — remove group read from .env specifically
#    .env should already be 600 (only botuser can read), but let's be explicit.
if [ -f "$BOT_HOME/.env" ]; then
    chmod 600 "$BOT_HOME/.env"
    chown botuser:botuser "$BOT_HOME/.env"
    echo "Protected .env: chmod 600"
fi

# Also protect any backup .env files
find "$BOT_HOME" -name ".env*" -exec chmod 600 {} \; 2>/dev/null || true

# 4. Give teacher access to systemd journal logs for the bot service
#    The systemd-journal group allows reading journalctl
usermod -aG systemd-journal "$TEACHER_USER"
echo "Added $TEACHER_USER to systemd-journal group"

# 5. Set up SSH key authentication (optional, if teacher provides a public key)
#    For now we enable password auth — teacher can SSH with password above.
#    To add an SSH key later:
#      mkdir -p /home/ucitel/.ssh
#      echo "ssh-rsa AAAA..." >> /home/ucitel/.ssh/authorized_keys
#      chmod 700 /home/ucitel/.ssh
#      chmod 600 /home/ucitel/.ssh/authorized_keys
#      chown -R ucitel:ucitel /home/ucitel/.ssh

# 6. Add a helpful welcome message for the teacher
cat > /home/$TEACHER_USER/.bashrc << 'EOF'
# Welcome message for teacher account
echo ""
echo "=== AI Signal Bot — Teacher Read-Only Access ==="
echo "Bot directory:   /home/botuser/btc-signal-bot/"
echo "View bot code:   ls /home/botuser/btc-signal-bot/"
echo "View live logs:  journalctl -u btc-signal-bot -f"
echo "Past logs:       journalctl -u btc-signal-bot --since '1 hour ago'"
echo "Bot status:      systemctl status btc-signal-bot"
echo ""
echo "NOTE: .env file (API keys) is NOT accessible from this account."
echo ""
EOF
chown $TEACHER_USER:$TEACHER_USER /home/$TEACHER_USER/.bashrc

echo ""
echo "=== DONE ==="
echo ""
echo "Teacher account created:"
echo "  SSH:      ssh $TEACHER_USER@167.235.155.88"
echo "  Password: $TEACHER_PASS"
echo ""
echo "Teacher can read:"
echo "  /home/botuser/btc-signal-bot/   (all code, docs, notebooks)"
echo "  journalctl -u btc-signal-bot    (service logs)"
echo ""
echo "Teacher CANNOT read:"
echo "  /home/botuser/btc-signal-bot/.env  (API keys protected)"
echo ""
echo "Verify .env is protected:"
echo "  sudo -u $TEACHER_USER cat /home/botuser/btc-signal-bot/.env"
echo "  → should show: Permission denied"
