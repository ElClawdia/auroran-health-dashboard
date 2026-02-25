#!/usr/bin/env python3
"""
Import Suunto export files to InfluxDB without Suunto API access.

Supported inputs (best-effort autodetect):
- CSV / JSON files with daily metrics or workout rows
- GPX / TCX workout files
- FIT workout files (only if optional `fitparse` package is installed)
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

from config import INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, INFLUXDB_BUCKET


SOURCE_TAG = "suunto_export"


@dataclass
class ParseSummary:
    files_seen: int = 0
    files_parsed: int = 0
    files_skipped: int = 0
    workouts_found: int = 0
    daily_found: int = 0
    workouts_written: int = 0
    daily_written: int = 0


def parse_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if s == "":
        return None
    s = s.replace(",", ".")
    # Keep only number-ish chars if the field has units.
    s = re.sub(r"[^0-9.\-]", "", s)
    if s in ("", "-", ".", "-."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_int(val: Any) -> Optional[int]:
    f = parse_float(val)
    if f is None:
        return None
    return int(round(f))


def parse_dt(val: Any) -> Optional[datetime]:
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    # Normalize Zulu.
    s = s.replace("Z", "+00:00")
    known_formats = (
        "%Y-%m-%d %H:%M:%S %z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    )
    for fmt in known_formats:
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def date_to_utc_midnight(date_str: str) -> datetime:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return d.replace(tzinfo=timezone.utc)


def pick(row: dict[str, Any], keys: Iterable[str]) -> Any:
    lowered = {k.lower(): v for k, v in row.items()}
    for k in keys:
        if k.lower() in lowered:
            return lowered[k.lower()]
    return None


def normalize_daily(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    raw_date = pick(row, ("date", "day", "local_date"))
    dt = parse_dt(raw_date)
    if not dt:
        return None
    date = dt.date().isoformat()

    steps = parse_int(pick(row, ("steps", "step_count")))
    sleep_hours = parse_float(pick(row, ("sleep_hours", "sleep_duration_hours", "sleep")))
    # Handle sleep as seconds/minutes if explicit unit-ish columns exist.
    sleep_seconds = parse_float(pick(row, ("sleep_seconds", "sleep_duration_sec")))
    sleep_minutes = parse_float(pick(row, ("sleep_minutes", "sleep_duration_min")))
    if sleep_seconds is not None:
        sleep_hours = sleep_seconds / 3600.0
    elif sleep_minutes is not None:
        sleep_hours = sleep_minutes / 60.0

    hrv = parse_float(pick(row, ("hrv", "hrv_avg", "hrv_average")))
    resting_hr = parse_float(pick(row, ("resting_hr", "resting_heart_rate", "rhr")))
    recovery = parse_float(pick(row, ("recovery_score", "readiness", "recovery")))

    if all(v is None for v in (steps, sleep_hours, hrv, resting_hr, recovery)):
        return None

    return {
        "date": date,
        "steps": steps,
        "sleep_duration_hours": sleep_hours,
        "hrv_avg": hrv,
        "resting_hr": resting_hr,
        "recovery_score": recovery,
    }


def normalize_workout(row: dict[str, Any], fallback_name: str = "") -> Optional[dict[str, Any]]:
    raw_start = pick(row, ("start_time", "start", "startdate", "start_date", "datetime", "time"))
    start_dt = parse_dt(raw_start)
    date = start_dt.date().isoformat() if start_dt else None
    if not date:
        raw_date = pick(row, ("date", "day"))
        d2 = parse_dt(raw_date)
        date = d2.date().isoformat() if d2 else None
    if not date:
        return None

    workout_type = str(pick(row, ("type", "sport", "activity_type", "activity")) or "Workout")
    name = str(pick(row, ("name", "title")) or fallback_name or workout_type)

    duration_minutes = parse_float(
        pick(row, ("duration_minutes", "duration_min", "duration", "moving_time_minutes"))
    )
    duration_seconds = parse_float(pick(row, ("duration_seconds", "moving_time_seconds", "elapsed_time")))
    if duration_minutes is None and duration_seconds is not None:
        duration_minutes = duration_seconds / 60.0

    distance = parse_float(pick(row, ("distance", "distance_m", "distance_meters")))
    distance_km = parse_float(pick(row, ("distance_km",)))
    if distance is None and distance_km is not None:
        distance = distance_km * 1000.0

    avg_hr = parse_float(pick(row, ("avg_hr", "average_hr", "heart_rate_avg")))
    max_hr = parse_float(pick(row, ("max_hr", "maximum_hr", "heart_rate_max")))
    elevation_gain = parse_float(pick(row, ("elevation_gain", "ascent", "total_ascent")))
    calories = parse_int(pick(row, ("calories", "kcal")))
    suffer_score = parse_float(pick(row, ("suffer_score", "relative_effort", "effort")))

    if duration_minutes is None and distance is None and avg_hr is None and max_hr is None:
        return None

    uid = pick(row, ("id", "activity_id", "workout_id", "uuid"))
    if not uid:
        uid = f"{SOURCE_TAG}:{date}:{name}:{int(duration_minutes or 0)}"

    return {
        "date": date,
        "start_time": start_dt.isoformat() if start_dt else "",
        "type": workout_type,
        "name": name,
        "duration": duration_minutes or 0.0,
        "distance": distance or 0.0,
        "elevation_gain": elevation_gain or 0.0,
        "avg_hr": avg_hr or 0.0,
        "max_hr": max_hr or 0.0,
        "calories": calories if calories is not None else 0,
        "suffer_score": suffer_score if suffer_score is not None else 0.0,
        "source_id": str(uid),
    }


def parse_csv(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    workouts: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row = {k.strip(): v for k, v in row.items() if k}
            d = normalize_daily(row)
            if d:
                daily.append(d)
            w = normalize_workout(row, fallback_name=path.stem)
            if w:
                workouts.append(w)
    return workouts, daily


def extract_json_records(obj: Any) -> list[dict[str, Any]]:
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        for key in ("data", "records", "activities", "workouts", "dailies", "items", "results"):
            val = obj.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
        return [obj]
    return []


def parse_json(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    workouts: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        obj = json.load(f)
    records = extract_json_records(obj)
    for row in records:
        d = normalize_daily(row)
        if d:
            daily.append(d)
        w = normalize_workout(row, fallback_name=path.stem)
        if w:
            workouts.append(w)
    return workouts, daily


def parse_tcx(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    workouts: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []
    ns = {
        "tcx": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2",
        "ns3": "http://www.garmin.com/xmlschemas/ActivityExtension/v2",
    }
    root = ET.parse(path).getroot()
    for act in root.findall(".//tcx:Activity", ns):
        sport = act.attrib.get("Sport", "Workout")
        activity_id = act.findtext("tcx:Id", default="", namespaces=ns)
        start_dt = parse_dt(activity_id)
        duration_s = 0.0
        distance_m = 0.0
        calories = 0
        max_hr = None
        avg_hr = None
        for lap in act.findall("tcx:Lap", ns):
            duration_s += float(lap.findtext("tcx:TotalTimeSeconds", default="0", namespaces=ns) or 0)
            distance_m += float(lap.findtext("tcx:DistanceMeters", default="0", namespaces=ns) or 0)
            calories += int(float(lap.findtext("tcx:Calories", default="0", namespaces=ns) or 0))
            lap_avg = parse_float(lap.findtext("tcx:AverageHeartRateBpm/tcx:Value", default="", namespaces=ns))
            lap_max = parse_float(lap.findtext("tcx:MaximumHeartRateBpm/tcx:Value", default="", namespaces=ns))
            if lap_avg is not None:
                avg_hr = lap_avg if avg_hr is None else (avg_hr + lap_avg) / 2.0
            if lap_max is not None:
                max_hr = lap_max if max_hr is None else max(max_hr, lap_max)

        if not start_dt:
            continue
        date = start_dt.date().isoformat()
        workouts.append(
            {
                "date": date,
                "start_time": start_dt.isoformat(),
                "type": sport,
                "name": f"{sport} ({path.stem})",
                "duration": duration_s / 60.0,
                "distance": distance_m,
                "elevation_gain": 0.0,
                "avg_hr": avg_hr or 0.0,
                "max_hr": max_hr or 0.0,
                "calories": calories,
                "suffer_score": 0.0,
                "source_id": f"{SOURCE_TAG}:tcx:{path.stem}:{activity_id or date}",
            }
        )
    return workouts, daily


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def parse_gpx(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    workouts: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []
    root = ET.parse(path).getroot()
    # GPX default namespace handling.
    ns_match = re.match(r"\{(.+)\}", root.tag)
    ns_uri = ns_match.group(1) if ns_match else ""
    ns = {"gpx": ns_uri} if ns_uri else {}
    q = "gpx:" if ns_uri else ""

    trk = root.find(f"{q}trk", ns)
    if trk is None:
        return workouts, daily
    name = trk.findtext(f"{q}name", default=path.stem, namespaces=ns)
    pts = root.findall(f".//{q}trkpt", ns)
    if len(pts) < 2:
        return workouts, daily

    times: list[datetime] = []
    coords: list[tuple[float, float]] = []
    for p in pts:
        lat = parse_float(p.attrib.get("lat"))
        lon = parse_float(p.attrib.get("lon"))
        if lat is not None and lon is not None:
            coords.append((lat, lon))
        t = parse_dt(p.findtext(f"{q}time", default="", namespaces=ns))
        if t:
            times.append(t)

    if not times:
        return workouts, daily
    start_dt = min(times)
    end_dt = max(times)
    duration_min = max(0.0, (end_dt - start_dt).total_seconds() / 60.0)
    distance = 0.0
    for i in range(1, len(coords)):
        distance += haversine_m(coords[i - 1][0], coords[i - 1][1], coords[i][0], coords[i][1])

    date = start_dt.date().isoformat()
    workouts.append(
        {
            "date": date,
            "start_time": start_dt.isoformat(),
            "type": "Workout",
            "name": name,
            "duration": duration_min,
            "distance": distance,
            "elevation_gain": 0.0,
            "avg_hr": 0.0,
            "max_hr": 0.0,
            "calories": 0,
            "suffer_score": 0.0,
            "source_id": f"{SOURCE_TAG}:gpx:{path.stem}:{date}",
        }
    )
    return workouts, daily


def parse_fit(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    workouts: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []
    try:
        from fitparse import FitFile  # type: ignore
    except Exception:
        # Optional dependency missing; just skip FIT parsing.
        return workouts, daily

    fit = FitFile(str(path))
    for rec in fit.get_messages("session"):
        values = {f.name: f.value for f in rec}
        start_dt = parse_dt(values.get("start_time"))
        if not start_dt:
            continue
        date = start_dt.date().isoformat()
        workouts.append(
            {
                "date": date,
                "start_time": start_dt.isoformat(),
                "type": str(values.get("sport") or "Workout"),
                "name": f"{values.get('sport', 'Workout')} ({path.stem})",
                "duration": (parse_float(values.get("total_timer_time")) or 0.0) / 60.0,
                "distance": parse_float(values.get("total_distance")) or 0.0,
                "elevation_gain": parse_float(values.get("total_ascent")) or 0.0,
                "avg_hr": parse_float(values.get("avg_heart_rate")) or 0.0,
                "max_hr": parse_float(values.get("max_heart_rate")) or 0.0,
                "calories": parse_int(values.get("total_calories")) or 0,
                "suffer_score": 0.0,
                "source_id": f"{SOURCE_TAG}:fit:{path.stem}:{date}",
            }
        )
    return workouts, daily


def parse_file(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return parse_csv(path)
    if suffix == ".json":
        return parse_json(path)
    if suffix == ".tcx":
        return parse_tcx(path)
    if suffix == ".gpx":
        return parse_gpx(path)
    if suffix == ".fit":
        return parse_fit(path)
    return [], []


def write_to_influx(
    workouts: list[dict[str, Any]],
    daily: list[dict[str, Any]],
    dry_run: bool = False,
) -> tuple[int, int]:
    if dry_run:
        return len(workouts), len(daily)
    if not INFLUXDB_TOKEN:
        raise RuntimeError("No InfluxDB token configured")

    client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
    write_api = client.write_api(write_options=SYNCHRONOUS)
    workouts_written = 0
    daily_written = 0
    try:
        for w in workouts:
            time_dt = parse_dt(w.get("start_time")) or date_to_utc_midnight(w["date"])
            point = (
                Point("workouts")
                .tag("type", str(w.get("type", "Workout")))
                .tag("date", w["date"])
                .tag("source", SOURCE_TAG)
                .field("strava_id", str(w.get("source_id", "")))
                .field("name", str(w.get("name", "Suunto Export")))
                .field("start_time", str(w.get("start_time", "")))
                .field("duration", float(w.get("duration", 0.0)))
                .field("distance", float(w.get("distance", 0.0)))
                .field("elevation_gain", float(w.get("elevation_gain", 0.0)))
                .field("avg_hr", float(w.get("avg_hr", 0.0)))
                .field("max_hr", float(w.get("max_hr", 0.0)))
                .field("suffer_score", float(w.get("suffer_score", 0.0)))
                .field("calories", int(w.get("calories", 0)))
                .time(time_dt)
            )
            write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
            workouts_written += 1

        for d in daily:
            point = Point("daily_health").tag("date", d["date"]).tag("source", SOURCE_TAG).time(
                date_to_utc_midnight(d["date"])
            )
            if d.get("sleep_duration_hours") is not None:
                point = point.field("sleep_duration_hours", float(d["sleep_duration_hours"]))
            if d.get("hrv_avg") is not None:
                point = point.field("hrv_avg", float(d["hrv_avg"]))
            if d.get("resting_hr") is not None:
                point = point.field("resting_hr", float(d["resting_hr"]))
            if d.get("steps") is not None:
                point = point.field("steps", int(d["steps"]))
            if d.get("recovery_score") is not None:
                point = point.field("recovery_score", float(d["recovery_score"]))
            write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
            daily_written += 1
    finally:
        write_api.close()
        client.close()

    return workouts_written, daily_written


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import exported Suunto data (CSV/JSON/GPX/TCX/FIT) into InfluxDB.",
        epilog="""
