# Garmin Daily Sync Automation

This repository contains a macOS `launchd` automation that syncs Garmin Connect activity and health data daily for marathon-coach handoff.

## Why a Morning Report Can Look Incomplete

If the job runs at about 06:30–07:30 Singapore time and asks Garmin for **today's** calendar date, Garmin may still be processing same-day data. That explains a row like `2026-06-03` containing only early values such as steps, resting HR, max HR, and stress, while sleep, HRV, training readiness, recovery time, acute load, and activities are empty.

The updated workflow fixes this in three ways:

1. It uses `Asia/Singapore` by default when deciding what date is "today".
2. It still fetches recent raw days, but labels the same-day row as `in_progress_today` and treats only previous days as complete by default.
3. It looks back farther for activities so late Garmin uploads after the previous default window are not missed.

## What It Produces

- `data/raw/garmin_YYYY-MM-DD.json`: raw Garmin payloads, including endpoint errors if Garmin returns them
- `data/raw/garmin_activities_START_END.json`: raw recent activities for troubleshooting late uploads
- `data/processed/garmin_daily_health.csv`: normalized health metrics with `data_status`, `core_complete`, `missing_core_fields`, and `diagnostic_note`
- `data/processed/garmin_activities.csv`: normalized activities across all workout types
- `data/processed/coach_summary.md`: compact summary for a marathon coach agent
- `data/marathon_coach/YYYY-MM-DD/`: date-stamped daily files for upload or syncing
- `data/marathon_coach/latest_*`: stable latest-file aliases for a coach agent

## Install

The installer stores your Garmin password in macOS Keychain under service `garminconnect-daily-sync`. It does not write the password into the `launchd` plist.

```bash
scripts/install_launchd_garmin_sync.sh \
  --email your_garmin_email@example.com \
  --time 06:30 \
  --timezone Asia/Singapore
```

## First Run

Run once manually to complete Garmin MFA and create saved tokens:

```bash
scripts/run_garmin_sync_once.sh
```

After that, `launchd` runs it daily using the saved Garmin tokens. The wrapper normally uses the repository virtualenv at `.venv/bin/python`. If that virtualenv is missing or cannot import `garminconnect`, it now falls back to a system `python3`/`python` that already has `garminconnect` installed. For an emergency run with another known-good interpreter, set `GARMIN_SYNC_PYTHON`:

```bash
GARMIN_SYNC_PYTHON=/path/to/python scripts/run_garmin_sync_once.sh
```

## Recommended Daily Behavior

By default the script syncs four recent days ending at today in Singapore time, but exports only completed health rows to the coach summary:

```bash
scripts/run_garmin_sync_once.sh
```

For the June 3, 2026 example, a morning run will still write raw and processed data for `2026-06-03`, but the row is marked `in_progress_today`. The coach summary and `data/marathon_coach/*_garmin_daily_health.csv` export will focus on completed dates through `2026-06-02` unless you opt out.

If you intentionally want the same-day in-progress row in the coach summary, run:

```bash
scripts/run_garmin_sync_once.sh --no-coach-completed-only
```

Useful troubleshooting options:

```bash
# Re-pull the specific June 3 window and include previous days for comparison
scripts/run_garmin_sync_once.sh --end-date 2026-06-03 --days 4

# Increase activity history if a workout was uploaded late
scripts/run_garmin_sync_once.sh --activity-lookback-days 30
```

If the automation log says `.venv/bin/python` is missing and PyPI is temporarily unavailable, first check whether your system Python already has the dependency installed:

```bash
python3 -c "import garminconnect; print('garminconnect ok')"
scripts/run_garmin_sync_once.sh
```

If that import fails, repair the virtualenv when package access is available:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

## Google Drive Desktop Export

If Google Drive for desktop is installed and mounted locally, create a Drive folder named `Marathon Coach`, then reinstall with:

```bash
scripts/install_launchd_garmin_sync.sh \
  --email your_garmin_email@example.com \
  --time 06:30 \
  --timezone Asia/Singapore \
  --drive-output-dir "/path/to/Google Drive/My Drive/Marathon Coach"
```

Each run copies a dated folder plus `latest_*` files into that Drive folder.

## Logs

- `logs/garmin_daily_sync.out.log`
- `logs/garmin_daily_sync.err.log`

## Uninstall

```bash
scripts/uninstall_launchd_garmin_sync.sh
```
