#!/usr/bin/env bash
#
# One-time server bootstrap for the World Cup 2026 bot.
# Run this ONCE on a fresh Ubuntu/Debian server (as root). After it finishes,
# GitHub Actions will keep the code updated on every push to main.
#
#   curl -fsSL https://raw.githubusercontent.com/Yoosofbidardel/worldcup-2026-sheets-prediction-bot/aria/deploy/setup-server.sh | bash
#   # ...or copy this file up and: bash setup-server.sh
#
# Override defaults with env vars, e.g.:
#   DEPLOY_DIR=/opt/wc2026-leagueB SERVICE_NAME=wc2026-b bash setup-server.sh
set -euo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-/opt/wc2026}"
SERVICE_NAME="${SERVICE_NAME:-wc2026}"

echo "==> Installing system packages (python3, venv, pip, rsync, git)…"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip rsync git

echo "==> Creating $DEPLOY_DIR…"
mkdir -p "$DEPLOY_DIR"
cd "$DEPLOY_DIR"

echo "==> Creating Python virtualenv…"
test -d .venv || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip

echo "==> Installing the systemd service ($SERVICE_NAME)…"
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<UNIT
[Unit]
Description=World Cup 2026 prediction bot ($SERVICE_NAME)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$DEPLOY_DIR
ExecStart=$DEPLOY_DIR/.venv/bin/python main.py
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null

cat <<DONE

✅ Server is bootstrapped.

NEXT (do these once, then you're fully automated):

  1. Put your secrets in $DEPLOY_DIR (these are NEVER in git):
       nano $DEPLOY_DIR/.env                  # tokens, GOOGLE_SHEET_ID, ADMIN_IDS…
       nano $DEPLOY_DIR/service_account.json  # paste the Google key JSON

  2. Add these GitHub repo secrets (Settings → Secrets and variables → Actions):
       SSH_HOST=<this server's IP>
       SSH_USER=root
       SSH_KEY=<the PRIVATE deploy key>     (see DEPLOY.md to generate it)
       DEPLOY_DIR=$DEPLOY_DIR
       SERVICE_NAME=$SERVICE_NAME
       SSH_PORT=22                          (optional)

  3. Push to main (or re-run the Deploy action). It will rsync the code here
     and start the bot. Check it with:
       systemctl status $SERVICE_NAME
       journalctl -u $SERVICE_NAME -f

DONE