Examples:
  python3 sync_suunto.py
      Import from suunto_export/ directory.
  python3 sync_suunto.py --input-dir /path/to/exports
      Use custom input directory.
  python3 sync_suunto.py --dry-run
      Parse files and show summary without writing.
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--input-dir",
        default="suunto_export",
        help="Directory containing exported Suunto files (csv/json/gpx/tcx/fit).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse files and print summary without writing to InfluxDB.",
    )
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    if not input_dir.exists():
        print(f"Input directory not found: {input_dir}")
        print("Create it and drop your exported files there, then re-run.")
        sys.exit(1)

    files = [p for p in input_dir.rglob("*") if p.is_file()]
    summary = ParseSummary(files_seen=len(files))
    all_workouts: list[dict[str, Any]] = []
    all_daily: list[dict[str, Any]] = []

    for path in files:
        workouts, daily = parse_file(path)
        if workouts or daily:
            summary.files_parsed += 1
            summary.workouts_found += len(workouts)
            summary.daily_found += len(daily)
            all_workouts.extend(workouts)
            all_daily.extend(daily)
        else:
            summary.files_skipped += 1

    # Deduplicate by stable keys.
    uniq_workouts = {w["source_id"]: w for w in all_workouts}
    uniq_daily = {d["date"]: d for d in all_daily}  # keep latest row per date

    workouts_written, daily_written = write_to_influx(
        list(uniq_workouts.values()),
        list(uniq_daily.values()),
        dry_run=args.dry_run,
    )
    summary.workouts_written = workouts_written
    summary.daily_written = daily_written

    mode = "DRY RUN" if args.dry_run else "WRITE"
    print(f"[{mode}] Suunto import summary")
    print(f"  files seen:      {summary.files_seen}")
    print(f"  files parsed:    {summary.files_parsed}")
    print(f"  files skipped:   {summary.files_skipped}")
    print(f"  workouts found:  {summary.workouts_found}")
    print(f"  daily found:     {summary.daily_found}")
    print(f"  workouts written:{summary.workouts_written}")
    print(f"  daily written:   {summary.daily_written}")


if __name__ == "__main__":
    main()
