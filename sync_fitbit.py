#!/usr/bin/env python3
"""
Sync Fitbit data (weight, steps, sleep, resting HR) to InfluxDB daily_health.

Setup:
1. Register app at https://dev.fitbit.com/apps/new (type: Personal)
2. Get OAuth tokens via https://dev.fitbit.com/apps/oauthinteractivetutorial
   (use your Client ID, Client Secret, Callback URL)
3. Create fitbit_tokens.json with access_token, refresh_token, expires_at
4. Add to secrets.json: fitbit_client_id, fitbit_client_secret
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

from config import (
    INFLUXDB_BUCKET,
    INFLUXDB_ORG,
    INFLUXDB_TOKEN,
    INFLUXDB_URL,
    get_secret,
)
from fitbit_client import FitbitClient


def sync_fitbit_to_influxdb(days: int = 7) -> bool:
    """Fetch Fitbit data and write to InfluxDB daily_health."""
    client_id = get_secret("fitbit_client_id", "")
    client_secret = get_secret("fitbit_client_secret", "")
    token_file = Path(__file__).parent / "fitbit_tokens.json"

    if not client_id or not client_secret:
        print("ERROR: Add fitbit_client_id and fitbit_client_secret to secrets.json")
        return False
    if not token_file.exists():
        print("ERROR: Create fitbit_tokens.json with access_token, refresh_token, expires_at")
        print("  Use https://dev.fitbit.com/apps/oauthinteractivetutorial to get tokens")
        return False
    if not INFLUXDB_TOKEN:
        print("ERROR: InfluxDB not configured")
        return False

    client = FitbitClient(
        client_id=client_id,
        client_secret=client_secret,
        token_file=token_file,
    )
    client.load_tokens_from_file()

    if not client.is_configured:
        print("ERROR: Could not load Fitbit tokens from fitbit_tokens.json")
        return False

    # Refresh token if expired (with 5 min buffer)
    with open(client.token_file) as f:
        token_data = json.load(f)
    expires_at = token_data.get("expires_at", 0)
    if expires_at < (datetime.now().timestamp() + 300):
        if not client.refresh_access_token():
            print("ERROR: Fitbit token refresh failed")
            return False
        print("Fitbit token refreshed")
    elif not client.access_token:
        client.access_token = token_data.get("access_token", "")
        client.refresh_token = token_data.get("refresh_token", "")

    today = datetime.now().date()
    end_date = today.isoformat()
    start_date = (today - timedelta(days=days)).isoformat()

    # Fetch weight (Fitbit API max 31 days per request)
    weight_by_date: dict[str, float] = {}
    chunk_start = today - timedelta(days=days)
    while chunk_start <= today:
        chunk_end = min(chunk_start + timedelta(days=30), today)
        logs = client.get_weight_range(
            chunk_start.isoformat(),
            chunk_end.isoformat(),
        )
        for entry in logs:
            dt = entry.get("date")
            if dt and start_date <= dt <= end_date:
                try:
                    weight_by_date[dt] = float(entry.get("weight", 0))
                except (ValueError, TypeError):
                    pass
        chunk_start = chunk_end + timedelta(days=1)

    # Build daily payload: weight + steps + sleep + resting_hr
    daily: dict[str, dict] = defaultdict(dict)
    for dt_str, w in weight_by_date.items():
        daily[dt_str]["weight"] = round(w, 2)

    # Fetch steps in chunks (max 1095 days per request)
    chunk_start = today - timedelta(days=days)
    while chunk_start <= today:
        chunk_end = min(chunk_start + timedelta(days=1094), today)
        for dt, steps in client.get_steps_range(
            chunk_start.isoformat(), chunk_end.isoformat()
        ).items():
            if dt not in daily:
                daily[dt] = {}
            if steps is not None:
                daily[dt]["steps"] = steps
        chunk_start = chunk_end + timedelta(days=1)

    # Fetch sleep in chunks (max 100 days per request)
    chunk_start = today - timedelta(days=days)
    while chunk_start <= today:
        chunk_end = min(chunk_start + timedelta(days=99), today)
        for dt, hours in client.get_sleep_range(
            chunk_start.isoformat(), chunk_end.isoformat()
        ).items():
            if dt not in daily:
                daily[dt] = {}
            if hours:
                daily[dt]["sleep_duration_hours"] = round(hours, 3)
        chunk_start = chunk_end + timedelta(days=1)

    # Fetch resting HR in chunks (max 365 days per request)
    chunk_start = today - timedelta(days=days)
    while chunk_start <= today:
        chunk_end = min(chunk_start + timedelta(days=364), today)
        for dt, rhr in client.get_resting_hr_range(
            chunk_start.isoformat(), chunk_end.isoformat()
        ).items():
            if dt not in daily:
                daily[dt] = {}
            if rhr is not None:
                daily[dt]["resting_hr"] = round(rhr, 2)
        chunk_start = chunk_end + timedelta(days=1)

    # Batch write to InfluxDB
    influx = InfluxDBClient(
        url=INFLUXDB_URL,
        token=INFLUXDB_TOKEN,
        org=INFLUXDB_ORG,
    )
    write_api = influx.write_api(write_options=SYNCHRONOUS)

    points = []
    for date_str, fields in sorted(daily.items()):
        if not fields:
            continue
        ts = datetime.fromisoformat(date_str).replace(
            hour=12, minute=0, second=0, tzinfo=timezone.utc
        )
        point = Point("daily_health").tag("date", date_str).time(ts)
        for key, val in fields.items():
            point = point.field(key, val)
        points.append(point)

    if points:
        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=points)

    written = len(points)
    for date_str, fields in sorted(daily.items()):
        if fields:
            summary = ", ".join(f"{k}={v}" for k, v in fields.items())
            print(f"  {date_str}: {summary}")

    write_api.close()
    influx.close()

    print(f"Wrote {written} days to InfluxDB bucket '{INFLUXDB_BUCKET}'")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync Fitbit data (weight, steps, sleep, resting HR) to InfluxDB.",
        epilog="""
Setup:
  1. Register app: https://dev.fitbit.com/apps/new (Personal)
  2. Get tokens: https://dev.fitbit.com/apps/oauthinteractivetutorial
  3. Create fitbit_tokens.json with access_token, refresh_token, expires_at
  4. Add fitbit_client_id, fitbit_client_secret to secrets.json

Examples:
  ./sync_fitbit.py
      Sync last 7 days (default).
  ./sync_fitbit.py --days 365
      Sync full year (weight, steps, sleep, resting HR).
  ./sync_fitbit.py --days 1095
      Sync ~3 years of history.
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Number of days to sync (default: 7; use 365+ for full history)",
    )
    args = parser.parse_args()

    sync_fitbit_to_influxdb(days=args.days)


if __name__ == "__main__":
    main()
