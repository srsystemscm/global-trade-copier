#!/usr/bin/env bash
#
# Provisions a fresh Ubuntu VPS to run the Global Trade Copier hub +
# dashboard, always-on. Idempotent -- safe to re-run after a `git pull` /
# `rsync` to redeploy.
#
# NOT executed against a real VPS anywhere in this project's history --
# there wasn't one available in the dev environment. Written to documented
# Ubuntu/apt/systemd/nginx conventions and reviewed carefully, but treat
# the first real run as a first real run: watch the output, don't walk away.
#
# Usage (run as a sudo-capable user, from inside a checkout of this repo):
#   bash deploy/provision.sh
# (files transferred via git/rsync won't have the executable bit set,
# hence `bash script.sh` rather than `./script.sh`)
#
# Assumes Ubuntu 22.04 or 24.04. Tested on: neither (see above).

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="/opt/tradecopier"
APP_USER="tradecopier"

echo "==> Installing base packages"
sudo apt-get update -y
sudo apt-get install -y curl gnupg ca-certificates software-properties-common ufw rsync

echo "==> Installing Python 3.11"
if ! python3.11 --version >/dev/null 2>&1; then
    sudo add-apt-repository -y ppa:deadsnakes/ppa
    sudo apt-get update -y
    sudo apt-get install -y python3.11 python3.11-venv python3.11-dev
fi
python3.11 --version

echo "==> Installing Node.js LTS"
if ! node --version >/dev/null 2>&1; then
    curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
    sudo apt-get install -y nodejs
fi
node --version

echo "==> Installing nginx"
sudo apt-get install -y nginx

echo "==> Creating service user '${APP_USER}'"
if ! id -u "${APP_USER}" >/dev/null 2>&1; then
    sudo useradd --system --no-create-home --shell /usr/sbin/nologin "${APP_USER}"
fi

echo "==> Syncing repo to ${APP_DIR}"
sudo mkdir -p "${APP_DIR}"
sudo rsync -a --delete \
    --exclude '.venv' --exclude 'node_modules' --exclude 'dist' \
    --exclude '__pycache__' --exclude '*.pyc' \
    --exclude 'hub/data' --exclude 'hub/logs' --exclude '.env' \
    "${REPO_DIR}/hub" "${REPO_DIR}/ui" "${REPO_DIR}/bridge_ea" "${REPO_DIR}/deploy" \
    "${APP_DIR}/"

echo "==> Setting up the hub's Python venv"
if [ ! -d "${APP_DIR}/hub/.venv" ]; then
    sudo python3.11 -m venv "${APP_DIR}/hub/.venv"
fi
sudo "${APP_DIR}/hub/.venv/bin/pip" install --upgrade pip -q
sudo "${APP_DIR}/hub/.venv/bin/pip" install -r "${APP_DIR}/hub/requirements.txt"

echo "==> Setting up .env (won't overwrite an existing one)"
if [ ! -f "${APP_DIR}/hub/.env" ]; then
    sudo cp "${APP_DIR}/hub/.env.example" "${APP_DIR}/hub/.env"
    echo "    Created ${APP_DIR}/hub/.env from the template -- edit it before starting the service."
fi

echo "==> Building the dashboard"
(cd "${APP_DIR}/ui" && sudo npm install && sudo npm run build)

echo "==> Fixing ownership"
sudo mkdir -p "${APP_DIR}/hub/data" "${APP_DIR}/hub/logs"
sudo chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}/hub"

echo "==> Installing the systemd service"
sudo cp "${APP_DIR}/deploy/tradecopier-hub.service" /etc/systemd/system/tradecopier-hub.service
sudo systemctl daemon-reload
sudo systemctl enable tradecopier-hub

echo "==> Installing the nginx site"
sudo cp "${APP_DIR}/deploy/nginx.conf" /etc/nginx/sites-available/tradecopier
sudo ln -sf /etc/nginx/sites-available/tradecopier /etc/nginx/sites-enabled/tradecopier
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx

cat <<EOF

==> Provisioning complete. Before starting the hub:

1. Edit ${APP_DIR}/hub/.env -- Schwab OAuth client ID/secret, SMTP if you
   want email notifications (see hub/.env.example for the full list).
2. Edit ${APP_DIR}/hub/config/slaves.json (or add slaves later through the
   dashboard once it's up) to point at your real MT4/Schwab/IBKR accounts.
3. Run bash deploy/ufw-rules.sh <your-MT4-IP> to open the firewall (SSH, 80/443, and the
   ZMQ ports restricted to wherever your MT4 terminal actually runs).
4. sudo systemctl start tradecopier-hub
5. sudo systemctl status tradecopier-hub   # confirm it's running
   journalctl -u tradecopier-hub -f        # follow its logs
6. Visit http://<this-server's-IP>/ for the dashboard. Set up HTTPS with
   certbot once you have a domain pointed here (see deploy/nginx.conf).

EOF
