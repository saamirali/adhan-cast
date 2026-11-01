#!/usr/bin/env bash
# Install Adhan service to /opt/adhan-service and enable systemd unit. Usage: sudo ./install.sh [port]
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo"; exit 1; }
PORT="${1:-8080}"
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST=/opt/adhan-service
apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-pip >/dev/null
id adhan >/dev/null 2>&1 || useradd --system --home /var/lib/adhan --shell /usr/sbin/nologin adhan
mkdir -p "$DEST" /var/lib/adhan/media
cp -r "$SRC/app" "$SRC/requirements.txt" "$DEST/"
python3 -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install -q --upgrade pip
"$DEST/venv/bin/pip" install -q -r "$DEST/requirements.txt"
chown -R adhan:adhan /var/lib/adhan
sed "s/ADHAN_PORT=8080/ADHAN_PORT=$PORT/" "$SRC/adhan.service" > /etc/systemd/system/adhan.service
systemctl daemon-reload
systemctl enable --now adhan.service
if command -v ufw >/dev/null && ufw status | grep -q active; then
  ufw allow "$PORT"/tcp comment 'Adhan web UI + media'
  ufw allow 5353/udp comment 'mDNS (Chromecast discovery)'
fi
IP=$(hostname -I | awk '{print $1}')
echo "Installed. Open http://$IP:$PORT  (logs: journalctl -u adhan -f)"
