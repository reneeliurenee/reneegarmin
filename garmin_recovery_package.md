# Garmin Marathon Coach Workflow Recovery Package
Generated from local workspace `/Users/liur/Documents/Codex/2026-05-29/prior-conversation-with-codex-conversation-role` on 2026-05-31. The current recovery folder was empty, and the source folder is not a git repository, so no git patch is available.
## Files to create or change
### `README.md`
```markdown
# Garmin Daily Sync Automation

This folder contains a macOS `launchd` automation that syncs Garmin Connect activity and health data daily.

## What It Produces

- `data/raw/garmin_YYYY-MM-DD.json`: raw Garmin payloads
- `data/processed/garmin_daily_health.csv`: normalized health metrics
- `data/processed/garmin_activities.csv`: normalized activities across all workout types
- `data/processed/coach_summary.md`: compact summary for a marathon coach agent
- `data/marathon_coach/YYYY-MM-DD/`: date-stamped daily files for upload or syncing
- `data/marathon_coach/latest_*`: stable latest-file aliases for a coach agent

## Install

The installer uses the latest `garminconnect` package when Python 3.12+ is available. On this Mac's built-in Python 3.9, it installs the newest compatible `garminconnect` release instead.

```bash
scripts/install_launchd_garmin_sync.sh --email your_garmin_email@example.com --time 06:30
```

The installer stores your Garmin password in macOS Keychain under service `garminconnect-daily-sync`. It does not write the password into the `launchd` plist.

## First Run

Run once manually to complete Garmin MFA and create saved tokens:

```bash
scripts/run_garmin_sync_once.sh
```

After that, `launchd` runs it daily using the saved Garmin tokens.

## Google Drive Desktop Export

If Google Drive for desktop is installed and mounted locally, create a Drive folder named `Marathon Coach`, then reinstall with:

```bash
scripts/install_launchd_garmin_sync.sh \
  --email your_garmin_email@example.com \
  --time 06:30 \
  --drive-output-dir "/path/to/Google Drive/My Drive/Marathon Coach"
