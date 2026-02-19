#!/usr/bin/env python3
"""
Health Dashboard - Flask Web Server
Auroran Health Command Center 🦞

Run: python app.py
Access: http://localhost:5000
"""

import os
import sys
import logging
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request
from influxdb_client import InfluxDBClient, Point, WriteOptions
from influxdb_client.client.write_api import SYNCHRONOUS
import pandas as pd

# Configure logging with timestamps
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Create logs directory
log_dir = Path(__file__).parent / "logs"
log_dir.mkdir(exist_ok=True)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(log_dir / "health-dashboard.log", maxBytes=10*1024*1024, backupCount=5)
    ]
)
logger = logging.getLogger(__name__)

# Import our modules
from suunto_client import SuuntoClient
from strava_client import StravaClient, MockStravaClient
from planner import ExercisePlanner
from training_load import calculate_training_load, calculate_ctl_atl_tsb, calculate_pmc_series, get_status_description

app = Flask(__name__)

# Configuration
from config import INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, INFLUXDB_BUCKET
from config import SUUNTO_CLIENT_ID, SUUNTO_CLIENT_SECRET
from config import STRAVA_ACCESS_TOKEN, STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN
from config import FLASK_HOST, FLASK_PORT, FLASK_DEBUG

# Initialize InfluxDB client with fallback
influx_client = None
write_api = None
query_api = None

if INFLUXDB_TOKEN:
    try:
        influx_client = InfluxDBClient(
            url=INFLUXDB_URL,
            token=INFLUXDB_TOKEN,
            org=INFLUXDB_ORG
        )
        # Quick health check
        health = influx_client.health()
        if health.status == "pass":
            write_api = influx_client.write_api(write_options=SYNCHRONOUS)
            query_api = influx_client.query_api()
            logger.info(f"Connected to InfluxDB at {INFLUXDB_URL}")
        else:
            logger.warning("InfluxDB health check failed, using demo mode")
            influx_client = None
    except Exception as e:
        logger.error(f"Could not connect to InfluxDB: {e}")
        logger.warning("Running in demo mode with mock data")
        influx_client = None

# Initialize modules
suunto = SuuntoClient(SUUNTO_CLIENT_ID, SUUNTO_CLIENT_SECRET)
strava = StravaClient(
    access_token=STRAVA_ACCESS_TOKEN,
    client_id=STRAVA_CLIENT_ID,
    client_secret=STRAVA_CLIENT_SECRET,
    refresh_token=STRAVA_REFRESH_TOKEN
) if STRAVA_ACCESS_TOKEN else MockStravaClient()
planner = ExercisePlanner()

# Generate mock data for demo mode
def get_mock_health_today():
    """Return realistic mock data for demo"""
    import random
    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "sleep_hours": round(7.0 + random.random() * 1.5, 1),
        "hrv": random.randint(38, 48),
        "resting_hr": random.randint(54, 62),
        "steps": random.randint(5000, 12000),
        "recovery_score": random.randint(70, 95),
        "training_load": round(random.uniform(0.8, 1.4), 2),
        "trend": {
            "sleep": "+12m" if random.random() > 0.5 else "-5m",
            "hrv": "+5ms ▲" if random.random() > 0.5 else "-3ms ▼",
            "resting_hr": "-2bpm ▼" if random.random() > 0.5 else "+1bpm ▲"
        }
    }

