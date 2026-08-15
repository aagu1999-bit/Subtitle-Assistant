#!/bin/bash
set -e
pnpm install --frozen-lockfile
pnpm --filter db push

# Restart the Flask subtitle-burner app so merged Python changes take effect
# (new routes like /polish/run will 404/405 until the process is replaced).
pkill -f "python app.py" || true
pkill -f "gunicorn.*app:app" || true
