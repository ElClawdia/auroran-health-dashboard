#!/usr/bin/env python3
"""
Sync Strava workouts to InfluxDB
Run periodically via cron: */10 * * * *
"""

import os
import sys
import json
from pathlib import Path

# Suppress config INFO/DEBUG logs for CLI runs
os.environ.setdefault("LOG_LEVEL", "WARNING")

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent))

import subprocess

from config import (
    INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, INFLUXDB_BUCKET,
    STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN,
)
from datetime import datetime, timedelta, date, timezone
from collections import defaultdict

# Get fresh Strava token (auto-refreshes if needed)
try:
    renew_script = Path(__file__).parent / "renew-strava-tokens" / "renew_strava_token.py"
    STRAVA_ACCESS_TOKEN = subprocess.run(
        [sys.executable, "renew_strava_token.py"],
        capture_output=True, text=True, timeout=30,
        cwd=str(renew_script.parent)  # Run from the renew-strava-tokens directory
    ).stdout.strip()
except Exception as e:
    print(f"ERROR: Could not get Strava token: {e}")
    sys.exit(1)

from strava_client import StravaClient
from influxdb_client import InfluxDBClient, Point
from training_load import calculate_training_load


def _parse_iso_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def build_activity_timestamp(activity: dict) -> datetime:
    """Return the workout's UTC timestamp, preferring Strava's UTC start time."""
    parsed = _parse_iso_datetime(activity.get("start_date_utc") or activity.get("start_date"))
    if parsed:
        return parsed.astimezone(timezone.utc)

    activity_date = activity.get("date", "")
    activity_time = activity.get("time", "")
    if activity_date and activity_time:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(f"{activity_date} {activity_time}", fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    if activity_date:
        return datetime.strptime(activity_date, "%Y-%m-%d").replace(hour=12, tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _fetch_existing_activity_meta(query_api, range_days: int) -> dict[str, dict]:
    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -{range_days}d)
      |> filter(fn: (r) => r._measurement == "workouts" or r._measurement == "workout_cache")
      |> filter(fn: (r) => r._field == "strava_id" or r._field == "start_time")
    '''

    by_point = {}
    for record in query_api.query_stream(query):
        key = f'{record.values.get("_measurement", "")}|{record.get_time()}'
        entry = by_point.setdefault(
            key,
            {
                "measurement": record.values.get("_measurement", ""),
                "date": record.values.get("date", ""),
            },
        )
        entry[record.get_field()] = record.get_value()

    by_id: dict[str, dict] = defaultdict(
        lambda: {"measurements": set(), "dates": set(), "times": set()}
    )
    for entry in by_point.values():
        strava_id = str(entry.get("strava_id") or "").strip()
        if not strava_id or not strava_id.isdigit():
            continue
        meta = by_id[strava_id]
        meta["measurements"].add(entry.get("measurement", ""))
        if entry.get("date"):
            meta["dates"].add(entry["date"])
        if entry.get("start_time"):
            meta["times"].add(entry["start_time"])

    return by_id
import argparse


def sync_strava_to_influxdb(days=None, force=False, newer_than=None):
    """Sync Strava activities to InfluxDB"""
    
    # Check config
    if not STRAVA_ACCESS_TOKEN:
        print("ERROR: No Strava access token configured")
        return False
    
    if not INFLUXDB_TOKEN:
        print("ERROR: No InfluxDB token configured")
        return False
    
    # Initialize clients (config credentials used for token refresh during long syncs)
    strava = StravaClient(
        access_token=STRAVA_ACCESS_TOKEN,
        client_id=STRAVA_CLIENT_ID or "",
        client_secret=STRAVA_CLIENT_SECRET or "",
        refresh_token=STRAVA_REFRESH_TOKEN or "",
    )
    influxdb = InfluxDBClient(
        url=INFLUXDB_URL,
        token=INFLUXDB_TOKEN,
        org=INFLUXDB_ORG
    )
    from influxdb_client.client.write_api import SYNCHRONOUS
    write_api = influxdb.write_api(write_options=SYNCHRONOUS)
    
    try:
        # Determine how far back to fetch
        if force:
            # Full historical sync
            fetch_days = 365 * 3
            print(f"Force sync: fetching ~3 years of history...")
        elif newer_than:
            # Fetch from specific date
            try:
                dt = datetime.strptime(newer_than, "%Y%m%d")
                fetch_days = (datetime.now() - dt).days
            except ValueError:
                print(f"ERROR: Invalid date format for --newer-than: {newer_than}. Use YYYYMMDD")
                return False
            print(f"Fetching activities since {newer_than} ({fetch_days} days)...")
        elif days:
            fetch_days = days
            print(f"Fetching last {fetch_days} days...")
        else:
            # Default: incremental - only last 30 days (quick cron run)
            fetch_days = 30
            print(f"Incremental sync: fetching last {fetch_days} days...")
        
        activities = strava.get_activities(fetch_days)
        
        print(f"Syncing {len(activities)} activities to InfluxDB...")
        
        # Get existing Strava IDs + stored date/time so we can repair mismatches.
        query_api = influxdb.query_api()
        # Use range that matches our sync - extend for full historical syncs
        range_days = min(max(fetch_days + 30, 365), 4000)  # 4000d ~ 11 years
        try:
            existing_meta = _fetch_existing_activity_meta(query_api, range_days)
            existing_ids = set(existing_meta.keys())
            print(f"Found {len(existing_ids)} existing Strava IDs")
        except Exception as e:
            print(f"Query error: {e}")
            existing_meta = {}
            existing_ids = set()
        
        print(f"Found {len(existing_ids)} existing workouts in InfluxDB")
        
        synced = 0
        repaired = 0
        skipped = 0
        for activity in activities:
            strava_id = str(activity.get("id", ""))
            date = activity.get("date", "")
            time = activity.get("time", "")
            meta = existing_meta.get(strava_id)

            # Skip only if the activity is already stored with matching local date/time
            # in both measurements. Otherwise rewrite it to repair stale UTC/local mismatches.
            if meta and {"workouts", "workout_cache"}.issubset(meta["measurements"]) and (
                not date or date in meta["dates"]
            ) and (
                not time or time in meta["times"]
            ):
                skipped += 1
                continue
                
            try:
                strava_id = str(activity.get("id", ""))
                date = activity.get("date", "")
                time = activity.get("time", "")
                activity_ts = build_activity_timestamp(activity)
                # Use Strava ID as part of measurement for idempotent writes
                # Ensure all numeric fields are float to avoid type conflicts, handle None
                def to_float(val, default=0.0):
                    if val is None:
                        return default
                    try:
                        return float(val)
                    except (ValueError, TypeError):
                        return default
                
                # Use Strava suffer_score (Relative Effort); fallback to HR-based load when missing
                ss = to_float(activity.get("suffer_score"))
                if ss <= 0:
                    dur = int(to_float(activity.get("duration"), 0))
                    ss = calculate_training_load(
                        duration_minutes=dur,
                        avg_hr=activity.get("avg_hr"),
                        max_hr=activity.get("max_hr"),
                        suffer_score=None,
                    ) if dur > 0 else 0.0
                
                # Write to both measurements: workouts (legacy) and workout_cache (optimized)
                point = Point("workouts")\
                    .tag("type", activity.get("type", "Unknown"))\
                    .tag("date", date)\
                    .field("strava_id", strava_id)\
                    .field("date", date)\
                    .field("start_time", time)\
                    .field("duration", to_float(activity.get("duration")))\
                    .field("distance", to_float(activity.get("distance")))\
                    .field("elevation_gain", to_float(activity.get("elevation_gain")))\
                    .field("avg_hr", to_float(activity.get("avg_hr")))\
                    .field("max_hr", to_float(activity.get("max_hr")))\
                    .field("suffer_score", to_float(ss))\
                    .field("calories", to_float(activity.get("calories")))\
                    .field("name", activity.get("name", ""))\
                    .time(activity_ts)
                
                write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
                
                # Also write to workout_cache (optimized for faster queries)
                cache_point = Point("workout_cache")\
                    .tag("type", activity.get("type", "Unknown"))\
                    .tag("date", date)\
                    .field("strava_id", strava_id)\
                    .field("start_time", time)\
                    .field("duration", to_float(activity.get("duration")))\
                    .field("distance", to_float(activity.get("distance")))\
                    .field("elevation_gain", to_float(activity.get("elevation_gain")))\
                    .field("avg_hr", to_float(activity.get("avg_hr")))\
                    .field("max_hr", to_float(activity.get("max_hr")))\
                    .field("calories", to_float(activity.get("calories")))\
                    .field("suffer_score", to_float(ss))\
                    .field("name", activity.get("name", ""))\
                    .time(activity_ts)
                
                write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=cache_point)
                existing_ids.add(strava_id)  # Add to avoid duplicates in same run
                if meta:
                    meta["measurements"].update({"workouts", "workout_cache"})
                    if date:
                        meta["dates"].add(date)
                    if time:
                        meta["times"].add(time)
                    repaired += 1
                else:
                    existing_meta[strava_id] = {
                        "measurements": {"workouts", "workout_cache"},
                        "dates": {date} if date else set(),
                        "times": {time} if time else set(),
                    }
                    synced += 1
            except Exception as e:
                print(f"Error syncing activity {activity.get('id')}: {e}")
        
        print(f"Synced {synced} new, repaired {repaired}, skipped {skipped} existing")

        # Write recent workouts cache to disk using Influx data (not just latest API results)
        try:
            cutoff = (datetime.now() - timedelta(days=42)).strftime("%Y-%m-%d")
            measurements = ["workout_cache", "workouts"]
            recent_workouts = {}
            cache_query = f'''
            from(bucket: \"{INFLUXDB_BUCKET}\")
              |> range(start: -42d)
              |> filter(fn: (r) => r._measurement == \"workout_cache\" or r._measurement == \"workouts\")
              |> filter(fn: (r) => r.date >= \"{cutoff}\")
            '''
            for record in query_api.query_stream(cache_query):
                key = f'{record.values.get("_measurement", "")}|{record.get_time()}'
                entry = recent_workouts.setdefault(
                    key,
                    {"date": record.values.get("date", ""), "type": record.values.get("type", "")},
                )
                entry[record.get_field()] = record.get_value()

            if recent_workouts:
                recent = list(recent_workouts.values())
                recent = sorted(recent, key=lambda x: (x.get("date", ""), x.get("start_time", "")), reverse=True)
                deduped = []
                seen = set()
                for workout in recent:
                    strava_id = workout.get("strava_id")
                    dedupe_key = f"strava:{strava_id}" if strava_id else "|".join(
                        [
                            str(workout.get("date", "")),
                            str(workout.get("start_time", "")),
                            str(workout.get("name", "")),
                            str(workout.get("type", "")),
                        ]
                    )
                    if dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                    deduped.append(workout)
                payload = {
                    "loaded_at": datetime.now().isoformat(),
                    "measurements": measurements,
                    "data": deduped[:200],
                }
                cache_file = Path(__file__).parent / "logs" / "recent_workouts_cache.json"
                cache_file.write_text(json.dumps(payload))
                print(f"Wrote recent workouts cache: {cache_file}")
        except Exception as e:
            print(f"Warning: failed to write recent workouts cache: {e}")
        
    finally:
        # Proper cleanup to ensure all writes are flushed
        write_api.close()
        influxdb.close()
    
    return True

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Sync Strava workouts to InfluxDB.",
        epilog="""
Examples:
  python3 sync_strava.py
      Incremental sync (last 30 days).
  python3 sync_strava.py --days 7
      Sync last 7 days only.
  python3 sync_strava.py --force
      Full sync (~3 years of history).
  python3 sync_strava.py --newer-than 20240101
      Sync activities since 2024-01-01.
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--days", "-d", type=int, default=None,
        help="Number of days to fetch (default: 30 for incremental sync)"
    )
    parser.add_argument(
        "--force", "-f", action="store_true",
        help="Force full sync: fetch ~3 years of history"
    )
    parser.add_argument(
        "--newer-than", "-n", type=str, default=None,
        help="Fetch activities newer than YYYYMMDD (e.g., 20240101)"
    )
    args = parser.parse_args()
    
    sync_strava_to_influxdb(
        days=args.days,
        force=args.force,
        newer_than=args.newer_than,
    )
