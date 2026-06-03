#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="$ROOT_DIR/.venv/bin/python"

if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "Missing virtualenv Python at $VENV_PYTHON"
  echo "Run: scripts/install_launchd_garmin_sync.sh --email you@example.com"
  exit 1
fi

exec "$VENV_PYTHON" "$ROOT_DIR/scripts/garmin_daily_sync.py" "$@"
