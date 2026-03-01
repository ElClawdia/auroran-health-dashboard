#!/usr/bin/env python3
"""
Rebuild workout points with event-time _time instead of sync-time _time.

Default behavior is safe:
- reads from `workout_cache`
- writes to `workout_cache_rebuilt`
- does not modify existing measurements

Use `--execute` to write the rebuilt points.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

from config import INFLUXDB_BUCKET, INFLUXDB_ORG, INFLUXDB_TOKEN, INFLUXDB_URL


def build_activity_timestamp(activity_date: str, activity_time: str | None) -> datetime:
    if activity_date and activity_time:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(f"{activity_date} {activity_time}", fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    if activity_date:
        return datetime.strptime(activity_date, "%Y-%m-%d").replace(hour=12, tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def dedupe_key(workout: dict) -> str:
    strava_id = workout.get("strava_id")
    if strava_id:
        return f"strava:{strava_id}"
    return "|".join(
        [
            str(workout.get("date", "")),
            str(workout.get("start_time", "")),
            str(workout.get("name", "")),
            str(workout.get("type", "")),
        ]
    )


def load_workouts(query_api, bucket: str, measurement: str, range_days: int) -> list[dict]:
    query = f'''
    from(bucket: "{bucket}")
      |> range(start: -{range_days}d)
      |> filter(fn: (r) => r._measurement == "{measurement}")
    '''

    rows: dict[str, dict] = defaultdict(dict)
    for record in query_api.query_stream(query):
        key = str(record.get_time())
        row = rows[key]
        row[record.get_field()] = record.get_value()
        row["date"] = record.values.get("date", "")
        row["type"] = record.values.get("type", "")

    deduped: dict[str, dict] = {}
    for row in rows.values():
        key = dedupe_key(row)
        existing = deduped.get(key)
        if existing is None or len(row) >= len(existing):
            deduped[key] = row

    return sorted(
        deduped.values(),
        key=lambda x: (x.get("date", ""), x.get("start_time", "")),
    )


def build_point(measurement: str, workout: dict) -> Point:
    point = Point(measurement)
    if workout.get("type"):
        point = point.tag("type", str(workout["type"]))
    if workout.get("date"):
        point = point.tag("date", str(workout["date"]))

    for field, value in workout.items():
        if field in {"type", "date"}:
            continue
        if value is None:
            continue
        point = point.field(field, value)

    point = point.time(build_activity_timestamp(workout.get("date", ""), workout.get("start_time")))
    return point


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild workout_cache with correct event timestamps.")
    parser.add_argument("--source-measurement", default="workout_cache")
    parser.add_argument("--output-measurement", default="workout_cache_rebuilt")
    parser.add_argument("--range-days", type=int, default=5000)
    parser.add_argument("--url", default=INFLUXDB_URL)
    parser.add_argument("--org", default=INFLUXDB_ORG)
    parser.add_argument("--bucket", default=INFLUXDB_BUCKET)
    parser.add_argument("--token", default=INFLUXDB_TOKEN)
    parser.add_argument("--execute", action="store_true", help="Actually write rebuilt points.")
    args = parser.parse_args()

    if not args.token:
        raise RuntimeError("Missing InfluxDB token")

    client = InfluxDBClient(url=args.url, token=args.token, org=args.org, timeout=180_000)
    query_api = client.query_api()
    write_api = client.write_api(write_options=SYNCHRONOUS)

    try:
        workouts = load_workouts(query_api, args.bucket, args.source_measurement, args.range_days)
        if not workouts:
            print("No workouts found.")
            return

        points = [build_point(args.output_measurement, workout) for workout in workouts]
        print(f"Loaded {len(workouts)} workouts from {args.source_measurement}")
        print(f"Prepared {len(points)} points for {args.output_measurement}")
        print(f"Date range: {workouts[0].get('date')} -> {workouts[-1].get('date')}")

        sample = workouts[min(2, len(workouts) - 1)]
        sample_ts = build_activity_timestamp(sample.get("date", ""), sample.get("start_time"))
        print(f"Sample: {sample.get('date')} {sample.get('start_time')} -> {sample_ts.isoformat()}")

        if not args.execute:
            print("Dry run only. Re-run with --execute to write rebuilt points.")
            return

        write_api.write(bucket=args.bucket, org=args.org, record=points)
        print(f"Wrote {len(points)} points to measurement '{args.output_measurement}'")
    finally:
        write_api.close()
        client.close()


if __name__ == "__main__":
    main()
