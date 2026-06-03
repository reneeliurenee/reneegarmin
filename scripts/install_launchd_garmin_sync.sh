#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.liur.garmin-daily-sync"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
KEYCHAIN_SERVICE="garminconnect-daily-sync"
RUN_TIME="06:30"
EMAIL=""
DRIVE_OUTPUT_DIR=""
TIMEZONE="Asia/Singapore"

usage() {
  cat <<USAGE
Usage: $0 --email you@example.com [--time HH:MM] [--drive-output-dir DIR] [--timezone Asia/Singapore]
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --email) EMAIL="$2"; shift 2 ;;
    --time) RUN_TIME="$2"; shift 2 ;;
    --drive-output-dir) DRIVE_OUTPUT_DIR="$2"; shift 2 ;;
    --timezone) TIMEZONE="$2"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ -z "$EMAIL" ]]; then
  echo "--email is required" >&2
  usage
  exit 2
fi

IFS=: read -r HOUR MINUTE <<< "$RUN_TIME"
if [[ ! "$HOUR" =~ ^[0-9]{1,2}$ || ! "$MINUTE" =~ ^[0-9]{2}$ ]]; then
  echo "--time must be HH:MM" >&2
  exit 2
fi

python3 -m venv "$ROOT_DIR/.venv"
"$ROOT_DIR/.venv/bin/python" -m pip install --upgrade pip
"$ROOT_DIR/.venv/bin/python" -m pip install -r "$ROOT_DIR/requirements.txt"

mkdir -p "$ROOT_DIR/logs" "$HOME/Library/LaunchAgents"

if ! security find-generic-password -a "$EMAIL" -s "$KEYCHAIN_SERVICE" >/dev/null 2>&1; then
  echo "Store Garmin password in macOS Keychain for $EMAIL"
  security add-generic-password -a "$EMAIL" -s "$KEYCHAIN_SERVICE" -w
fi

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$ROOT_DIR/scripts/run_garmin_sync_once.sh</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$ROOT_DIR</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>GARMIN_EMAIL</key>
    <string>$EMAIL</string>
    <key>GARMIN_KEYCHAIN_SERVICE</key>
    <string>$KEYCHAIN_SERVICE</string>
    <key>GARMIN_SYNC_TIMEZONE</key>
    <string>$TIMEZONE</string>
PLIST

if [[ -n "$DRIVE_OUTPUT_DIR" ]]; then
  cat >> "$PLIST" <<PLIST
    <key>MARATHON_COACH_DRIVE_DIR</key>
    <string>$DRIVE_OUTPUT_DIR</string>
PLIST
fi

cat >> "$PLIST" <<PLIST
  </dict>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>$HOUR</integer>
    <key>Minute</key>
    <integer>$MINUTE</integer>
  </dict>
  <key>RunAtLoad</key>
  <false/>
  <key>StandardOutPath</key>
  <string>$ROOT_DIR/logs/garmin_daily_sync.out.log</string>
  <key>StandardErrorPath</key>
  <string>$ROOT_DIR/logs/garmin_daily_sync.err.log</string>
</dict>
</plist>
PLIST

plutil -lint "$PLIST"
launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL"

echo "Installed launchd job: $LABEL"
echo "Schedule: daily at $RUN_TIME local time ($TIMEZONE data window)"
echo "Plist: $PLIST"
echo "Run once interactively to complete MFA/token setup if needed:"
echo "  $ROOT_DIR/scripts/run_garmin_sync_once.sh"
