#!/bin/sh
set -eu

echo "[startup] Starting ClipShortener API..."
echo "[startup] Acquisition handled by external acquisition service."

exec python3 -m uvicorn app:app \
  --host 0.0.0.0 \
  --port "${PORT:-10000}"
