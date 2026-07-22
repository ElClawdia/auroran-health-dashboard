#!/usr/bin/env python3
"""
Sync Oura daily health data to InfluxDB daily_health.

Writes Oura data using the dashboard's existing field names:
  sleep_duration_hours, hrv_avg, resting_hr, steps, recovery_score,
  active_calories, total_calories.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

from config import (
    INFLUXDB_BUCKET,
    INFLUXDB_ORG,
    INFLUXDB_TOKEN,
    INFLUXDB_URL,
    OURA_CLIENT_ID,
    OURA_CLIENT_SECRET,
)
from oura_client import OuraClient


SOURCE_TAG = "oura"
TOKEN_FILE = Path(__file__).resolve().parent / "oura_tokens.json"


def first_number(document: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = document.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def document_day(document: dict[str, Any]) -> str | None:
    value = document.get("day") or document.get("date")
    if isinstance(value, str) and len(value) >= 10:
        return value[:10]
    return None


def seconds_to_hours(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value / 3600.0, 3)


def merge_activity(daily: dict[str, dict[str, Any]], documents: list[dict[str, Any]]) -> None:
    for doc in documents:
        day = document_day(doc)
        if not day:
            continue
        steps = first_number(doc, ("steps",))
        active_calories = first_number(doc, ("active_calories",))
        total_calories = first_number(doc, ("total_calories",))
        activity_score = first_number(doc, ("score",))
        if steps is not None:
            daily[day]["steps"] = int(round(steps))
        if active_calories is not None:
            daily[day]["active_calories"] = int(round(active_calories))
        if total_calories is not None:
            daily[day]["total_calories"] = int(round(total_calories))
        if activity_score is not None:
            daily[day]["activity_score"] = round(activity_score, 2)


def merge_sleep(daily: dict[str, dict[str, Any]], documents: list[dict[str, Any]]) -> None:
    for doc in documents:
        day = document_day(doc)
        if not day:
            continue
        sleep_seconds = first_number(
            doc,
            (
                "total_sleep_duration",
                "sleep_duration",
                "time_in_bed",
            ),
        )
        hrv = first_number(doc, ("average_hrv", "hrv_average", "hrv_avg"))
        resting_hr = first_number(doc, ("average_heart_rate", "lowest_heart_rate"))
        sleep_score = first_number(doc, ("score",))
        sleep_hours = seconds_to_hours(sleep_seconds)
        if sleep_hours is not None:
            daily[day]["sleep_duration_hours"] = sleep_hours
        if hrv is not None:
            daily[day]["hrv_avg"] = round(hrv, 2)
        if resting_hr is not None:
            daily[day]["resting_hr"] = round(resting_hr, 2)
        if sleep_score is not None:
            daily[day]["sleep_score"] = round(sleep_score, 2)


def merge_sleep_details(daily: dict[str, dict[str, Any]], documents: list[dict[str, Any]]) -> None:
    for doc in documents:
        day = document_day(doc)
        if not day:
            continue
        fields = {
            "sleep_duration_hours": seconds_to_hours(first_number(doc, ("total_sleep_duration",))),
            "deep_sleep_hours": seconds_to_hours(first_number(doc, ("deep_sleep_duration",))),
            "rem_sleep_hours": seconds_to_hours(first_number(doc, ("rem_sleep_duration",))),
            "light_sleep_hours": seconds_to_hours(first_number(doc, ("light_sleep_duration",))),
            "awake_hours": seconds_to_hours(first_number(doc, ("awake_time",))),
            "hrv_avg": first_number(doc, ("average_hrv",)),
            "resting_hr": first_number(doc, ("lowest_heart_rate", "average_heart_rate")),
            "avg_sleep_hr": first_number(doc, ("average_heart_rate",)),
            "sleep_efficiency": first_number(doc, ("efficiency",)),
        }
        for key, value in fields.items():
            if value is None:
                continue
            daily[day][key] = round(float(value), 3 if key.endswith("_hours") else 2)


def merge_readiness(daily: dict[str, dict[str, Any]], documents: list[dict[str, Any]]) -> None:
    for doc in documents:
        day = document_day(doc)
        if not day:
            continue
        readiness_score = first_number(doc, ("score",))
        temperature_deviation = first_number(doc, ("temperature_deviation",))
        if readiness_score is not None:
            daily[day]["recovery_score"] = round(readiness_score, 2)
        if temperature_deviation is not None:
            daily[day]["temperature_deviation"] = round(temperature_deviation, 3)


def date_to_utc_noon(day: str) -> datetime:
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=12, tzinfo=timezone.utc)


def sync_oura_to_influxdb(
    days: int = 7,
    start_date: str | None = None,
    end_date: str | None = None,
    dry_run: bool = False,
) -> bool:
    if not OURA_CLIENT_ID or not OURA_CLIENT_SECRET:
        print("ERROR: Add oura_client_id and oura_client_secret to secrets.json or environment variables.")
        return False
    if not TOKEN_FILE.exists():
        print("ERROR: Create oura_tokens.json by running ./refresh_oura_tokens.py")
        return False
    if not INFLUXDB_TOKEN and not dry_run:
        print("ERROR: InfluxDB not configured")
        return False

    today = date.today()
    end = end_date or today.isoformat()
    start = start_date or (today - timedelta(days=days)).isoformat()

    client = OuraClient(
        client_id=OURA_CLIENT_ID,
        client_secret=OURA_CLIENT_SECRET,
        token_file=TOKEN_FILE,
    )
    client.load_tokens_from_file()
    if not client.is_configured:
        print("ERROR: Could not load Oura tokens from oura_tokens.json")
        return False
    if client.token_expiring() and not client.refresh_access_token():
        print("ERROR: Oura token refresh failed; re-run ./refresh_oura_tokens.py")
        return False

    daily: dict[str, dict[str, Any]] = defaultdict(dict)
    merge_activity(daily, client.get_daily_activity(start, end))
    merge_sleep(daily, client.get_daily_sleep(start, end))
    merge_sleep_details(daily, client.get_sleep(start, end))
    merge_readiness(daily, client.get_daily_readiness(start, end))

    points = []
    for day, fields in sorted(daily.items()):
        if not fields:
            continue
        point = Point("daily_health").tag("date", day).tag("source", SOURCE_TAG).time(date_to_utc_noon(day))
        for key, value in fields.items():
            if isinstance(value, int):
                point = point.field(key, value)
            elif isinstance(value, float):
                point = point.field(key, float(value))
        points.append(point)

    for day, fields in sorted(daily.items()):
        if fields:
            summary = ", ".join(f"{key}={value}" for key, value in sorted(fields.items()))
            print(f"  {day}: {summary}")

    if dry_run:
        print(f"Dry run: would write {len(points)} days to InfluxDB bucket '{INFLUXDB_BUCKET}'")
        return True

    influx = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
    write_api = influx.write_api(write_options=SYNCHRONOUS)
    try:
        if points:
            write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=points)
    finally:
        write_api.close()
        influx.close()

    print(f"Wrote {len(points)} days to InfluxDB bucket '{INFLUXDB_BUCKET}'")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Oura daily data to InfluxDB.")
    parser.add_argument("--days", type=int, default=7, help="Number of days to sync when no date range is given.")
    parser.add_argument("--start-date", help="Start date in YYYY-MM-DD format.")
    parser.add_argument("--end-date", help="End date in YYYY-MM-DD format.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and print data without writing InfluxDB.")
    args = parser.parse_args()
    sync_oura_to_influxdb(
        days=args.days,
        start_date=args.start_date,
        end_date=args.end_date,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