```

Each run will copy a dated folder plus `latest_*` files into that Drive folder.

## Logs

- `logs/garmin_daily_sync.out.log`
- `logs/garmin_daily_sync.err.log`

## Uninstall

```bash
scripts/uninstall_launchd_garmin_sync.sh
```
```
### `requirements.txt`
```text
garminconnect>=0.3.3; python_version >= "3.12"
garminconnect==0.2.8; python_version < "3.12"
curl_cffi
```
### `scripts/garmin_daily_sync.py`
```python
#!/usr/bin/env python3
import argparse
import csv
import inspect
import json
import os
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from garminconnect import Garmin


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOKENSTORE = Path.home() / ".garminconnect"
DEFAULT_KEYCHAIN_SERVICE = "garminconnect-daily-sync"
GARMIN_ACTIVITY_EXPORT_COLUMNS = [
    "Activity ID",
    "Activity Type",
    "Date",
    "Favorite",
    "Title",
    "Distance",
    "Calories",
    "Time",
    "Avg HR",
    "Max HR",
    "Aerobic TE",
    "Avg Run Cadence",
    "Max Run Cadence",
    "Avg Pace",
    "Best Pace",
    "Total Ascent",
    "Total Descent",
    "Avg Stride Length",
    "Avg Vertical Ratio",
    "Avg Vertical Oscillation",
    "Avg Step Speed Loss",
    "Avg Step Speed Loss %",
    "Avg Ground Contact Time",
    "Avg GCT Balance",
    "Avg GAP",
    "Normalized Power® (NP®)",
    "Training Stress Score®",
    "Avg Power",
    "Max Power",
    "Steps",
    "Total Reps",
    "Total Sets",
    "Body Battery Drain",
    "Min Temp",
    "Decompression",
    "Best Lap Time",
    "Number of Laps",
    "Max Temp",
    "Avg Resp",
    "Min Resp",
    "Max Resp",
    "Moving Time",
    "Elapsed Time",
    "Min Elevation",
    "Max Elevation",
]


def json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def get_keychain_password(email: str, service: str) -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-a", email, "-s", service, "-w"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"No Garmin password found in macOS Keychain for account '{email}' "
            f"and service '{service}'. Run scripts/install_launchd_garmin_sync.sh again."
        )
    return result.stdout.rstrip("\n")


def login(email: str, password: str, tokenstore: Path) -> Garmin:
    def prompt_mfa() -> str:
        if not sys.stdin.isatty():
            raise RuntimeError(
                "Garmin MFA is required, but this launchd run has no interactive terminal. "
                "Run scripts/run_garmin_sync_once.sh manually first to create login tokens."
            )
        return input("Garmin MFA code: ")

    tokenstore = tokenstore.expanduser()
    tokenstore.mkdir(parents=True, exist_ok=True)

    garmin_signature = inspect.signature(Garmin)
    if "prompt_mfa" in garmin_signature.parameters:
        client = Garmin(email, password, prompt_mfa=prompt_mfa)
        client.login(str(tokenstore))
        return client

    client = Garmin(email, password)
    has_saved_tokens = any(tokenstore.iterdir())
    if has_saved_tokens:
        client.login(str(tokenstore))
    else:
        client.login()
        if hasattr(client, "garth") and hasattr(client.garth, "dump"):
            client.garth.dump(str(tokenstore))
    return client


def safe_call(name: str, fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as exc:
        return {"_error": f"{type(exc).__name__}: {exc}"}


def get_recent_activities_for_dates(client: Garmin, start: date, end: date) -> list[dict[str, Any]]:
    if hasattr(client, "get_activities_by_date"):
        data = safe_call(
            "get_activities_by_date",
            lambda: client.get_activities_by_date(start.isoformat(), end.isoformat()),
        )
        return data if isinstance(data, list) else []

    data = safe_call("get_activities", lambda: client.get_activities(0, 100))
    if not isinstance(data, list):
        return []

    activities = []
    for activity in data:
        if not isinstance(activity, dict):
            continue
        started = activity.get("startTimeLocal") or activity.get("startTimeGMT") or ""
        activity_date = started.split("T")[0].split(" ")[0]
        if start.isoformat() <= activity_date <= end.isoformat():
            activities.append(activity)
    return activities


def collect_day(client: Garmin, day: date) -> dict[str, Any]:
    day_s = day.isoformat()
    week_start = (day - timedelta(days=7)).isoformat()

    return {
        "date": day_s,
        "daily": {
            "stats": safe_call("get_stats", lambda: client.get_stats(day_s)),
            "user_summary": safe_call("get_user_summary", lambda: client.get_user_summary(day_s)),
            "stats_and_body": safe_call("get_stats_and_body", lambda: client.get_stats_and_body(day_s)),
            "steps": safe_call("get_steps_data", lambda: client.get_steps_data(day_s)),
            "heart_rates": safe_call("get_heart_rates", lambda: client.get_heart_rates(day_s)),
            "resting_heart_rate": safe_call(
                "get_resting_heart_rate", lambda: client.get_resting_heart_rate(day_s)
            ),
            "sleep": safe_call("get_sleep_data", lambda: client.get_sleep_data(day_s)),
            "stress": safe_call("get_all_day_stress", lambda: client.get_all_day_stress(day_s)),
            "hrv": safe_call("get_hrv_data", lambda: client.get_hrv_data(day_s)),
            "training_readiness": safe_call(
                "get_training_readiness", lambda: client.get_training_readiness(day_s)
            ),
            "training_status": safe_call("get_training_status", lambda: client.get_training_status(day_s)),
            "body_battery": safe_call(
                "get_body_battery", lambda: client.get_body_battery(week_start, day_s)
            ),
            "intensity_minutes": safe_call(
                "get_intensity_minutes_data", lambda: client.get_intensity_minutes_data(day_s)
            ),
            "body_composition": safe_call(
                "get_body_composition", lambda: client.get_body_composition(day_s)
            ),
        },
    }


def nested_get(data: Any, *keys: str, default: Any = "") -> Any:
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return default if current is None else current


def first_dict(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict) and "_error" not in value:
            return value
    return {}


def hrv_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or "_error" in value:
        return {}
    summary = value.get("hrvSummary")
    if isinstance(summary, dict):
        return summary
    return value


def latest_training_readiness(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and "_error" not in value:
        return value
    if not isinstance(value, list):
        return {}

    candidates = [item for item in value if isinstance(item, dict)]
    if not candidates:
        return {}

    def sort_key(item: dict[str, Any]) -> str:
        return str(item.get("timestampLocal") or item.get("timestamp") or item.get("calendarDate") or "")

    return sorted(candidates, key=sort_key)[-1]


def seconds_to_hours(value: Any) -> str:
    try:
        return f"{float(value) / 3600:.2f}"
    except (TypeError, ValueError):
        return ""


def missing(value: str = "--") -> str:
    return value


def format_bool(value: Any) -> str:
    if value is None:
        return missing()
    return str(bool(value)).lower()


def format_number(value: Any, decimals: Optional[int] = None) -> str:
    if value is None or value == "":
        return missing()
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if decimals is None:
        if number.is_integer():
            return str(int(number))
        return str(number)
    return f"{number:.{decimals}f}"


def format_int_with_commas(value: Any) -> str:
    if value is None or value == "":
        return missing()
    try:
        return f"{int(round(float(value))):,}"
    except (TypeError, ValueError):
        return str(value)


def format_duration(seconds: Any, tenths: bool = False) -> str:
    if seconds is None or seconds == "":
        return missing()
    try:
        total = float(seconds)
    except (TypeError, ValueError):
        return str(seconds)

    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    whole_seconds = int(total % 60)
    if tenths:
        tenth = int(round((total - int(total)) * 10))
        if tenth == 10:
            whole_seconds += 1
            tenth = 0
        return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{tenth}"
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}"


def format_pace_from_speed(speed_mps: Any) -> str:
    if speed_mps is None or speed_mps == "":
        return missing()
    try:
        speed = float(speed_mps)
    except (TypeError, ValueError):
        return str(speed_mps)
    if speed <= 0:
        return missing()
    seconds_per_km = 1000 / speed
    minutes = int(seconds_per_km // 60)
    seconds = int(round(seconds_per_km % 60))
    if seconds == 60:
        minutes += 1
        seconds = 0
    return f"{minutes}:{seconds:02d}"


def format_distance_km(distance_m: Any) -> str:
    if distance_m is None or distance_m == "":
        return missing()
    try:
        return f"{float(distance_m) / 1000:.2f}"
    except (TypeError, ValueError):
        return str(distance_m)


def format_activity_type(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("typeKey") or value.get("typeId") or ""
    if not value:
        return missing()
    text = str(value).replace("_", " ").strip()
    return " ".join(part.capitalize() for part in text.split())


def format_gct_balance(value: Any) -> str:
    if value is None or value == "":
        return missing()
    try:
        left = float(value)
    except (TypeError, ValueError):
        return str(value)
    right = 100 - left
    return f"{left:.1f}% L / {right:.1f}% R"


def daily_row(record: dict[str, Any]) -> dict[str, Any]:
    daily = record.get("daily", {})
    summary = first_dict(daily.get("user_summary"), daily.get("stats"))
    hr = first_dict(daily.get("heart_rates"))
    sleep = first_dict(daily.get("sleep"))
    sleep_dto = nested_get(sleep, "dailySleepDTO", default={})
    stress = first_dict(daily.get("stress"))
    hrv = hrv_summary(daily.get("hrv"))
    hrv_baseline = hrv.get("baseline") if isinstance(hrv.get("baseline"), dict) else {}
    readiness = latest_training_readiness(daily.get("training_readiness"))

    return {
        "date": record["date"],
        "steps": summary.get("totalSteps", ""),
        "total_distance_m": summary.get("totalDistanceMeters", ""),
        "active_kcal": summary.get("activeKilocalories", ""),
        "total_kcal": summary.get("totalKilocalories", ""),
        "resting_hr": hr.get("restingHeartRate", ""),
        "max_hr": hr.get("maxHeartRate", ""),
        "avg_stress": stress.get("avgStressLevel", ""),
        "sleep_hours": seconds_to_hours(nested_get(sleep_dto, "sleepTimeSeconds")),
        "deep_sleep_hours": seconds_to_hours(nested_get(sleep_dto, "deepSleepSeconds")),
        "hrv_status": hrv.get("status", ""),
        "hrv_avg": hrv.get("lastNightAvg", ""),
        "hrv_weekly_avg": hrv.get("weeklyAvg", ""),
        "hrv_last_night_5_min_high": hrv.get("lastNight5MinHigh", ""),
        "hrv_baseline_balanced_low": hrv_baseline.get("balancedLow", ""),
        "hrv_baseline_balanced_upper": hrv_baseline.get("balancedUpper", ""),
        "hrv_feedback": hrv.get("feedbackPhrase", ""),
        "training_readiness_score": readiness.get("score", ""),
        "training_readiness_level": readiness.get("level", ""),
        "training_readiness_feedback_short": readiness.get("feedbackShort", ""),
        "training_readiness_feedback_long": readiness.get("feedbackLong", ""),
        "training_readiness_sleep_score": readiness.get("sleepScore", ""),
        "training_readiness_recovery_time": readiness.get("recoveryTime", ""),
        "training_readiness_acute_load": readiness.get("acuteLoad", ""),
        "training_readiness_hrv_factor_percent": readiness.get("hrvFactorPercent", ""),
        "training_readiness_hrv_factor_feedback": readiness.get("hrvFactorFeedback", ""),
        "training_readiness_sleep_factor_feedback": readiness.get("sleepScoreFactorFeedback", ""),
        "training_readiness_recovery_factor_feedback": readiness.get("recoveryTimeFactorFeedback", ""),
        "training_readiness_acwr_factor_feedback": readiness.get("acwrFactorFeedback", ""),
        "training_readiness_stress_factor_feedback": readiness.get("stressHistoryFactorFeedback", ""),
        "training_readiness_sleep_history_factor_feedback": readiness.get("sleepHistoryFactorFeedback", ""),
        "training_readiness_timestamp_local": readiness.get("timestampLocal", ""),
    }


def activity_row(activity: dict[str, Any]) -> dict[str, Any]:
    return {
        "Activity ID": activity.get("activityId") or activity.get("id") or "",
        "Activity Type": format_activity_type(activity.get("activityType")),
        "Date": activity.get("startTimeLocal", missing()),
        "Favorite": format_bool(activity.get("favorite")),
        "Title": activity.get("activityName", missing()),
        "Distance": format_distance_km(activity.get("distance")),
        "Calories": format_number(activity.get("calories"), 0),
        "Time": format_duration(activity.get("duration")),
        "Avg HR": format_number(activity.get("averageHR") or activity.get("avgHR"), 0),
        "Max HR": format_number(activity.get("maxHR"), 0),
        "Aerobic TE": format_number(activity.get("aerobicTrainingEffect"), 1),
        "Avg Run Cadence": format_number(activity.get("averageRunningCadenceInStepsPerMinute"), 0),
        "Max Run Cadence": format_number(activity.get("maxRunningCadenceInStepsPerMinute"), 0),
        "Avg Pace": format_pace_from_speed(activity.get("averageSpeed")),
        "Best Pace": format_pace_from_speed(activity.get("maxSpeed")),
        "Total Ascent": format_number(activity.get("elevationGain"), 0),
        "Total Descent": format_number(activity.get("elevationLoss"), 0),
        "Avg Stride Length": format_number(activity.get("avgStrideLength"), 2),
        "Avg Vertical Ratio": format_number(activity.get("avgVerticalRatio"), 1),
        "Avg Vertical Oscillation": format_number(activity.get("avgVerticalOscillation"), 1),
        "Avg Step Speed Loss": format_number(activity.get("avgStepSpeedLoss"), 1),
        "Avg Step Speed Loss %": format_number(activity.get("avgStepSpeedLossPercent"), 2),
        "Avg Ground Contact Time": format_number(activity.get("avgGroundContactTime"), 0),
        "Avg GCT Balance": format_gct_balance(activity.get("avgGroundContactBalance")),
        "Avg GAP": format_pace_from_speed(activity.get("avgGradeAdjustedSpeed")),
        "Normalized Power® (NP®)": format_number(activity.get("normPower"), 0),
        "Training Stress Score®": format_number(activity.get("trainingStressScore"), 1),
        "Avg Power": format_number(activity.get("avgPower"), 0),
        "Max Power": format_number(activity.get("maxPower"), 0),
        "Steps": format_int_with_commas(activity.get("steps")),
        "Total Reps": format_number(activity.get("totalReps"), 0),
        "Total Sets": format_number(activity.get("totalSets"), 0),
        "Body Battery Drain": format_number(activity.get("differenceBodyBattery"), 0),
        "Min Temp": format_number(activity.get("minTemperature"), 1),
        "Decompression": "Yes" if activity.get("decoDive") else "No",
        "Best Lap Time": format_duration(activity.get("minActivityLapDuration"), tenths=True),
        "Number of Laps": format_number(activity.get("lapCount"), 0),
        "Max Temp": format_number(activity.get("maxTemperature"), 1),
        "Avg Resp": format_number(activity.get("avgRespirationRate"), 0),
        "Min Resp": format_number(activity.get("minRespirationRate"), 0),
        "Max Resp": format_number(activity.get("maxRespirationRate"), 0),
        "Moving Time": format_duration(activity.get("movingDuration")),
        "Elapsed Time": format_duration(activity.get("elapsedDuration")),
        "Min Elevation": format_number(activity.get("minElevation"), 0),
        "Max Elevation": format_number(activity.get("maxElevation"), 0),
    }


def upsert_csv(
    path: Path,
    key: str,
    rows: list[dict[str, Any]],
    preferred_fieldnames: Optional[list[str]] = None,
) -> None:
    if not rows:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, dict[str, Any]] = {}
    fieldnames = preferred_fieldnames or list(rows[0].keys())

    if path.exists():
        with path.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames and preferred_fieldnames is None:
                fieldnames = list(dict.fromkeys([*reader.fieldnames, *fieldnames]))
            for row in reader:
                row_key = row.get(key)
                if row_key:
                    existing[row_key] = row

    for row in rows:
        row_key = str(row.get(key, ""))
        if row_key:
            existing[row_key] = row

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row_key in sorted(existing):
            writer.writerow(existing[row_key])


def write_coach_summary(path: Path, daily_rows: list[dict[str, Any]], activities: list[dict[str, Any]]) -> None:
    lines = [
        "# Garmin Daily Coach Summary",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "## Daily Health",
    ]
    for row in daily_rows:
        lines.append(
            "- {date}: steps={steps}, resting_hr={resting_hr}, sleep_hours={sleep_hours}, "
            "avg_stress={avg_stress}, hrv_avg={hrv_avg}, readiness={training_readiness_score}".format(**row)
        )

    lines.extend(["", "## Activities"])
    for activity in sorted(activities, key=lambda item: item.get("Date", "")):
        lines.append(
            "- {Date} | {Activity Type} | {Title} | Time={Time} | "
            "Distance={Distance} | Avg HR={Avg HR}".format(**activity)
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_marathon_coach_exports(processed_dir: Path, export_root: Path, stamp: str) -> Path:
    dated_dir = export_root / stamp
    dated_dir.mkdir(parents=True, exist_ok=True)

    files = {
        "garmin_daily_health.csv": f"{stamp}_garmin_daily_health.csv",
        "garmin_activities.csv": f"{stamp}_garmin_activities.csv",
        "coach_summary.md": f"{stamp}_coach_summary.md",
    }
    for source_name, dated_name in files.items():
        source = processed_dir / source_name
        if source.exists():
            shutil.copy2(source, dated_dir / dated_name)
            shutil.copy2(source, export_root / f"latest_{source_name}")

    return dated_dir


def copy_marathon_exports_to_drive(local_export_dir: Path, drive_output_dir: Optional[str]) -> Optional[Path]:
    if not drive_output_dir:
        return None

    drive_dir = Path(drive_output_dir).expanduser()
    if not drive_dir.exists():
        raise RuntimeError(
            f"MARATHON_COACH_DRIVE_DIR does not exist: {drive_dir}. "
            "Install Google Drive for desktop or update the path."
        )

    target = drive_dir / local_export_dir.name
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(local_export_dir, target)

    for latest_file in local_export_dir.parent.glob("latest_*"):
        shutil.copy2(latest_file, drive_dir / latest_file.name)

    return target


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Daily Garmin Connect activity and health data sync.")
    parser.add_argument("--email", default=os.getenv("GARMIN_EMAIL"), help="Garmin account email.")
    parser.add_argument("--password", default=os.getenv("GARMIN_PASSWORD"), help=argparse.SUPPRESS)
    parser.add_argument("--keychain-service", default=os.getenv("GARMIN_KEYCHAIN_SERVICE", DEFAULT_KEYCHAIN_SERVICE))
    parser.add_argument("--tokenstore", default=os.getenv("GARMINTOKENS", str(DEFAULT_TOKENSTORE)))
    parser.add_argument("--out-dir", default=str(PROJECT_ROOT / "data"))
    parser.add_argument(
        "--marathon-export-dir",
        default=os.getenv("MARATHON_COACH_EXPORT_DIR", str(PROJECT_ROOT / "data" / "marathon_coach")),
        help="Local dated export folder for marathon coach files.",
    )
    parser.add_argument(
        "--drive-output-dir",
        default=os.getenv("MARATHON_COACH_DRIVE_DIR"),
        help="Optional local Google Drive Desktop folder path to receive dated outputs.",
    )
    parser.add_argument("--days", type=int, default=2, help="Number of days to sync ending at --end-date.")
    parser.add_argument("--end-date", default=date.today().isoformat(), help="YYYY-MM-DD end date.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.email:
        raise RuntimeError("GARMIN_EMAIL is required.")

    password = args.password or get_keychain_password(args.email, args.keychain_service)
    end_day = date.fromisoformat(args.end_date)
    start_day = end_day - timedelta(days=max(args.days - 1, 0))

    out_dir = Path(args.out_dir)
    raw_dir = out_dir / "raw"
    processed_dir = out_dir / "processed"

    client = login(args.email, password, Path(args.tokenstore))
    activities_raw = get_recent_activities_for_dates(client, start_day, end_day)

    records = []
    for offset in range(args.days):
        day = start_day + timedelta(days=offset)
        record = collect_day(client, day)
        record["activities"] = [
            activity
            for activity in activities_raw
            if str(activity.get("startTimeLocal", "")).split("T")[0].split(" ")[0] == day.isoformat()
        ]
        records.append(record)
        raw_path = raw_dir / f"garmin_{day.isoformat()}.json"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(json.dumps(record, indent=2, default=json_default), encoding="utf-8")

    daily_rows = [daily_row(record) for record in records]
    activity_rows = [activity_row(activity) for activity in activities_raw if isinstance(activity, dict)]

    upsert_csv(processed_dir / "garmin_daily_health.csv", "date", daily_rows)
    upsert_csv(
        processed_dir / "garmin_activities.csv",
        "Activity ID",
        activity_rows,
        preferred_fieldnames=GARMIN_ACTIVITY_EXPORT_COLUMNS,
    )
    write_coach_summary(processed_dir / "coach_summary.md", daily_rows, activity_rows)
    export_dir = write_marathon_coach_exports(processed_dir, Path(args.marathon_export_dir), end_day.isoformat())
    drive_export_dir = copy_marathon_exports_to_drive(export_dir, args.drive_output_dir)

    print(f"Synced Garmin data for {start_day.isoformat()} to {end_day.isoformat()}")
    print(f"Raw JSON: {raw_dir}")
    print(f"Processed files: {processed_dir}")
    print(f"Marathon coach export: {export_dir}")
    if drive_export_dir:
        print(f"Google Drive Desktop export: {drive_export_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```
