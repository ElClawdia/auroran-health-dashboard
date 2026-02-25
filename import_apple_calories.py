#!/usr/bin/env python3
"""
Import Apple Health calories data for specific dates to InfluxDB.
Extracts:
- BasalEnergyBurned (resting metabolic calories)
- ActiveEnergyBurned (exercise/movement calories)
- Total daily calories = basal + active
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
import xml.etree.ElementTree as ET

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

from config import INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, INFLUXDB_BUCKET

DATE_FMT = "%Y-%m-%d %H:%M:%S %z"


def _parse_dt(value: str) -> datetime:
    return datetime.strptime(value, DATE_FMT)


def parse_apple_calories(xml_path: Path, target_dates: set[str]) -> dict[str, dict[str, float]]:
    """
    Parse Apple Health XML and extract calorie data for target dates.
    Returns dict of {date: {basal_calories, active_calories, total_calories}}
    """
    daily = defaultdict(lambda: {"basal": 0.0, "active": 0.0})

    processed = 0
    for _, elem in ET.iterparse(str(xml_path), events=("end",)):
        if elem.tag != "Record":
            elem.clear()
            continue

        rec_type = elem.attrib.get("type", "")
        start = elem.attrib.get("startDate")
        value = elem.attrib.get("value")
        
        if not start or not value:
            elem.clear()
            continue

        # Only process calorie records
        if rec_type not in ("HKQuantityTypeIdentifierBasalEnergyBurned", 
                           "HKQuantityTypeIdentifierActiveEnergyBurned"):
            elem.clear()
            continue

        try:
            start_dt = _parse_dt(start)
            day = start_dt.date().isoformat()
            
            # Only process target dates
            if day not in target_dates:
                elem.clear()
                continue

            cal_value = float(value)
            
            if rec_type == "HKQuantityTypeIdentifierBasalEnergyBurned":
                daily[day]["basal"] += cal_value
            elif rec_type == "HKQuantityTypeIdentifierActiveEnergyBurned":
                daily[day]["active"] += cal_value
                
        except Exception as e:
            pass

        processed += 1
        if processed % 100000 == 0:
            print(f"Processed {processed} records...")
        elem.clear()

    # Calculate totals
    result = {}
    for day, data in daily.items():
        if day in target_dates:
            result[day] = {
                "basal_calories": round(data["basal"], 1),
                "active_calories": round(data["active"], 1),
                "total_calories": round(data["basal"] + data["active"], 1)
            }
    
    return result


def write_to_influxdb(calorie_data: dict[str, dict[str, float]]) -> int:
    """Write calorie data to InfluxDB daily_health measurement."""
    if not INFLUXDB_TOKEN:
        raise RuntimeError("Missing InfluxDB token")

    client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
    write_api = client.write_api(write_options=SYNCHRONOUS)

    try:
        for day, fields in calorie_data.items():
            ts = datetime.fromisoformat(day).replace(hour=12, tzinfo=timezone.utc)
            point = Point("daily_health").tag("date", day).time(ts)
            for key, val in fields.items():
                point = point.field(key, val)
            write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
            print(f"  Wrote {day}: basal={fields['basal_calories']}, active={fields['active_calories']}, total={fields['total_calories']} kcal")
    finally:
        write_api.close()
        client.close()

    return len(calorie_data)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import Apple Health calories (basal + active) to InfluxDB daily_health.",
        epilog="""
Examples:
  python3 import_apple_calories.py --days 2
      Import calories for last 2 days.
  python3 import_apple_calories.py --days 7
      Import calories for last week.
  python3 import_apple_calories.py --dates 2026-02-20 2026-02-21
      Import specific dates.
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--xml",
        default="apple_health_export/export.xml",
        help="Path to Apple Health export.xml file",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=2,
        help="Number of days to import (counting back from today)",
    )
    parser.add_argument(
        "--dates",
        nargs="*",
        help="Specific dates to import (YYYY-MM-DD format)",
    )
    args = parser.parse_args()

    xml_path = Path(args.xml)
    if not xml_path.exists():
        raise FileNotFoundError(f"Apple Health file not found: {xml_path}")

    # Determine target dates
    if args.dates:
        target_dates = set(args.dates)
    else:
        today = datetime.now().date()
        target_dates = set()
        for i in range(args.days):
            date = (today - timedelta(days=i)).isoformat()
            target_dates.add(date)
    
    print(f"Target dates: {sorted(target_dates)}")
    print(f"Reading Apple Health export: {xml_path}")
    
    calorie_data = parse_apple_calories(xml_path, target_dates)
    
    if not calorie_data:
        print("No calorie data found for target dates!")
        return
    
    print(f"\nCalorie data found:")
    for day in sorted(calorie_data.keys()):
        data = calorie_data[day]
        print(f"  {day}: Basal={data['basal_calories']:.0f} + Active={data['active_calories']:.0f} = Total={data['total_calories']:.0f} kcal")
    
    print(f"\nWriting to InfluxDB bucket '{INFLUXDB_BUCKET}'...")
    written = write_to_influxdb(calorie_data)
    print(f"Successfully wrote {written} days of calorie data!")


if __name__ == "__main__":
    main()