def get_mock_history(days=30):
    """Return realistic mock history"""
    import random
    dates = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days-1, -1, -1)]
    
    hrv = [35 + i + random.randint(-3, 5) for i in range(days)]
    resting_hr = [65 - i//3 + random.randint(-2, 2) for i in range(days)]
    sleep = [7 + (i % 5) * 0.2 + random.uniform(-0.3, 0.3) for i in range(days)]
    recovery = [60 + i + random.randint(-5, 10) for i in range(days)]
    
    return {
        "dates": dates,
        "hrv": [max(20, min(60, h)) for h in hrv],
        "resting_hr": [max(50, min(70, r)) for r in resting_hr],
        "sleep": [round(max(5, min(9, s)), 1) for s in sleep],
        "recovery": [max(30, min(100, r)) for r in recovery]
    }

def get_mock_workouts():
    """Return realistic mock workouts"""
    import random
    workouts = [
        {"date": "2026-02-15", "type": "Running", "duration": 35, "avg_hr": 145, "max_hr": 168, "feeling": "great"},
        {"date": "2026-02-14", "type": "Strength", "duration": 45, "avg_hr": 110, "max_hr": 135, "feeling": "good"},
        {"date": "2026-02-13", "type": "Rest", "duration": 0, "avg_hr": 62, "max_hr": 78, "feeling": "great"},
        {"date": "2026-02-12", "type": "Cycling", "duration": 60, "avg_hr": 128, "max_hr": 155, "feeling": "good"},
        {"date": "2026-02-11", "type": "HIIT", "duration": 25, "avg_hr": 155, "max_hr": 175, "feeling": "okay"},
        {"date": "2026-02-10", "type": "Running", "duration": 40, "avg_hr": 142, "max_hr": 165, "feeling": "great"},
    ]
    return workouts


@app.route('/')
def index():
    """Main dashboard page"""
    return render_template('index.html')


@app.route('/api/health/today')
def health_today():
    """Get today's health metrics"""
    logger.info("Fetching today's health metrics")
    if not query_api:
        return jsonify({"error": "InfluxDB not configured"}), 500
    
    try:
        query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: -30d)
          |> filter(fn: (r) => r._measurement == "daily_health")
          |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
        '''
        result = query_api.query_data_frame(query)
        if isinstance(result, list):
            if len(result) == 0:
                return jsonify({"error": "No data from InfluxDB"}), 404
            result = pd.concat(result, ignore_index=True)

        if result.empty:
            return jsonify({"error": "No data from InfluxDB"}), 404

        # Build one row per date and take the latest available date.
        if "date" not in result.columns:
            return jsonify({"error": "No date field in daily_health data"}), 404

        df = result.copy()
        numeric_cols = [
            c for c in ["sleep_duration_hours", "hrv_avg", "resting_hr", "steps", "recovery_score", "training_load"]
            if c in df.columns
        ]
        for c in numeric_cols:
            df[c] = pd.to_numeric(df[c], errors="coerce")

        if numeric_cols:
            df = df.groupby("date", as_index=False)[numeric_cols].mean()
        else:
            df = df[["date"]].drop_duplicates()

        df = df.sort_values("date")
        latest = df.iloc[-1]

        def clean_number(val):
            return None if pd.isna(val) else float(val)

        def latest_non_null(col):
            if col not in df.columns:
                return None
            series = df[col].dropna()
            if series.empty:
                return None
            return series.iloc[-1]

        sleep_val = latest_non_null("sleep_duration_hours")
        hrv_val = latest_non_null("hrv_avg")
        resting_hr_val = latest_non_null("resting_hr")
        recovery_val = latest_non_null("recovery_score")
        training_load_val = latest_non_null("training_load")
        steps_val = latest_non_null("steps")
        steps_clean = None if steps_val is None else int(float(steps_val))

        return jsonify({
            "date": latest.get("date", datetime.now().strftime("%Y-%m-%d")),
            "sleep_hours": clean_number(sleep_val),
            "hrv": clean_number(hrv_val),
            "resting_hr": clean_number(resting_hr_val),
            "steps": steps_clean,
            "recovery_score": clean_number(recovery_val),
            "training_load": clean_number(training_load_val)
        })
    except Exception as e:
        logger.error(f"Error fetching today's health: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/health/history')
def health_history():
    """Get historical health data"""
    logger.info("Fetching health history")
    days = request.args.get('days', 30, type=int)
    
    if not query_api:
        # Return mock data for demo
        logger.info("Using mock data for history (no InfluxDB)")
        return jsonify({"error": "No data from InfluxDB"}), 404
    
    try:
        query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: -{days}d)
          |> filter(fn: (r) => r._measurement == "daily_health")
          |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
        '''
        result = query_api.query_data_frame(query)

        if isinstance(result, list):
            if len(result) == 0:
                return jsonify({"error": "No data from InfluxDB"}), 404
            result = pd.concat(result, ignore_index=True)
        
        if result.empty or len(result) == 0:
            # Return mock trend data
            return jsonify({"error": "No data from InfluxDB"}), 404
            return jsonify({
                "dates": dates,
                "hrv": [30 + i + (i % 7) for i in range(days)],
                "resting_hr": [65 - i//3 for i in range(days)],
                "sleep": [7 + (i % 5) * 0.2 for i in range(days)],
                "recovery": [60 + i + (i % 10) for i in range(days)]
            })
        
        # Process actual data.
        # Ensure exactly one row per date and a strict trailing window (e.g. 30 days).
        df = result.copy()
        if "date" not in df.columns:
            return jsonify({"error": "No date field in daily_health data"}), 404

        numeric_cols = [
            c for c in ["hrv_avg", "resting_hr", "sleep_duration_hours", "recovery_score", "steps"]
            if c in df.columns
        ]
        if numeric_cols:
            for c in numeric_cols:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df = df.groupby("date", as_index=False)[numeric_cols].mean()
        else:
            df = df[["date"]].drop_duplicates()

        df = df.sort_values("date").tail(days)

        def clean_series(series, digits=2):
            return [None if pd.isna(v) else round(float(v), digits) for v in series.tolist()]

        return jsonify({
            "dates": df["date"].tolist(),
            "hrv": clean_series(df["hrv_avg"], 2) if "hrv_avg" in df else [],
            "resting_hr": clean_series(df["resting_hr"], 2) if "resting_hr" in df else [],
            "sleep": clean_series(df["sleep_duration_hours"], 2) if "sleep_duration_hours" in df else [],
            "recovery": clean_series(df["recovery_score"], 1) if "recovery_score" in df else [],
            "steps": clean_series(df["steps"], 0) if "steps" in df else []
        })
    except Exception as e:
        logger.error(f"Error fetching health history: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/workouts', methods=['GET', 'POST'])
def workouts():
    """Get or log workouts"""
    logger.info("Fetching workouts")
    if request.method == 'GET':
        if not query_api:
            # Return mock data for demo
            logger.info("Using mock workouts (no InfluxDB)")
            return jsonify({"error": "No workouts from InfluxDB"}), 404
        
        try:
            # Get last 365 days of workouts from InfluxDB
            query = f'''
            from(bucket: "{INFLUXDB_BUCKET}")
              |> range(start: -365d)
              |> filter(fn: (r) => r._measurement == "workouts")
              |> filter(fn: (r) => r._field != "date")
              |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
            '''
            # Always read from InfluxDB only (Strava → InfluxDB → Dashboard)
            result = query_api.query_data_frame(query)
            
            # Handle both list and DataFrame returns
            if isinstance(result, list):
                if len(result) == 0:
                    return jsonify({"error": "No workouts from InfluxDB"}), 404
                import pandas as pd
                result = pd.concat(result, ignore_index=True) if all(hasattr(r, 'empty') for r in result) else result
            elif hasattr(result, 'empty') and result.empty:
                return jsonify({"error": "No workouts from InfluxDB"}), 404
            
            # Sort by date descending (newest first)
            records = result.to_dict(orient='records')
            records.sort(key=lambda x: str(x.get('date', '')), reverse=True)
            return jsonify(records)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    # POST - Log new workout
    if not write_api:
        logger.warning("Cannot log workout: InfluxDB not configured")
        return jsonify({"error": "InfluxDB not configured"}), 500
    
    data = request.json
    logger.info(f"Logging workout: {data.get('type')} - {data.get('date')}")
    try:
        point = Point("workouts")\
            .tag("type", data.get("type", "Unknown"))\
            .tag("date", data.get("date", datetime.now().strftime("%Y-%m-%d")))\
            .field("duration_minutes", float(data.get("duration", 0)))\
            .field("avg_hr", float(data.get("avg_hr", 0)))\
            .field("max_hr", float(data.get("max_hr", 0)))\
            .field("calories", int(data.get("calories", 0)))\
            .field("intensity", float(data.get("intensity", 5)))\
            .field("feeling", data.get("feeling", "okay"))
        
        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
        logger.info(f"Workout logged successfully: {data.get('type')}")
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Error logging workout: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/recommendations/today')
def recommendations_today():
    """Get today's exercise recommendations"""
    # Use mock data for demo
    health_data = get_mock_health_today()
    
    # Use planner to generate recommendation
    rec = planner.get_recommendation(health_data)
    return jsonify(rec)


@app.route('/api/suunto/sync')
def suunto_sync():
    """Sync data from Suunto API"""
    logger.info("Starting Suunto sync")
    if not suunto.is_configured:
        logger.warning("Suunto not configured")
        return jsonify({"error": "Suunto not configured"}), 500
    
    try:
        data = suunto.get_daily_summaries(days=7)
        logger.info(f"Suunto returned {len(data) if data else 0} days of data")
        
        if write_api and data:
            for day in data:
                point = Point("daily_health")\
                    .tag("date", day.get("date"))\
                    .field("sleep_duration_hours", day.get("sleep_hours", 0))\
                    .field("hrv_avg", day.get("hrv", 0))\
                    .field("resting_hr", day.get("resting_hr", 0))\
                    .field("steps", day.get("steps", 0))
                
                write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
            logger.info(f"Synced {len(data)} days to InfluxDB")
        
        return jsonify({"synced": len(data) if data else 0, "data": data})
    except Exception as e:
        logger.error(f"Error syncing Suunto: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/strava/sync')
def strava_sync():
    """Sync workouts from Strava API"""
    logger.info("Starting Strava sync")
    days = request.args.get('days', 30, type=int)
    
    if not strava.is_configured:
        # Return mock data
        logger.info("Strava not configured, using demo mode")
        mock_workouts = MockStravaClient().get_activities(days)
        return jsonify({"synced": len(mock_workouts), "data": mock_workouts, "mode": "demo"})
    
    if not write_api:
        logger.warning("InfluxDB not configured for Strava sync")
        return jsonify({"error": "InfluxDB not configured"}), 500
    
    try:
        activities = strava.get_activities(days)
        
        if activities:
            from influxdb_client import Point
            for activity in activities:
                point = Point("workouts")\
                    .tag("type", activity.get("type", "Unknown"))\
                    .tag("date", activity.get("date", ""))\
                    .field("duration_minutes", float(activity.get("duration", 0)))\
                    .field("avg_hr", float(activity.get("avg_hr", 0)) if activity.get("avg_hr") else 0.0)\
                    .field("max_hr", float(activity.get("max_hr", 0)) if activity.get("max_hr") else 0.0)\
                    .field("calories", activity.get("calories", 0))\
                    .field("feeling", activity.get("feeling", "good"))
                
                write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
        
        logger.info(f"Synced {len(activities) if activities else 0} activities to InfluxDB")
        return jsonify({"synced": len(activities) if activities else 0, "data": activities})
    except Exception as e:
        logger.error(f"Error syncing Strava: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/pmc')
def pmc():
    """
    Get Performance Management Chart data (CTL, ATL, TSB)
    This calculates fitness, strain, and form from training load
    """
    logger.info("Fetching PMC data")
    # Always query enough days to get full range
    days = request.args.get('days', 90, type=int)
    
    # For weekly view (ATL), use current week (Mon-Sun)
    from datetime import datetime, timedelta
    today = datetime.now()
    # Find start of current week (Monday)
    week_start = today - timedelta(days=today.weekday())
    week_start_str = week_start.strftime("%Y-%m-%d")
    
    query_days = max(days + 42, 90)  # Warm-up window so 30-day chart is stable.
    
    # Read training load from InfluxDB workouts only (never from Strava)
    daily_loads = []
    
    if query_api:
        try:
            # Get suffer_score from workouts to calculate training load
            query = f'''
            from(bucket: "{INFLUXDB_BUCKET}")
              |> range(start: -{query_days}d)
              |> filter(fn: (r) => r._measurement == "workouts")
              |> filter(fn: (r) => r._field == "suffer_score")
              |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
            '''
            result = query_api.query_data_frame(query)
            if not result.empty and 'suffer_score' in result.columns:
                # Group by date and sum suffer_score (as proxy for training load)
                from collections import defaultdict
                by_date = defaultdict(float)
                for _, row in result.iterrows():
                    date = row.get('date', '')
                    load = row.get('suffer_score', 0)
                    if date and load:
                        by_date[date] += float(load)
                daily_loads = [{"date": d, "load": l} for d, l in sorted(by_date.items())]
                logger.info(f"Loaded {len(daily_loads)} days of training load from InfluxDB workouts")
        except Exception as e:
            logger.error(f"Error fetching PMC data from InfluxDB: {e}")
    
    # No mock data - return error if nothing from InfluxDB
    if not daily_loads:
        return jsonify({"error": "No training load data from InfluxDB"}), 404
    
    # Build continuous daily load series (fill missing days with zero load),
    # then compute CTL/ATL/TSB series for charting.
    from datetime import datetime, timedelta

    loads_map = {d["date"]: float(d.get("load", 0.0)) for d in daily_loads}
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=query_days - 1)

    full_series = []
    cur = start_date
    while cur <= end_date:
        ds = cur.isoformat()
        full_series.append({"date": ds, "load": loads_map.get(ds, 0.0)})
        cur += timedelta(days=1)

    pmc_series = calculate_pmc_series(full_series)

    pmc_recent = pmc_series[-days:]
    latest = pmc_recent[-1] if pmc_recent else {"ctl": 0, "atl": 0, "tsb": 0}
    status = get_status_description(latest["tsb"])
    
    return jsonify({
        "ctl": latest["ctl"],
        "atl": latest["atl"],
        "tsb": latest["tsb"],
        "status": status,
        "description": status,
        "days_tracked": len(full_series),
        "chart": {
            "dates": [d["date"] for d in pmc_recent],
            "ctl": [d["ctl"] for d in pmc_recent],
            "atl": [d["atl"] for d in pmc_recent],
            "tsb": [d["tsb"] for d in pmc_recent],
        }
    })


@app.route('/api/trends')
def trends():
    """Get weekly/monthly trend analysis"""
    days = request.args.get('days', 7, type=int)
    
    # Get history data
    history = health_history().get_json()
    
    if "error" in history:
        return jsonify({"error": "No data"})
    
    # Calculate trends
    hrv_trend = "↑" if history["hrv"][-1] > history["hrv"][0] else "↓"
    hr_trend = "↓" if history["resting_hr"][-1] < history["resting_hr"][0] else "↑"
    
    # Weekly averages
    weekly_hrv = sum(history["hrv"][-7:]) / 7 if len(history["hrv"]) >= 7 else 0
    weekly_sleep = sum(history["sleep"][-7:]) / 7 if len(history["sleep"]) >= 7 else 0
    
    return jsonify({
        "hrv_trend": hrv_trend,
        "hr_trend": hr_trend,
        "weekly_avg_hrv": round(weekly_hrv, 1),
        "weekly_avg_sleep": round(weekly_sleep, 1),
        "training_status": "BUILD" if weekly_hrv > 35 else "RECOVERY"
    })


if __name__ == '__main__':
    logger.info(f"Starting Health Dashboard on port {FLASK_PORT}")
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=FLASK_DEBUG)
