#!/usr/bin/env python3
"""
Health Dashboard - Flask Web Server
Auroran Health Command Center 🦞

Run: python app.py
Access: http://localhost:5000
"""

import os
import sys
import secrets
import logging
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request, session, redirect, url_for
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
from training_load import calculate_training_load, calculate_ctl_atl_tsb, calculate_pmc_series, get_status_description, reload_params
from auth import login_required, authenticate, get_current_user, update_user, get_user
from formula_learning import load_params, run_learning_cycle

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', secrets.token_hex(32))

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

# Simple in-memory cache for workouts and PMC data
_workout_cache = {"data": None, "expires": None}
_pmc_cache = {"data": None, "expires": None}
CACHE_TTL_SECONDS = 60  # 1 minute - balance between freshness and performance

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
@login_required
def index():
    """Main dashboard page"""
    user = get_current_user()
    return render_template('index.html', user=user)


@app.route('/login', methods=['GET', 'POST'])
def login_page():
    """Login page"""
    if request.method == 'POST':
        data = request.get_json() if request.is_json else request.form
        username = data.get('username', '')
        password = data.get('password', '')
        
        user = authenticate(username, password)
        if user:
            session['user'] = username
            session.permanent = True
            app.permanent_session_lifetime = timedelta(days=30)
            if request.is_json:
                return jsonify({"success": True, "user": user["full_name"]})
            return redirect(url_for('index'))
        
        if request.is_json:
            return jsonify({"error": "Invalid username or password"}), 401
        return render_template('login.html', error="Invalid username or password")
    
    return render_template('login.html')


@app.route('/logout')
def logout():
    """Logout and clear session"""
    session.pop('user', None)
    return redirect(url_for('login_page'))


@app.route('/register')
def register_page():
    """Registration page (currently disabled)"""
    return render_template('register.html')


@app.route('/account', methods=['GET', 'POST'])
@login_required
def account_page():
    """Account settings page"""
    user = get_current_user()
    
    if request.method == 'POST':
        data = request.get_json() if request.is_json else request.form
        updates = {}
        
        if data.get('full_name'):
            updates['full_name'] = data['full_name']
        if data.get('email'):
            updates['email'] = data['email']
        if data.get('new_password') and data.get('current_password'):
            # Verify current password first
            if not authenticate(session['user'], data['current_password']):
                if request.is_json:
                    return jsonify({"error": "Current password is incorrect"}), 400
                return render_template('account.html', user=user, error="Current password is incorrect")
            updates['new_password'] = data['new_password']
        
        if updates:
            update_user(session['user'], updates)
            user = get_current_user()  # Refresh user data
            if request.is_json:
                return jsonify({"success": True})
        
        if request.is_json:
            return jsonify({"success": True})
        return render_template('account.html', user=user, success="Account updated successfully")
    
    return render_template('account.html', user=user)


@app.route('/api/user')
@login_required
def api_user():
    """Get current user info"""
    user = get_current_user()
    if user:
        return jsonify({
            "username": user["username"],
            "full_name": user["full_name"],
            "email": user["email"]
        })
    return jsonify({"error": "Not logged in"}), 401


@app.route('/api/health/today')
@login_required
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
@login_required
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


