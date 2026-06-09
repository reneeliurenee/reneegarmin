#!/usr/bin/env python3
"""Daily Garmin Connect sync with freshness diagnostics.

The most common reason a morning report looks "incomplete" is that the
script asks Garmin for today's calendar date before Garmin has finalized
sleep/HRV/training-readiness/recovery metrics. This collector therefore keeps
fetching recent raw days, but marks same-day rows as in-progress and can export
only completed health days for coach handoff.
"""
from __future__ import annotations

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
from zoneinfo import ZoneInfo

from garminconnect import Garmin


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOKENSTORE = Path.home() / ".garminconnect"
DEFAULT_KEYCHAIN_SERVICE = "garminconnect-daily-sync"
DEFAULT_TIMEZONE = os.getenv("GARMIN_SYNC_TIMEZONE", "Asia/Singapore")
HEALTH_REQUIRED_FIELDS = (
    "steps",
    "resting_hr",
    "max_hr",
    "avg_stress",
    "sleep_hours",
    "hrv_avg",
    "training_readiness_score",
    "recovery_time_hours",
    "acute_training_load",
)
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
    if shutil.which("security") is None:
        raise RuntimeError(
            "GARMIN_PASSWORD is not set and the macOS Keychain 'security' command is unavailable. "
            "Run this automation on the configured macOS host, pass --password, or set GARMIN_PASSWORD "
            f"for account '{email}'."
        )

    result = subprocess.run(
        ["security", "find-generic-password", "-a", email, "-s", service, "-w"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"No Garmin password found in macOS Keychain for account '{email}' and service '{service}'. "
            "Run scripts/install_launchd_garmin_sync.sh again, pass --password, or set GARMIN_PASSWORD."
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
    try:
        garmin_signature = inspect.signature(Garmin)
        if "prompt_mfa" in garmin_signature.parameters:
            client = Garmin(email, password, prompt_mfa=prompt_mfa)
            client.login(str(tokenstore))
            return client

        client = Garmin(email, password)
        if any(tokenstore.iterdir()):
            client.login(str(tokenstore))
        else:
            client.login()
            if hasattr(client, "garth") and hasattr(client.garth, "dump"):
                client.garth.dump(str(tokenstore))
        return client
    except RuntimeError:
        raise
    except Exception as exc:
        message = str(exc)
        guidance = ""
        if "ProxyError" in message or "403 Forbidden" in message or "CONNECT tunnel failed" in message:
            guidance = (
                " This appears to be a network/proxy block while connecting to Garmin. "
                "Run the automation from a network that can reach sso.garmin.com/connect.garmin.com, "
                "or update proxy allowlisting for Garmin Connect."
            )
        raise RuntimeError(f"Garmin login failed for account '{email}': {message}.{guidance}") from exc


def safe_call(name: str, fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as exc:  # Garmin endpoints intermittently fail independently.
        return {"_error": f"{name}: {type(exc).__name__}: {exc}"}


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


def first_value(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return ""


def hrv_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or "_error" in value:
        return {}
    summary = value.get("hrvSummary")
    return summary if isinstance(summary, dict) else value


def latest_training_readiness(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and "_error" not in value:
        return value
    if not isinstance(value, list):
        return {}
    candidates = [item for item in value if isinstance(item, dict)]
    if not candidates:
        return {}
    return sorted(
        candidates,
        key=lambda item: str(item.get("timestampLocal") or item.get("timestamp") or item.get("calendarDate") or ""),
    )[-1]


def seconds_to_hours(value: Any) -> str:
    try:
        return f"{float(value) / 3600:.2f}"
    except (TypeError, ValueError):
        return ""


def hours_from_seconds(value: Any) -> str:
    return seconds_to_hours(value)


def format_number(value: Any, decimals: Optional[int] = None, missing: str = "") -> str:
    if value is None or value == "":
        return missing
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if decimals is None:
        return str(int(number)) if number.is_integer() else str(number)
    return f"{number:.{decimals}f}"


def format_int_with_commas(value: Any) -> str:
    try:
        return f"{int(round(float(value))):,}"
    except (TypeError, ValueError):
        return ""


def format_duration(seconds: Any, tenths: bool = False) -> str:
    if seconds is None or seconds == "":
        return ""
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
    try:
        speed = float(speed_mps)
    except (TypeError, ValueError):
        return ""
    if speed <= 0:
        return ""
    seconds_per_km = 1000 / speed
    minutes = int(seconds_per_km // 60)
    seconds = int(round(seconds_per_km % 60))
    if seconds == 60:
        minutes += 1
        seconds = 0
    return f"{minutes}:{seconds:02d}"


def format_distance_km(distance_m: Any) -> str:
    try:
        return f"{float(distance_m) / 1000:.2f}"
    except (TypeError, ValueError):
        return ""


def format_activity_type(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("typeKey") or value.get("typeId") or ""
    if not value:
        return ""
    return " ".join(part.capitalize() for part in str(value).replace("_", " ").split())


def format_gct_balance(value: Any) -> str:
    try:
        left = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{left:.1f}% L / {100 - left:.1f}% R"


def training_status_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or "_error" in value:
        return {}
    return first_dict(value.get("mostRecentTrainingStatus"), value.get("trainingStatus"), value)


def collect_day(client: Garmin, day: date) -> dict[str, Any]:
    day_s = day.isoformat()
    week_start = (day - timedelta(days=7)).isoformat()
    return {
        "date": day_s,
        "collected_at": datetime.now().isoformat(timespec="seconds"),
        "daily": {
            "stats": safe_call("get_stats", lambda: client.get_stats(day_s)),
            "user_summary": safe_call("get_user_summary", lambda: client.get_user_summary(day_s)),
            "stats_and_body": safe_call("get_stats_and_body", lambda: client.get_stats_and_body(day_s)),
            "steps": safe_call("get_steps_data", lambda: client.get_steps_data(day_s)),
            "heart_rates": safe_call("get_heart_rates", lambda: client.get_heart_rates(day_s)),
            "resting_heart_rate": safe_call("get_resting_heart_rate", lambda: client.get_resting_heart_rate(day_s)),
            "sleep": safe_call("get_sleep_data", lambda: client.get_sleep_data(day_s)),
            "stress": safe_call("get_all_day_stress", lambda: client.get_all_day_stress(day_s)),
            "hrv": safe_call("get_hrv_data", lambda: client.get_hrv_data(day_s)),
            "training_readiness": safe_call("get_training_readiness", lambda: client.get_training_readiness(day_s)),
            "training_status": safe_call("get_training_status", lambda: client.get_training_status(day_s)),
            "body_battery": safe_call("get_body_battery", lambda: client.get_body_battery(week_start, day_s)),
            "intensity_minutes": safe_call("get_intensity_minutes_data", lambda: client.get_intensity_minutes_data(day_s)),
            "body_composition": safe_call("get_body_composition", lambda: client.get_body_composition(day_s)),
        },
    }


def daily_row(record: dict[str, Any], completed_through: date) -> dict[str, Any]:
    daily = record.get("daily", {})
    stats = first_dict(daily.get("stats"))
    summary = first_dict(daily.get("user_summary"), stats, daily.get("stats_and_body"))
    stats_body = first_dict(daily.get("stats_and_body"))
    hr = first_dict(daily.get("heart_rates"))
    resting = first_dict(daily.get("resting_heart_rate"))
    sleep = first_dict(daily.get("sleep"))
    sleep_dto = nested_get(sleep, "dailySleepDTO", default={})
    stress = first_dict(daily.get("stress"))
    hrv = hrv_summary(daily.get("hrv"))
    hrv_baseline = hrv.get("baseline") if isinstance(hrv.get("baseline"), dict) else {}
    readiness = latest_training_readiness(daily.get("training_readiness"))
    training_status = training_status_payload(daily.get("training_status"))
    row_date = date.fromisoformat(record["date"])

    row = {
        "date": record["date"],
        "data_status": "complete_window" if row_date <= completed_through else "in_progress_today",
        "steps": first_value(summary.get("totalSteps"), stats_body.get("totalSteps")),
        "total_distance_m": first_value(summary.get("totalDistanceMeters"), stats_body.get("totalDistanceMeters")),
        "active_kcal": first_value(summary.get("activeKilocalories"), stats_body.get("activeKilocalories")),
        "total_kcal": first_value(summary.get("totalKilocalories"), stats_body.get("totalKilocalories")),
        "resting_hr": first_value(hr.get("restingHeartRate"), resting.get("value"), summary.get("restingHeartRate")),
        "max_hr": first_value(hr.get("maxHeartRate"), summary.get("maxHeartRate")),
        "avg_stress": first_value(stress.get("avgStressLevel"), summary.get("averageStressLevel")),
        "sleep_hours": hours_from_seconds(nested_get(sleep_dto, "sleepTimeSeconds")),
        "deep_sleep_hours": hours_from_seconds(nested_get(sleep_dto, "deepSleepSeconds")),
        "hrv_status": hrv.get("status", ""),
        "hrv_avg": first_value(hrv.get("lastNightAvg"), hrv.get("weeklyAvg")),
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
        "recovery_time_hours": hours_from_seconds(first_value(readiness.get("recoveryTime"), training_status.get("recoveryTime"))),
        "acute_training_load": first_value(training_status.get("acuteTrainingLoad"), training_status.get("load")),
        "training_status": first_value(training_status.get("trainingStatus"), training_status.get("trainingStatusKey")),
    }
    missing_fields = [field for field in HEALTH_REQUIRED_FIELDS if row.get(field) in (None, "")]
    row["missing_core_fields"] = ",".join(missing_fields)
    row["core_complete"] = str(not missing_fields).lower()
    if row["data_status"] == "in_progress_today" and missing_fields:
        row["diagnostic_note"] = "same-day Garmin data can be incomplete before post-sleep/cloud processing finishes"
    elif missing_fields:
        row["diagnostic_note"] = "Garmin endpoint returned no value; inspect matching data/raw JSON for endpoint errors"
    else:
        row["diagnostic_note"] = ""
    return row


def get_recent_activities_for_dates(client: Garmin, start: date, end: date, lookback_days: int = 14) -> list[dict[str, Any]]:
    query_start = min(start, end - timedelta(days=lookback_days))
    if hasattr(client, "get_activities_by_date"):
        data = safe_call("get_activities_by_date", lambda: client.get_activities_by_date(query_start.isoformat(), end.isoformat()))
        if isinstance(data, list):
            return filter_activities_by_date(data, start, end)

    activities: list[dict[str, Any]] = []
    seen: set[str] = set()
    page_size = 100
    for offset in range(0, 500, page_size):
        data = safe_call("get_activities", lambda offset=offset: client.get_activities(offset, page_size))
        if not isinstance(data, list) or not data:
            break
        for activity in filter_activities_by_date(data, start, end):
            key = str(activity.get("activityId") or activity.get("activityUuid") or json.dumps(activity, sort_keys=True, default=json_default))
            if key not in seen:
                seen.add(key)
                activities.append(activity)
        oldest = min((activity_date(item) for item in data if activity_date(item)), default=end)
        if oldest < query_start:
            break
    return activities


def activity_date(activity: dict[str, Any]) -> Optional[date]:
    started = str(activity.get("startTimeLocal") or activity.get("startTimeGMT") or activity.get("beginTimestamp") or "")
    if not started:
        return None
    try:
        return date.fromisoformat(started.split("T")[0].split(" ")[0])
    except ValueError:
        return None


def filter_activities_by_date(data: list[Any], start: date, end: date) -> list[dict[str, Any]]:
    activities = []
    for activity in data:
        if not isinstance(activity, dict):
            continue
        day = activity_date(activity)
        if day and start <= day <= end:
            activities.append(activity)
    return activities


def activity_row(activity: dict[str, Any]) -> dict[str, Any]:
    return {
        "Activity ID": activity.get("activityId", ""),
        "Activity Type": format_activity_type(activity.get("activityType")),
        "Date": activity.get("startTimeLocal", ""),
        "Favorite": str(bool(activity.get("favorite"))).lower(),
        "Title": activity.get("activityName", ""),
        "Distance": format_distance_km(activity.get("distance")),
        "Calories": format_number(activity.get("calories"), 0),
        "Time": format_duration(activity.get("duration")),
        "Avg HR": format_number(activity.get("averageHR"), 0),
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
        "Avg Step Speed Loss": format_number(activity.get("avgStepSpeedLoss"), 2),
        "Avg Step Speed Loss %": format_number(activity.get("avgStepSpeedLossPercentage"), 1),
        "Avg Ground Contact Time": format_number(activity.get("avgGroundContactTime"), 0),
        "Avg GCT Balance": format_gct_balance(activity.get("avgGroundContactBalance")),
        "Avg GAP": format_pace_from_speed(activity.get("avgGradeAdjustedSpeed")),
        "Normalized Power® (NP®)": format_number(activity.get("normPower"), 0),
        "Training Stress Score®": format_number(activity.get("trainingStressScore"), 0),
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


def upsert_csv(path: Path, key: str, rows: list[dict[str, Any]], preferred_fieldnames: Optional[list[str]] = None) -> None:
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
            "- {date} [{data_status}]: steps={steps}, resting_hr={resting_hr}, sleep_hours={sleep_hours}, "
            "avg_stress={avg_stress}, hrv_avg={hrv_avg}, readiness={training_readiness_score}, "
            "missing={missing_core_fields}".format(**row)
        )
    lines.extend(["", "## Activities"])
    for activity in sorted(activities, key=lambda item: item.get("Date", "")):
        lines.append(
            "- {Date} | {Activity Type} | {Title} | Time={Time} | Distance={Distance} | Avg HR={Avg HR}".format(
                **activity
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_rows_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_marathon_coach_exports(
    processed_dir: Path,
    export_root: Path,
    stamp: str,
    daily_rows_for_export: Optional[list[dict[str, Any]]] = None,
) -> Path:
    dated_dir = export_root / stamp
    dated_dir.mkdir(parents=True, exist_ok=True)

    if daily_rows_for_export is not None:
        dated_daily = dated_dir / f"{stamp}_garmin_daily_health.csv"
        latest_daily = export_root / "latest_garmin_daily_health.csv"
        write_rows_csv(dated_daily, daily_rows_for_export)
        write_rows_csv(latest_daily, daily_rows_for_export)

    for source_name, dated_name in {
        "garmin_activities.csv": f"{stamp}_garmin_activities.csv",
        "coach_summary.md": f"{stamp}_coach_summary.md",
    }.items():
        source = processed_dir / source_name
        if source.exists():
            shutil.copy2(source, dated_dir / dated_name)
            shutil.copy2(source, export_root / f"latest_{source_name}")

    if daily_rows_for_export is None:
        source = processed_dir / "garmin_daily_health.csv"
        if source.exists():
            shutil.copy2(source, dated_dir / f"{stamp}_garmin_daily_health.csv")
            shutil.copy2(source, export_root / "latest_garmin_daily_health.csv")

    return dated_dir


def copy_marathon_exports_to_drive(local_export_dir: Path, drive_output_dir: Optional[str]) -> Optional[Path]:
    if not drive_output_dir:
        return None
    drive_dir = Path(drive_output_dir).expanduser()
    if not drive_dir.exists():
        raise RuntimeError(
            f"MARATHON_COACH_DRIVE_DIR does not exist: {drive_dir}. Install Google Drive for desktop or update the path."
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
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE, help="IANA timezone for deciding what 'today' means.")
    parser.add_argument("--days", type=int, default=4, help="Number of recent days to sync ending at --end-date.")
    parser.add_argument("--activity-lookback-days", type=int, default=14, help="Extra activity lookback to catch late uploads.")
    parser.add_argument("--end-date", help="YYYY-MM-DD end date. Defaults to today in --timezone.")
    parser.add_argument(
        "--completed-health-lag-days",
        type=int,
        default=1,
        help="Treat only dates at least this many days before --end-date as complete for coach diagnostics.",
    )
    parser.add_argument(
        "--coach-completed-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Exclude in-progress same-day health rows from coach_summary.md and dated coach exports.",
    )
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.email:
        raise RuntimeError("GARMIN_EMAIL or --email is required.")
    password = args.password or get_keychain_password(args.email, args.keychain_service)
    tz = ZoneInfo(args.timezone)
    end = date.fromisoformat(args.end_date) if args.end_date else datetime.now(tz).date()
    start = end - timedelta(days=max(args.days, 1) - 1)
    completed_through = end - timedelta(days=max(args.completed_health_lag_days, 0))

    client = login(args.email, password, Path(args.tokenstore))
    out_dir = Path(args.out_dir)
    raw_dir = out_dir / "raw"
    processed_dir = out_dir / "processed"
    records = [collect_day(client, start + timedelta(days=offset)) for offset in range((end - start).days + 1)]
    raw_dir.mkdir(parents=True, exist_ok=True)
    for record in records:
        (raw_dir / f"garmin_{record['date']}.json").write_text(
            json.dumps(record, indent=2, default=json_default, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    daily_rows = [daily_row(record, completed_through) for record in records]
    upsert_csv(processed_dir / "garmin_daily_health.csv", "date", daily_rows)

    raw_activities = get_recent_activities_for_dates(client, start, end, args.activity_lookback_days)
    (raw_dir / f"garmin_activities_{start.isoformat()}_{end.isoformat()}.json").write_text(
        json.dumps(raw_activities, indent=2, default=json_default, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    activity_rows = [activity_row(activity) for activity in raw_activities]
    upsert_csv(processed_dir / "garmin_activities.csv", "Activity ID", activity_rows, GARMIN_ACTIVITY_EXPORT_COLUMNS)

    coach_daily_rows = [row for row in daily_rows if not args.coach_completed_only or row["data_status"] == "complete_window"]
    write_coach_summary(processed_dir / "coach_summary.md", coach_daily_rows, activity_rows)
    export_dir = write_marathon_coach_exports(
        processed_dir, Path(args.marathon_export_dir), end.isoformat(), coach_daily_rows
    )
    drive_dir = copy_marathon_exports_to_drive(export_dir, args.drive_output_dir)

    print(f"Synced Garmin data for {start.isoformat()}..{end.isoformat()} ({args.timezone}).")
    print(f"Completed health through: {completed_through.isoformat()}.")
    print(f"Raw data: {raw_dir}")
    print(f"Processed data: {processed_dir}")
    print(f"Coach export: {export_dir}")
    if drive_dir:
        print(f"Drive export: {drive_dir}")
    incomplete = [row for row in daily_rows if row["missing_core_fields"]]
    if incomplete:
        print("Rows with missing core fields:")
        for row in incomplete:
            print(f"  {row['date']} ({row['data_status']}): {row['missing_core_fields']} - {row['diagnostic_note']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