### `scripts/install_launchd_garmin_sync.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.liur.garmin-daily-sync"
EMAIL=""
RUN_TIME="06:30"
KEYCHAIN_SERVICE="garminconnect-daily-sync"
DRIVE_OUTPUT_DIR=""

usage() {
  echo "Usage: $0 --email you@example.com [--time HH:MM] [--drive-output-dir '/path/to/Google Drive/Marathon Coach']"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --email)
      EMAIL="${2:-}"
      shift 2
      ;;
    --time)
      RUN_TIME="${2:-}"
      shift 2
      ;;
    --drive-output-dir)
      DRIVE_OUTPUT_DIR="${2:-}"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1"
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$EMAIL" ]]; then
  echo "--email is required."
  usage
  exit 1
fi

if [[ ! "$RUN_TIME" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]]; then
  echo "--time must be HH:MM in 24-hour local time."
  exit 1
fi

PYTHON_BIN=""
for candidate in python3.13 python3.12 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
PY
    then
      PYTHON_BIN="$(command -v "$candidate")"
      break
    fi
  fi
done

if [[ -z "$PYTHON_BIN" ]]; then
  echo "Python 3.9+ is required."
  exit 1
fi

mkdir -p "$ROOT_DIR/logs" "$ROOT_DIR/data/raw" "$ROOT_DIR/data/processed"
if [[ -n "$DRIVE_OUTPUT_DIR" ]]; then
  mkdir -p "$DRIVE_OUTPUT_DIR"
