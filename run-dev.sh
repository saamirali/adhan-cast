#!/usr/bin/env bash
# Run without installing (data in ~/.local/share/adhan). Set ADHAN_MOCK_CAST=1 to log instead of casting.
cd "$(dirname "$0")"
[ -d venv ] || { python3 -m venv venv && ./venv/bin/pip install -r requirements.txt; }
exec ./venv/bin/python -m app.main
