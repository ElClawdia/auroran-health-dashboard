#!/usr/bin/env python3
"""
Health Dashboard - Flask Web Server
Auroran Health Command Center 🦞

Run: python app.py
Access: http://localhost:5000
"""

import os
import json
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request
from influxdb_client import InfluxDBClient, Point, WriteOptions
from influxdb_client.client.write_api import SYNCHRONOUS
import pandas as pd

# Import our modules
from suunto_client import SuuntoClient
from planner import ExercisePlanner

app = Flask(__name__)

# Configuration
INFLUXDB_URL = os.getenv('INFLUXDB_URL', 'http://localhost:8086')
INFLUXDB_TOKEN = os.getenv('INFLUXDB_TOKEN', '')
INFLUXDB_ORG = os.getenv('INFLUXDB_ORG', 'tapio')
INFLUXDB_BUCKET = os.getenv('INFLUXDB_BUCKET', 'health')

SUUNTO_CLIENT_ID = os.getenv('SUUNTO_CLIENT_ID', '')
SUUNTO_CLIENT_SECRET = os.getenv('SUUNTO_CLIENT_SECRET', '')

# Initialize InfluxDB client
if INFLUXDB_TOKEN:
    influx_client = InfluxDBClient(
        url=INFLUXDB_URL,
        token=INFLUXDB_TOKEN,
        org=INFLUXDB_ORG
    )
    write_api = influx_client.write_api(write_options=SYNCHRONOUS)
    query_api = influx_client.query_api()
else:
    influx_client = None
    write_api = None
    query_api = None

# Initialize modules
suunto = SuuntoClient(SUUNTO_CLIENT_ID, SUUNTO_CLIENT_SECRET)
planner = ExercisePlanner()


@app.route('/')
def index():
    """Main dashboard page"""
    return render_template('index.html')


@app.route('/api/health/today')
def health_today():
    """Get today's health metrics"""
    if not query_api:
        return jsonify({"error": "InfluxDB not configured"}), 500
    
    try:
        query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: -1d)
          |> filter(fn: (r) => r._measurement == "daily_health")
        '''
        result = query_api.query_data_frame(query)
        
        if result.empty:
            # Return mock data for demo
            return jsonify({
                "date": datetime.now().strftime("%Y-%m-%d"),
                "sleep_hours": 7.45,
                "hrv": 42,
                "resting_hr": 58,
                "steps": 8500,
                "recovery_score": 85,
                "training_load": 1.2,
                "trend": {
                    "sleep": "+12m",
                    "hrv": "+5ms ▲",
                    "resting_hr": "-2bpm ▼"
                }
            })
        
        # Process actual data
        latest = result.iloc[-1] if len(result) > 0 else {}
        
        return jsonify({
            "date": latest.get("date", datetime.now().strftime("%Y-%m-%d")),
            "sleep_hours": float(latest.get("sleep_duration_hours", 7.5)),
            "hrv": float(latest.get("hrv_avg", 40)),
            "resting_hr": float(latest.get("resting_hr", 60)),
            "steps": int(latest.get("steps", 0)),
            "recovery_score": int(latest.get("recovery_score", 70)),
            "training_load": float(latest.get("training_load", 1.0))
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/health/history')
def health_history():
    """Get historical health data"""
    days = request.args.get('days', 30, type=int)
    
    if not query_api:
        return jsonify({"error": "InfluxDB not configured"}), 500
    
    try:
        query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: -{days}d)
          |> filter(fn: (r) => r._measurement == "daily_health")
          |> pivot(rowKey: "_time", columnKey: "_field", valueColumn: "_value")
        '''
        result = query_api.query_data_frame(query)
        
        if result.empty:
            # Return mock trend data
            dates = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") 
                     for i in range(days-1, -1, -1)]
            return jsonify({
                "dates": dates,
                "hrv": [30 + i + (i % 7) for i in range(days)],
                "resting_hr": [65 - i//3 for i in range(days)],
                "sleep": [7 + (i % 5) * 0.2 for i in range(days)],
                "recovery": [60 + i + (i % 10) for i in range(days)]
            })
        
        # Process actual data
        df = result.sort_values('date')
        
        return jsonify({
            "dates": df["date"].tolist(),
            "hrv": df["hrv_avg"].tolist() if "hrv_avg" in df else [],
            "resting_hr": df["resting_hr"].tolist() if "resting_hr" in df else [],
            "sleep": df["sleep_duration_hours"].tolist() if "sleep_duration_hours" in df else [],
            "recovery": df["recovery_score"].tolist() if "recovery_score" in df else []
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/workouts', methods=['GET', 'POST'])
def workouts():
    """Get or log workouts"""
    if request.method == 'GET':
        if not query_api:
            return jsonify({"error": "InfluxDB not configured"}), 500
        
        try:
            query = f'''
            from(bucket: "{INFLUXDB_BUCKET}")
              |> range(start: -30d)
              |> filter(fn: (r) => r._measurement == "workouts")
              |> pivot(rowKey: "_time", columnKey: "_field", valueColumn: "_value")
            '''
            result = query_api.query_data_frame(query)
            
            if result.empty:
                # Mock data
                return jsonify([
                    {"date": "2026-02-15", "type": "Running", "duration": 28, 
                     "avg_hr": 145, "feeling": "great"},
                    {"date": "2026-02-14", "type": "Strength", "duration": 45, 
                     "avg_hr": 110, "feeling": "good"},
                    {"date": "2026-02-13", "type": "Rest", "duration": 0, 
                     "avg_hr": 65, "feeling": "great"},
                ])
            
            return jsonify(result.to_dict(orient='records'))
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    # POST - Log new workout
    if not write_api:
        return jsonify({"error": "InfluxDB not configured"}), 500
    
    data = request.json
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
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/recommendations/today')
def recommendations_today():
    """Get today's exercise recommendations"""
    # Get today's health data
    health_data = health_today().get_json()
    
    if "error" in health_data:
        # Mock recommendation
        return jsonify({
            "recovery": 85,
            "recommendation": "HIGH",
            "message": "Great recovery! Push hard today.",
            "workout": {
                "type": "Running",
                "duration": 45,
                "zone": "2-3",
                "pace": "5:30-6:00 /km"
            },
            "alternatives": [
                {"type": "Intervals", "duration": 30, "intensity": "High"},
                {"type": "Strength", "duration": 45, "intensity": "Moderate"}
            ]
        })
    
    # Use planner to generate recommendation
    rec = planner.get_recommendation(health_data)
    return jsonify(rec)


@app.route('/api/suunto/sync')
def suunto_sync():
    """Sync data from Suunto API"""
    if not suunto.is_configured:
        return jsonify({"error": "Suunto not configured"}), 500
    
    try:
        data = suunto.get_daily_summaries(days=7)
        
        if write_api and data:
            for day in data:
                point = Point("daily_health")\
                    .tag("date", day.get("date"))\
                    .field("sleep_duration_hours", day.get("sleep_hours", 0))\
                    .field("hrv_avg", day.get("hrv", 0))\
                    .field("resting_hr", day.get("resting_hr", 0))\
                    .field("steps", day.get("steps", 0))
                
                write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
        
        return jsonify({"synced": len(data) if data else 0, "data": data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
