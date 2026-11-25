#!/usr/bin/env bash
# Run the full automated test suite (no network, no API keys, no real invoices).
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python}"
exec "$PY" -m pytest tests/ -v
