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
from datetime import datetime, timedelta, date

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
        
        # Get existing Strava IDs from InfluxDB to avoid duplicates
        query_api = influxdb.query_api()
        # Use range that matches our sync - extend for full historical syncs
        range_days = min(max(fetch_days + 30, 365), 4000)  # 4000d ~ 11 years
        existing_query = f'from(bucket: "{INFLUXDB_BUCKET}") |> range(start: -{range_days}d) |> filter(fn: (r) => r._measurement == "workouts") |> filter(fn: (r) => r._field == "strava_id")'
        try:
            result = query_api.query(existing_query)
            existing_ids = set()
            
            # Extract IDs from query result
            for table in result:
                for record in table.records:
                    val = str(record.get_value())
                    if val.isdigit():
                        existing_ids.add(val)
            
            print(f"Found {len(existing_ids)} existing Strava IDs")
        except Exception as e:
            print(f"Query error: {e}")
            existing_ids = set()
        
        print(f"Found {len(existing_ids)} existing workouts in InfluxDB")
        
        synced = 0
        skipped = 0
        for activity in activities:
            strava_id = str(activity.get("id", ""))
            date = activity.get("date", "")
            
            # Skip if already exists (by Strava ID)
            if strava_id in existing_ids:
                skipped += 1
                continue
                
            try:
                strava_id = str(activity.get("id", ""))
                date = activity.get("date", "")
                time = activity.get("time", "")
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
                    .field("name", activity.get("name", ""))
                
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
                    .field("name", activity.get("name", ""))
                
                write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=cache_point)
                existing_ids.add(strava_id)  # Add to avoid duplicates in same run
                synced += 1
            except Exception as e:
                print(f"Error syncing activity {activity.get('id')}: {e}")
        
        print(f"Synced {synced} new, skipped {skipped} existing")

        # Write recent workouts cache to disk for fast dashboard loads
        try:
            if activities:
                recent = []
                seen = set()
                for activity in activities:
                    strava_id = str(activity.get("id", ""))
                    if strava_id in seen:
                        continue
                    seen.add(strava_id)
                    recent.append({
                        "date": activity.get("date", ""),
                        "type": activity.get("type", ""),
                        "duration": activity.get("duration"),
                        "duration_minutes": activity.get("duration"),
                        "avg_hr": activity.get("avg_hr"),
                        "max_hr": activity.get("max_hr"),
                        "calories": activity.get("calories"),
                        "suffer_score": activity.get("suffer_score"),
                        "distance": activity.get("distance"),
                        "elevation_gain": activity.get("elevation_gain"),
                        "start_time": activity.get("time", ""),
                        "name": activity.get("name", ""),
                        "strava_id": strava_id,
                    })
                recent = sorted(recent, key=lambda x: (x.get("date", ""), x.get("start_time", "")), reverse=True)
                payload = {
                    "loaded_at": datetime.now().isoformat(),
                    "data": recent[:200],
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