def _fetch_workouts_from_influx():
    """Fetch workouts from InfluxDB with manual pivot (faster than Flux pivot)"""
    from collections import defaultdict
    
    cutoff = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')
    
    # Try workout_cache first (optimized, fewer records)
    # Fall back to workouts measurement if cache doesn't exist
    for measurement in ["workout_cache", "workouts"]:
        query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: -365d)
          |> filter(fn: (r) => r._measurement == "{measurement}")
          |> filter(fn: (r) => r.date >= "{cutoff}")
        '''
        
        tables = query_api.query_stream(query)
        
        # Manual pivot in Python using _time as unique key
        workouts = defaultdict(dict)
        for record in tables:
            key = str(record.get_time())
            field = record.get_field()
            value = record.get_value()
            workouts[key][field] = value
            workouts[key]['date'] = record.values.get('date', '')
            workouts[key]['type'] = record.values.get('type', '')
        
        if workouts:
            break
    
    # Sort by date and start_time descending
    result = sorted(
        workouts.values(), 
        key=lambda x: (x.get('date', ''), x.get('start_time', '')), 
        reverse=True
    )
    return result


@app.route('/api/workouts', methods=['GET', 'POST'])
@login_required
def workouts():
    """Get or log workouts"""
    if request.method == 'GET':
        if not query_api:
            logger.info("Using mock workouts (no InfluxDB)")
            return jsonify({"error": "No workouts from InfluxDB"}), 404
        
        try:
            # Check cache first
            now = datetime.now()
            if _workout_cache["data"] and _workout_cache["expires"] and now < _workout_cache["expires"]:
                logger.info("Returning cached workouts")
                return jsonify(_workout_cache["data"])
            
            logger.info("Fetching workouts from InfluxDB")
            records = _fetch_workouts_from_influx()
            
            if not records:
                return jsonify({"error": "No workouts from InfluxDB"}), 404
            
            # Update cache
            _workout_cache["data"] = records
            _workout_cache["expires"] = now + timedelta(seconds=CACHE_TTL_SECONDS)
            
            return jsonify(records)
        except Exception as e:
            logger.error(f"Error fetching workouts: {e}")
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


@app.route('/api/manual-values', methods=['GET', 'POST', 'DELETE'])
@login_required
def manual_values():
    """Get, set, or delete manual override values"""
    if request.method == 'GET':
        date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
        
        if not query_api:
            return jsonify({})
        
        try:
            query = f'''
            from(bucket: "{INFLUXDB_BUCKET}")
              |> range(start: -30d)
              |> filter(fn: (r) => r._measurement == "manual_values")
              |> filter(fn: (r) => r.date == "{date}")
              |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
            '''
            result = query_api.query_data_frame(query)
            
            if isinstance(result, list):
                if len(result) == 0:
                    return jsonify({})
                result = pd.concat(result, ignore_index=True)
            
            if result.empty:
                return jsonify({})
            
            # Get the latest values for each metric
            metrics = ['sleep', 'hrv', 'resting_hr', 'steps', 'weight', 'ctl', 'atl', 'tsb']
            values = {}
            for metric in metrics:
                if metric in result.columns:
                    val = result[metric].dropna()
                    if len(val) > 0:
                        values[metric] = float(val.iloc[-1])
                    else:
                        values[metric] = None
                else:
                    values[metric] = None
            
            return jsonify(values)
        except Exception as e:
            logger.error(f"Error fetching manual values: {e}")
            return jsonify({})
    
    elif request.method == 'POST':
        if not write_api:
            return jsonify({"error": "InfluxDB not configured"}), 500
        
        data = request.json
        metric = data.get('metric')
        value = data.get('value')
        date = data.get('date', datetime.now().strftime('%Y-%m-%d'))
        
        if not metric or value is None:
            return jsonify({"error": "Missing metric or value"}), 400
        
        try:
            point = Point("manual_values")\
                .tag("date", date)\
                .field(metric, float(value))
            
            write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
            logger.info(f"Manual value saved: {metric}={value} for {date}")
            return jsonify({"success": True})
        except Exception as e:
            logger.error(f"Error saving manual value: {e}")
            return jsonify({"error": str(e)}), 500
    
    elif request.method == 'DELETE':
        if not write_api:
            return jsonify({"error": "InfluxDB not configured"}), 500
        
        data = request.json
        metric = data.get('metric')
        date = data.get('date', datetime.now().strftime('%Y-%m-%d'))
        
        if not metric:
            return jsonify({"error": "Missing metric"}), 400
        
        try:
            # Write a null/sentinel value to indicate deletion
            # InfluxDB doesn't support true deletion easily, so we use a marker
            point = Point("manual_values")\
                .tag("date", date)\
                .tag("deleted", "true")\
                .field(metric, 0.0)
            
            write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
            logger.info(f"Manual value cleared: {metric} for {date}")
            return jsonify({"success": True})
        except Exception as e:
            logger.error(f"Error clearing manual value: {e}")
            return jsonify({"error": str(e)}), 500


@app.route('/api/recommendations/today')
@login_required
def recommendations_today():
    """Get today's exercise recommendations"""
    # Use mock data for demo
    health_data = get_mock_health_today()
    
    # Use planner to generate recommendation
    rec = planner.get_recommendation(health_data)
    return jsonify(rec)


@app.route('/api/calories')
@login_required
def calories():
    """Get calories burned from workouts for today"""
    date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
    
    if not query_api:
        return jsonify({"calories": 0})
    
    try:
        # Query calories from workouts for the specified date
        query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: -30d)
          |> filter(fn: (r) => r._measurement == "workout_cache" or r._measurement == "workouts")
          |> filter(fn: (r) => r.date == "{date}")
          |> filter(fn: (r) => r._field == "calories")
          |> sum()
        '''
        result = query_api.query(query)
        
        total_calories = 0
        for table in result:
            for record in table.records:
                val = record.get_value()
                if val:
                    total_calories += float(val)
        
        return jsonify({"calories": int(total_calories), "date": date})
    except Exception as e:
        logger.error(f"Error fetching calories: {e}")
        return jsonify({"calories": 0, "error": str(e)})


