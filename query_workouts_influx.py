#!/usr/bin/env python3
"""Query InfluxDB for workout data to diagnose display issues."""

import os
from datetime import datetime, timedelta

os.environ.setdefault("LOG_LEVEL", "WARNING")

from config import INFLUXDB_BUCKET, INFLUXDB_ORG, INFLUXDB_TOKEN, INFLUXDB_URL
from influxdb_client import InfluxDBClient

if not INFLUXDB_TOKEN:
    print("ERROR: InfluxDB not configured (no token)")
    exit(1)

client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
query_api = client.query_api()

# Query workouts - use long range to see what we have
for measurement in ["workout_cache", "workouts"]:
    print(f"\n=== {measurement} (last 4000 days = ~11 years) ===\n")
    q = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -4000d)
      |> filter(fn: (r) => r._measurement == "{measurement}")
      |> filter(fn: (r) => r._field == "strava_id" or r._field == "date")
      |> sort(columns: ["_time"], desc: false)
    '''
    count = 0
    dates = set()
    for table in query_api.query(q):
        for record in table.records:
            count += 1
            d = record.values.get("date", "")
            if d:
                dates.add(d)
    print(f"  Total records (strava_id/date fields): {count}")
    if dates:
        sorted_dates = sorted(dates)
        print(f"  Date range: {sorted_dates[0]} to {sorted_dates[-1]}")
        print(f"  Unique dates: {len(dates)}")

client.close()
print("\nDone.")
