#!/bin/bash
# Restart Subtitle Burner Flask so new routes (e.g. /polish/run) load after git pull.
set -e
cd "$(dirname "$0")/.."

echo "[restart-flask] killing old Flask / gunicorn…"
pkill -f "python app.py" 2>/dev/null || true
pkill -f "gunicorn.*app:app" 2>/dev/null || true
sleep 1

echo "[restart-flask] starting on PORT=${PORT:-8081}"
export PORT="${PORT:-8081}"
exec python app.py
