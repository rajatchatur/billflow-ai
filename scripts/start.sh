#!/usr/bin/env bash
# Start BillFlow AI locally. Works inside an activated conda env (e.g. aiagents311) or a plain venv.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -f .env ]; then
  echo "No .env found. Copy .env.example to .env and fill in BILLFLOW_SESSION_SECRET and the owner account."
  echo "  cp .env.example .env"
  exit 1
fi
set -a; . ./.env; set +a
PY="${PYTHON:-python}"
exec "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT:-8000}" --reload
