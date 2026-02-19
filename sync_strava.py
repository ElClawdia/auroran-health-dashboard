#!/usr/bin/env python3
"""
Sync Strava workouts to InfluxDB
Run periodically via cron: */10 * * * *
"""

import os
import sys
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent))

import subprocess

from config import INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, INFLUXDB_BUCKET
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

STRAVA_CLIENT_ID = ""
STRAVA_CLIENT_SECRET = ""
STRAVA_REFRESH_TOKEN = ""
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
    
    # Initialize clients
    strava = StravaClient(
        access_token=STRAVA_ACCESS_TOKEN,
        client_id=STRAVA_CLIENT_ID,
        client_secret=STRAVA_CLIENT_SECRET,
        refresh_token=STRAVA_REFRESH_TOKEN
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
        
        # Get existing workout dates from InfluxDB to avoid duplicates
        query_api = influxdb.query_api()
        # Query ALL workouts (not just 30 days) to avoid duplicates
        existing_query = f'from(bucket: "{INFLUXDB_BUCKET}") |> range(start: -365d) |> filter(fn: (r) => r._measurement == "workouts") |> filter(fn: (r) => r._field == "strava_id")'
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
                    .field("suffer_score", to_float(ss))\
                    .field("name", activity.get("name", ""))
                
                write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=cache_point)
                existing_ids.add(strava_id)  # Add to avoid duplicates in same run
                synced += 1
            except Exception as e:
                print(f"Error syncing activity {activity.get('id')}: {e}")
        
        print(f"Synced {synced} new, skipped {skipped} existing")
        
    finally:
        # Proper cleanup to ensure all writes are flushed
        write_api.close()
        influxdb.close()
    
    return True

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync Strava workouts to InfluxDB")
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