fi

"$PYTHON_BIN" -m venv "$ROOT_DIR/.venv"
"$ROOT_DIR/.venv/bin/python" -m pip install --upgrade pip
"$ROOT_DIR/.venv/bin/python" -m pip install -r "$ROOT_DIR/requirements.txt"

if ! security find-generic-password -a "$EMAIL" -s "$KEYCHAIN_SERVICE" -w >/dev/null 2>&1; then
  read -r -s -p "Garmin password for $EMAIL: " GARMIN_PASSWORD
  echo
  security add-generic-password \
    -a "$EMAIL" \
    -s "$KEYCHAIN_SERVICE" \
    -w "$GARMIN_PASSWORD" \
    -U >/dev/null
fi

HOUR="${RUN_TIME%%:*}"
MINUTE="${RUN_TIME##*:}"
HOUR="$((10#$HOUR))"
MINUTE="$((10#$MINUTE))"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents"

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
echo "Schedule: daily at $RUN_TIME local time"
echo "Plist: $PLIST"
if [[ -n "$DRIVE_OUTPUT_DIR" ]]; then
  echo "Google Drive Desktop export folder: $DRIVE_OUTPUT_DIR"
fi
echo
echo "Run once interactively to complete MFA/token setup if needed:"
echo "  $ROOT_DIR/scripts/run_garmin_sync_once.sh"
```
### `scripts/run_garmin_sync_once.sh`
```bash
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
```
### `scripts/uninstall_launchd_garmin_sync.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

