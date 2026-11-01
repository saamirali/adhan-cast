#!/usr/bin/env bash
# Usage: sudo ./uninstall.sh [--purge]   (--purge also deletes /var/lib/adhan settings + MP3s and the adhan user)
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo"; exit 1; }
systemctl disable --now adhan.service 2>/dev/null || true
rm -f /etc/systemd/system/adhan.service && systemctl daemon-reload
rm -rf /opt/adhan-service
if [ "${1:-}" = "--purge" ]; then rm -rf /var/lib/adhan; userdel adhan 2>/dev/null || true; echo "Purged data."; else echo "Data kept in /var/lib/adhan"; fi
echo "Uninstalled."
