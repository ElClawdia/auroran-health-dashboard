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

from config import INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, INFLUXDB_BUCKET, STRAVA_ACCESS_TOKEN
from strava_client import StravaClient
from influxdb_client import InfluxDBClient, Point
from datetime import datetime

def sync_strava_to_influxdb():
    """Sync Strava activities to InfluxDB"""
    
    # Check config
    if not STRAVA_ACCESS_TOKEN:
        print("ERROR: No Strava access token configured")
        return False
    
    if not INFLUXDB_TOKEN:
        print("ERROR: No InfluxDB token configured")
        return False
    
    # Initialize clients
    strava = StravaClient(STRAVA_ACCESS_TOKEN)
    influxdb = InfluxDBClient(
        url=INFLUXDB_URL,
        token=INFLUXDB_TOKEN,
        org=INFLUXDB_ORG
    )
    write_api = influxdb.write_api()
    
    # Get recent activities (last 7 days)
    activities = strava.get_activities(7)
    
    print(f"Syncing {len(activities)} activities to InfluxDB...")
    
    # Get existing workout dates from InfluxDB to avoid duplicates
    query_api = influxdb.query_api()
    # Query all workouts - returns list of DataFrames
    existing_query = f'from(bucket: "{INFLUXDB_BUCKET}") |> range(start: -30d) |> filter(fn: (r) => r._measurement == "workouts")'
    try:
        result = query_api.query_data_frame(existing_query)
        existing_dates = set()
        if isinstance(result, list):
            for df in result:
                if hasattr(df, 'columns') and '_value' in df.columns and 'date' in df.get('_value', []):
                    # Get unique dates from the 'date' tag
                    pass
                # Check for date in the dataframe
                for col in ['date', '_value']:
                    if col in df.columns:
                        existing_dates.update(df[col].dropna().astype(str).unique())
        elif hasattr(result, 'columns'):
            for col in result.columns:
                existing_dates.update(result[col].dropna().astype(str).unique())
        # Filter to look like dates (YYYY-MM-DD)
        existing_dates = {d for d in existing_dates if d and '-' in d and len(str(d)) == 10}
        print(f"Found {len(existing_dates)} existing workout dates: {list(existing_dates)[:5]}...")
        existing_ids = existing_dates  # Use dates as the dedup key
    except Exception as e:
        print(f"Query error: {e}")
        existing_ids = set()
    
    print(f"Found {len(existing_ids)} existing workouts in InfluxDB")
    
    synced = 0
    skipped = 0
    for activity in activities:
        date = activity.get("date", "")
        
        # Skip if already exists (by date)
        if date in existing_ids:
            skipped += 1
            continue
            
        try:
            strava_id = str(activity.get("id", ""))
            date = activity.get("date", "")
            # Use Strava ID as part of measurement for idempotent writes
            point = Point("workouts")\
                .tag("type", activity.get("type", "Unknown"))\
                .tag("date", date)\
                .field("strava_id", strava_id)\
                .field("duration", activity.get("duration", 0))\
                .field("distance", activity.get("distance", 0))\
                .field("elevation_gain", activity.get("elevation_gain", 0))\
                .field("avg_hr", activity.get("avg_hr", 0))\
                .field("max_hr", activity.get("max_hr", 0))\
                .field("suffer_score", activity.get("suffer_score", 0))\
                .field("name", activity.get("name", ""))
            
            write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
            existing_ids.add(strava_id)  # Add to avoid duplicates in same run
            synced += 1
        except Exception as e:
            print(f"Error syncing activity {activity.get('id')}: {e}")
    
    print(f"Synced {synced} new, skipped {skipped} existing")
    return True

if __name__ == "__main__":
    sync_strava_to_influxdb()
