#!/usr/bin/env python3
"""Audit manual_values for malformed dates and tombstone-heavy entries."""

from __future__ import annotations

import re
from collections import Counter

from influxdb_client import InfluxDBClient
import argparse

from config import INFLUXDB_BUCKET, INFLUXDB_ORG, INFLUXDB_TOKEN, INFLUXDB_URL


VALID_DATE_RE = re.compile(r"^20\d{2}-\d{2}-\d{2}$")


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit manual_values for malformed dates and duplicate tombstones.")
    parser.add_argument("--url", default=INFLUXDB_URL)
    parser.add_argument("--org", default=INFLUXDB_ORG)
    parser.add_argument("--bucket", default=INFLUXDB_BUCKET)
    parser.add_argument("--token", default=INFLUXDB_TOKEN)
    args = parser.parse_args()

    if not args.token:
        raise RuntimeError("Missing InfluxDB token")

    client = InfluxDBClient(url=args.url, token=args.token, org=args.org, timeout=120_000)
    query_api = client.query_api()
    try:
        query = f'''
        from(bucket: "{args.bucket}")
          |> range(start: -3650d)
          |> filter(fn: (r) => r._measurement == "manual_values")
        '''
        bad_rows = []
        deleted = Counter()
        by_key = Counter()

        for record in query_api.query_stream(query):
            date = record.values.get("date", "")
            field = record.get_field()
            deleted[str(record.values.get("deleted", "false")).lower()] += 1
            by_key[(date, field)] += 1
            if date and not VALID_DATE_RE.match(date):
                bad_rows.append(
                    {
                        "date": date,
                        "field": field,
                        "time": str(record.get_time()),
                        "value": record.get_value(),
                    }
                )

        print(f"manual_values rows: {sum(deleted.values())}")
        print(f"deleted tag counts: {dict(deleted)}")
        print(f"duplicate date+field combos (>1 rows): {sum(1 for v in by_key.values() if v > 1)}")
        if bad_rows:
            print("malformed date rows:")
            for row in bad_rows:
                print(row)
        else:
            print("no malformed manual_values dates found")
    finally:
        client.close()


if __name__ == "__main__":
    main()