@app.route('/api/weight', methods=['GET', 'POST'])
@login_required
def weight():
    """Get or set weight"""
    if request.method == 'GET':
        date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
        
        if not query_api:
            return jsonify({"weight": None})
        
        try:
            # First check manual values
            query = f'''
            from(bucket: "{INFLUXDB_BUCKET}")
              |> range(start: -30d)
              |> filter(fn: (r) => r._measurement == "manual_values")
              |> filter(fn: (r) => r._field == "weight")
              |> filter(fn: (r) => r.deleted != "true")
              |> last()
            '''
            result = query_api.query(query)
            
            for table in result:
                for record in table.records:
                    val = record.get_value()
                    if val:
                        return jsonify({"weight": float(val), "source": "manual"})
            
            # Fall back to daily_health if available
            query = f'''
            from(bucket: "{INFLUXDB_BUCKET}")
              |> range(start: -30d)
              |> filter(fn: (r) => r._measurement == "daily_health")
              |> filter(fn: (r) => r._field == "weight")
              |> last()
            '''
            result = query_api.query(query)
            
            for table in result:
                for record in table.records:
                    val = record.get_value()
                    if val:
                        return jsonify({"weight": float(val), "source": "auto"})
            
            return jsonify({"weight": None})
        except Exception as e:
            logger.error(f"Error fetching weight: {e}")
            return jsonify({"weight": None, "error": str(e)})
    
    elif request.method == 'POST':
        if not write_api:
            return jsonify({"error": "InfluxDB not configured"}), 500
        
        data = request.json
        weight_val = data.get('weight')
        date = data.get('date', datetime.now().strftime('%Y-%m-%d'))
        
        if weight_val is None:
            return jsonify({"error": "Missing weight value"}), 400
        
        try:
            point = Point("manual_values")\
                .tag("date", date)\
                .field("weight", float(weight_val))
            
            write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
            logger.info(f"Weight saved: {weight_val} kg for {date}")
            return jsonify({"success": True})
        except Exception as e:
            logger.error(f"Error saving weight: {e}")
            return jsonify({"error": str(e)}), 500


@app.route('/api/formula/params')
@login_required
def formula_params():
    """Get current formula parameters"""
    params = load_params()
    return jsonify(params)


@app.route('/api/formula/learn', methods=['POST'])
@login_required
def formula_learn():
    """
    Trigger a learning cycle to optimize formula parameters
    based on manually entered CTL/ATL values.
    """
    if not query_api:
        return jsonify({"error": "InfluxDB not configured"}), 500
    
    try:
        # Fetch daily loads from InfluxDB
        daily_loads = _fetch_daily_loads_from_influx(query_days=365)
        
        if not daily_loads:
            return jsonify({"error": "No training load data available"}), 400
        
        # Run learning cycle
        new_params = run_learning_cycle(query_api, INFLUXDB_BUCKET, daily_loads)
        
        # Reload parameters in training_load module
        reload_params()
        
        # Clear PMC cache to use new parameters
        _pmc_cache["data"] = None
        _pmc_cache["expires"] = None
        
        logger.info(f"Formula learning completed: {new_params}")
        return jsonify({
            "success": True,
            "params": new_params,
            "message": "Parameters optimized based on manual reference values"
        })
    except Exception as e:
        logger.error(f"Error in formula learning: {e}")
        return jsonify({"error": str(e)}), 500


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


def _fetch_daily_loads_from_influx(query_days=365):
    """Fetch daily training loads from InfluxDB (optimized, no pivot)"""
    from collections import defaultdict
    
    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -{query_days}d)
      |> filter(fn: (r) => r._measurement == "workouts")
      |> filter(fn: (r) => r._field == "suffer_score")
    '''
    
    tables = query_api.query_stream(query)
    by_date = defaultdict(float)
    
    for record in tables:
        date = record.values.get('date', '')
        load = record.get_value() or 0
        if date:
            by_date[date] += float(load)
    
    return [{"date": d, "load": l} for d, l in sorted(by_date.items())]


@app.route('/api/pmc')
@login_required
def pmc():
    """
    Get Performance Management Chart data (CTL, ATL, TSB)
    This calculates fitness, strain, and form from training load
    """
    days = request.args.get('days', 90, type=int)
    query_days = max(days + 42, 365)  # Full year for stable CTL/ATL
    
    # Check cache first
    now = datetime.now()
    if _pmc_cache["data"] and _pmc_cache["expires"] and now < _pmc_cache["expires"]:
        logger.info("Returning cached PMC data")
        cached = _pmc_cache["data"]
        # Return cached data but slice to requested days
        pmc_recent = cached["pmc_series"][-days:]
        latest = pmc_recent[-1] if pmc_recent else {"ctl": 0, "atl": 0, "tsb": 0}
        return jsonify({
            "ctl": latest["ctl"],
            "atl": latest["atl"],
            "tsb": latest["tsb"],
            "status": get_status_description(latest["tsb"]),
            "chart": {
                "dates": [d["date"] for d in pmc_recent],
                "ctl": [d["ctl"] for d in pmc_recent],
                "atl": [d["atl"] for d in pmc_recent],
                "tsb": [d["tsb"] for d in pmc_recent],
            }
        })
    
    logger.info("Fetching PMC data from InfluxDB")
    daily_loads = []
    
    if query_api:
        try:
            daily_loads = _fetch_daily_loads_from_influx(query_days)
            logger.info(f"Loaded {len(daily_loads)} days of training load from InfluxDB")
        except Exception as e:
            logger.error(f"Error fetching PMC data from InfluxDB: {e}")
    
    # No mock data - return error if nothing from InfluxDB
    if not daily_loads:
        return jsonify({"error": "No training load data from InfluxDB"}), 404
    
    # Build continuous daily load series (fill missing days with zero load),
    # then compute CTL/ATL/TSB series for charting.
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
    
    # Update cache
    _pmc_cache["data"] = {"pmc_series": pmc_series}
    _pmc_cache["expires"] = now + timedelta(seconds=CACHE_TTL_SECONDS)

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
