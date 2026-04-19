#!/bin/bash
set -e
pnpm install --frozen-lockfile
pnpm --filter db push

# Restart the Flask subtitle-burner app so merged Python changes take effect.
# The workflow manager will automatically relaunch the process.
pkill -f "python app.py" || true