LABEL="com.liur.garmin-daily-sync"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
rm -f "$PLIST"

echo "Removed launchd job: $LABEL"
```
### `~/Library/LaunchAgents/com.liur.garmin-daily-sync.plist`
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>EnvironmentVariables</key>
	<dict>
		<key>GARMIN_EMAIL</key>
		<string>reneeliuxiaoxinxin@gmail.com</string>
		<key>GARMIN_KEYCHAIN_SERVICE</key>
		<string>garminconnect-daily-sync</string>
	</dict>
	<key>Label</key>
	<string>com.liur.garmin-daily-sync</string>
	<key>ProgramArguments</key>
	<array>
		<string>/Users/liur/Documents/Codex/2026-05-29/prior-conversation-with-codex-conversation-role/scripts/run_garmin_sync_once.sh</string>
	</array>
	<key>RunAtLoad</key>
	<false/>
	<key>StandardErrorPath</key>
	<string>/Users/liur/Documents/Codex/2026-05-29/prior-conversation-with-codex-conversation-role/logs/garmin_daily_sync.err.log</string>
	<key>StandardOutPath</key>
	<string>/Users/liur/Documents/Codex/2026-05-29/prior-conversation-with-codex-conversation-role/logs/garmin_daily_sync.out.log</string>
	<key>StartCalendarInterval</key>
	<dict>
		<key>Hour</key>
		<integer>6</integer>
		<key>Minute</key>
		<integer>30</integer>
	</dict>
	<key>WorkingDirectory</key>
	<string>/Users/liur/Documents/Codex/2026-05-29/prior-conversation-with-codex-conversation-role</string>
</dict>
</plist>
```
## Generated files to recreate, not hand-edit
- `data/marathon_coach/2026-05-29/2026-05-29_coach_summary.md`
- `data/marathon_coach/2026-05-29/2026-05-29_garmin_activities.csv`
- `data/marathon_coach/2026-05-29/2026-05-29_garmin_daily_health.csv`
- `data/marathon_coach/2026-05-30/2026-05-30_coach_summary.md`
- `data/marathon_coach/2026-05-30/2026-05-30_garmin_activities.csv`
- `data/marathon_coach/2026-05-30/2026-05-30_garmin_daily_health.csv`
- `data/marathon_coach/2026-05-31/2026-05-31_coach_summary.md`
- `data/marathon_coach/2026-05-31/2026-05-31_garmin_activities.csv`
- `data/marathon_coach/2026-05-31/2026-05-31_garmin_daily_health.csv`
- `data/marathon_coach/latest_coach_summary.md`
- `data/marathon_coach/latest_garmin_activities.csv`
- `data/marathon_coach/latest_garmin_daily_health.csv`

