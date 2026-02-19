#!/usr/bin/env python3
"""
Sync Apple Health export.xml into InfluxDB daily_health measurement.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

from config import INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, INFLUXDB_BUCKET

DATE_FMT = "%Y-%m-%d %H:%M:%S %z"


def _parse_dt(value: str) -> datetime:
    return datetime.strptime(value, DATE_FMT)


def parse_apple_health(xml_path: Path) -> dict[str, dict[str, float]]:
    """
    Build daily aggregates for dashboard-compatible fields:
    - sleep_duration_hours
    - steps
    - resting_hr (if present in export)
    - hrv_avg (if present in export)
    """
    daily = defaultdict(
        lambda: {
            "_sleep_asleep_hours": 0.0,
            "_sleep_inbed_hours": 0.0,
            "steps": 0.0,
            "_resting_hr_values": [],
            "_heart_rate_values": [],
            "_hrv_values": [],
        }
    )

    processed = 0
    for _, elem in ET.iterparse(str(xml_path), events=("end",)):
        if elem.tag != "Record":
            continue

        rec_type = elem.attrib.get("type", "")
        start = elem.attrib.get("startDate")
        end = elem.attrib.get("endDate")
        value = elem.attrib.get("value")
        if not start or not value:
            elem.clear()
            continue

        try:
            start_dt = _parse_dt(start)
            day = start_dt.date().isoformat()
            row = daily[day]

            if rec_type == "HKQuantityTypeIdentifierStepCount":
                row["steps"] += float(value)

            elif rec_type == "HKCategoryTypeIdentifierSleepAnalysis":
                if end:
                    end_dt = _parse_dt(end)
                    hours = max(0.0, (end_dt - start_dt).total_seconds() / 3600.0)
                    # Prefer true Asleep segments. Fall back to InBed if Asleep is absent.
                    if "Asleep" in value:
                        row["_sleep_asleep_hours"] += hours
                    elif "InBed" in value:
                        row["_sleep_inbed_hours"] += hours

            elif rec_type == "HKQuantityTypeIdentifierRestingHeartRate":
                row["_resting_hr_values"].append(float(value))

            elif rec_type == "HKQuantityTypeIdentifierHeartRate":
                row["_heart_rate_values"].append(float(value))

            elif rec_type == "HKQuantityTypeIdentifierHeartRateVariabilitySDNN":
                row["_hrv_values"].append(float(value))
        except Exception:
            # Skip malformed records and continue full-file import.
            pass

        processed += 1
        if processed % 500000 == 0:
            print(f"Processed {processed} Apple Health records...")
        elem.clear()

    # Finalize daily aggregates.
    def p10(values: list[float]) -> float:
        vals = sorted(values)
        if not vals:
            return 0.0
        idx = max(0, int(round((len(vals) - 1) * 0.10)))
        return vals[idx]

    finalized: dict[str, dict[str, float]] = {}
    for day, row in daily.items():
        payload: dict[str, float] = {}
        sleep_hours = row["_sleep_asleep_hours"] if row["_sleep_asleep_hours"] > 0 else row["_sleep_inbed_hours"]
        if sleep_hours > 0:
            payload["sleep_duration_hours"] = round(sleep_hours, 3)
        if row["steps"] > 0:
            payload["steps"] = int(row["steps"])
        if row["_resting_hr_values"]:
            vals = row["_resting_hr_values"]
            payload["resting_hr"] = round(sum(vals) / len(vals), 2)
        elif row["_heart_rate_values"]:
            # If Apple export has no explicit resting-HR record, use low-end HR as proxy.
            payload["resting_hr"] = round(p10(row["_heart_rate_values"]), 2)
        if row["_heart_rate_values"]:
            vals = row["_heart_rate_values"]
            payload["avg_hr"] = round(sum(vals) / len(vals), 2)
        if row["_hrv_values"]:
            vals = row["_hrv_values"]
            payload["hrv_avg"] = round(sum(vals) / len(vals), 2)
        if payload:
            finalized[day] = payload

    return finalized


def write_daily_health(daily_payload: dict[str, dict[str, float]]) -> int:
    if not INFLUXDB_TOKEN:
        raise RuntimeError("Missing InfluxDB token")

    client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
    write_api = client.write_api(write_options=SYNCHRONOUS)

    try:
        for day, fields in daily_payload.items():
            ts = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
            point = Point("daily_health").tag("date", day).time(ts)
            for key, val in fields.items():
                point = point.field(key, val)
            write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
    finally:
        write_api.close()
        client.close()

    return len(daily_payload)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Apple Health XML to InfluxDB.")
    parser.add_argument(
        "--xml",
        default="apple_health_export/export.xml",
        help="Path to Apple Health export.xml file",
    )
    args = parser.parse_args()

    xml_path = Path(args.xml)
    if not xml_path.exists():
        raise FileNotFoundError(f"Apple Health file not found: {xml_path}")

    print(f"Reading Apple Health export: {xml_path}")
    daily = parse_apple_health(xml_path)
    print(f"Prepared {len(daily)} daily_health points")
    written = write_daily_health(daily)
    print(f"Wrote {written} daily_health points to InfluxDB bucket '{INFLUXDB_BUCKET}'")
    fields = ["sleep_duration_hours", "steps", "resting_hr", "avg_hr", "hrv_avg"]
    for f in fields:
        count = sum(1 for _, payload in daily.items() if f in payload)
        print(f"Days with {f}: {count}")


if __name__ == "__main__":
    main()
