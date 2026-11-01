#!/usr/bin/env bash
# Update an installed Adhan service in place. Keeps /var/lib/adhan (settings.json + MP3s). Usage: sudo ./update.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo"; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST=/opt/adhan-service
[ -d "$DEST/venv" ] || { echo "Not installed; run sudo ./install.sh first"; exit 1; }
trap 'systemctl daemon-reload || true; systemctl restart adhan.service || true' EXIT
rm -rf "$DEST/app"
cp -r "$SRC/app" "$SRC/requirements.txt" "$DEST/"
find "$DEST/app" -name __pycache__ -prune -exec rm -rf {} +
"$DEST/venv/bin/pip" install -q -r "$DEST/requirements.txt" || echo "WARNING: pip install failed; continuing with existing packages"
# refresh the unit file but keep the configured port
PORT=$(grep -oP 'ADHAN_PORT=\K[0-9]+' /etc/systemd/system/adhan.service 2>/dev/null || echo 8080)
sed "s/ADHAN_PORT=8080/ADHAN_PORT=$PORT/" "$SRC/adhan.service" > /etc/systemd/system/adhan.service
systemctl daemon-reload
systemctl restart adhan.service
sleep 2; systemctl --no-pager --lines=5 status adhan.service || true
echo "Updated. Data in /var/lib/adhan was kept."
