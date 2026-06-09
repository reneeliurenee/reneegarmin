#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="$ROOT_DIR/.venv/bin/python"

python_can_import_garminconnect() {
  "$1" - <<'PY' >/dev/null 2>&1
import garminconnect  # noqa: F401
PY
}

resolve_python() {
  if [[ -n "${GARMIN_SYNC_PYTHON:-}" ]]; then
    if [[ ! -x "$GARMIN_SYNC_PYTHON" ]]; then
      echo "GARMIN_SYNC_PYTHON is set but is not executable: $GARMIN_SYNC_PYTHON" >&2
      return 1
    fi
    echo "$GARMIN_SYNC_PYTHON"
    return 0
  fi

  if [[ -x "$VENV_PYTHON" ]]; then
    if python_can_import_garminconnect "$VENV_PYTHON"; then
      echo "$VENV_PYTHON"
      return 0
    fi
    echo "Repository virtualenv exists but cannot import garminconnect: $VENV_PYTHON" >&2
    echo "Falling back to a system Python if one has the dependency installed." >&2
  fi

  local candidate
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      candidate="$(command -v "$candidate")"
      if python_can_import_garminconnect "$candidate"; then
        echo "$candidate"
        return 0
      fi
    fi
  done

  cat >&2 <<ERROR
Missing repository virtualenv Python at $VENV_PYTHON, and no system Python with garminconnect was found.

To repair the normal local virtualenv, run:
  python3 -m venv "$ROOT_DIR/.venv"
  "$VENV_PYTHON" -m pip install --upgrade pip
  "$VENV_PYTHON" -m pip install -r "$ROOT_DIR/requirements.txt"

If dependencies are already installed in another interpreter, run with:
  GARMIN_SYNC_PYTHON=/path/to/python $0 [sync options]

If credentials are not configured, reinstall launchd with:
  scripts/install_launchd_garmin_sync.sh --email you@example.com
ERROR
  return 1
}

PYTHON="$(resolve_python)"
exec "$PYTHON" "$ROOT_DIR/scripts/garmin_daily_sync.py" "$@"
