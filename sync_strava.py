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
    
    # Get existing Strava IDs from InfluxDB to avoid duplicates
    query_api = influxdb.query_api()
    existing_query = f'from(bucket: "{INFLUXDB_BUCKET}") |> range(start: -30d) |> filter(fn: (r) => r._measurement =~ /workouts_/) |> keep(columns: ["strava_id"])'
    try:
        existing_df = query_api.query_data_frame(existing_query)
        existing_ids = set(existing_df['strava_id'].dropna().astype(str)) if not existing_df.empty else set()
    except Exception as e:
        print(f"Query error: {e}")
        existing_ids = set()
    
    print(f"Found {len(existing_ids)} existing workouts in InfluxDB")
    
    synced = 0
    skipped = 0
    for activity in activities:
        strava_id = str(activity.get("id", ""))
        
        # Skip if already exists
        if strava_id in existing_ids:
            skipped += 1
            continue
            
        try:
            # Use Strava ID as part of measurement for idempotent writes
            point = Point("workouts")\
                .tag("type", activity.get("type", "Unknown"))\
                .tag("strava_id", strava_id)\
                .field("date", activity.get("date", ""))\
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
