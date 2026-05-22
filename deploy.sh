#!/usr/bin/env bash
# deploy.sh — aktualizuje bota na serveru
#
# Spousti jako botuser v /home/botuser/btc-signal-bot:
#   ./deploy.sh
#
# Co dela:
#   1. git pull (nacte nejnovejsi kod z GitHubu)
#   2. pip install -r requirements.txt (aktualizuje zavislosti)
#   3. python -B -m scripts.test_all (rychly smoke test vsech instrumentu)
#   4. systemctl restart (pokud testy prosly)
#
# Poznamky:
#   - botuser musi mit sudo prava jen pro systemctl restart/status
#     (/etc/sudoers.d/botuser-systemctl)
#   - GIT_SSH_COMMAND pouziva deploy key (~/.ssh/github_deploy)
#   - Skript se zastavi pri prvni chybe (set -euo pipefail)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="btc-signal-bot"
PYTHON="${SCRIPT_DIR}/.venv/bin/python"
GIT_SSH="ssh -i ~/.ssh/github_deploy -o StrictHostKeyChecking=no"

cd "$SCRIPT_DIR"

echo "=============================="
echo "  AI Signal Bot — deploy"
echo "  $(date '+%Y-%m-%d %H:%M:%S UTC')"
echo "=============================="

# 1. Git pull
echo ""
echo "[1/4] git pull..."
GIT_SSH_COMMAND="$GIT_SSH" git pull origin main

# 2. Zavislosti
echo ""
echo "[2/4] pip install -r requirements.txt..."
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

# 3. Testy
echo ""
echo "[3/4] smoke testy (test_all.py)..."
"$PYTHON" -B -m scripts.test_all
echo "  Testy OK."

# 4. Restart sluzby
echo ""
echo "[4/4] systemctl restart $SERVICE_NAME..."
sudo systemctl restart "$SERVICE_NAME"
sleep 2
sudo systemctl status "$SERVICE_NAME" --no-pager -l

echo ""
echo "=============================="
echo "  Deploy OK!"
echo "  Sleduj logy: sudo journalctl -u $SERVICE_NAME -f"
echo "=============================="