## Diff / patch availability
No local git diff is available because `/Users/liur/Documents/Codex/2026-05-29/prior-conversation-with-codex-conversation-role` is not a git repository. Treat the file contents above as the final desired state.

## Tests and commands run
- Confirmed the recovery folder `/Users/liur/Documents/Codex/2026-05-31/i-ran-out-of-context-and` was empty.
- Confirmed prior source folder contains the workflow files and generated Garmin outputs.
- `git status` in the prior source folder failed with `fatal: not a git repository`, so no patch can be produced from git.
- Inspected installed LaunchAgent `~/Library/LaunchAgents/com.liur.garmin-daily-sync.plist`; it points to `scripts/run_garmin_sync_once.sh`, runs at 06:30, and uses Garmin email `reneeliuxiaoxinxin@gmail.com`.
- Verified latest generated activities CSV header contains 45 columns: the 44 visible Garmin export columns plus `Activity ID`.
- Verified latest generated daily health CSV header includes HRV and training-readiness fields such as `hrv_status`, `hrv_avg`, `training_readiness_score`, and `training_readiness_feedback_short`.

## Remaining issues
- The manual runner was the validated path; scheduled launchd remained unreliable from this Documents checkout because prior runs hit exit code 126 / Operation not permitted.
- Gmail delivery was the reliable handoff path after Google Drive friction, but Gmail sending itself is connector-dependent and was not re-run in this recovery turn.
- Garmin credentials are intentionally not included. The password should remain in macOS Keychain under service `garminconnect-daily-sync`; `GARMIN_EMAIL` must be set for manual runs.
