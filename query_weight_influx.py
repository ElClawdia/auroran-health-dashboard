#!/usr/bin/env python3
"""Query InfluxDB for weight data (manual_values + daily_health) to diagnose display issues."""

import os
from datetime import datetime, timedelta

os.environ.setdefault("LOG_LEVEL", "WARNING")  # suppress config INFO

from config import INFLUXDB_BUCKET, INFLUXDB_ORG, INFLUXDB_TOKEN, INFLUXDB_URL
from influxdb_client import InfluxDBClient

if not INFLUXDB_TOKEN:
    print("ERROR: InfluxDB not configured (no token)")
    exit(1)

client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
query_api = client.query_api()

print("=== manual_values (weight) last 180 days ===\n")
q = f'''
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: -180d)
  |> filter(fn: (r) => r._measurement == "manual_values")
  |> filter(fn: (r) => r._field == "weight")
  |> sort(columns: ["_time"], desc: true)
'''
for table in query_api.query(q):
    for record in table.records:
        date = record.values.get("date", "?")
        deleted = record.values.get("deleted", "")
        val = record.get_value()
        ts = record.get_time()
        print(f"  date={date}  weight={val}  deleted={deleted!r}  _time={ts}")

print("\n=== daily_health (weight) last 30 days ===\n")
q = f'''
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: -30d)
  |> filter(fn: (r) => r._measurement == "daily_health")
  |> filter(fn: (r) => r._field == "weight")
  |> sort(columns: ["_time"], desc: true)
'''
for table in query_api.query(q):
    for record in table.records:
        date = record.values.get("date", "?")
        val = record.get_value()
        ts = record.get_time()
        print(f"  date={date}  weight={val}  _time={ts}")

client.close()
print("\nDone.")
