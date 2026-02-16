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
    
    synced = 0
    for activity in activities:
        try:
            point = Point("workouts")\
                .tag("type", activity.get("type", "Unknown"))\
                .tag("strava_id", str(activity.get("id", "")))\
                .field("date", activity.get("date", ""))\
                .field("duration", activity.get("duration", 0))\
                .field("distance", activity.get("distance", 0))\
                .field("elevation_gain", activity.get("elevation_gain", 0))\
                .field("avg_hr", activity.get("avg_hr", 0))\
                .field("max_hr", activity.get("max_hr", 0))\
                .field("suffer_score", activity.get("suffer_score", 0))\
                .field("name", activity.get("name", ""))
            
            write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
            synced += 1
        except Exception as e:
            print(f"Error syncing activity {activity.get('id')}: {e}")
    
    print(f"Synced {synced}/{len(activities)} activities")
    return True

if __name__ == "__main__":
    sync_strava_to_influxdb()
