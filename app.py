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
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from auth import login_required, authenticate, get_current_user, update_user, get_user, hash_password
from formula_learning import load_params, run_learning_cycle
from email_service import generate_reset_token, consume_reset_token, send_password_reset_email

app = Flask(__name__)

# Configuration
from config import INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, INFLUXDB_BUCKET
from config import SUUNTO_CLIENT_ID, SUUNTO_CLIENT_SECRET
from config import STRAVA_ACCESS_TOKEN, STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN
from config import FLASK_HOST, FLASK_PORT, FLASK_DEBUG, FLASK_SECRET_KEY

# Use persistent secret key - generate and save if not configured
def get_or_create_secret_key():
    """Get secret key from config or generate and persist one"""
    if FLASK_SECRET_KEY:
        return FLASK_SECRET_KEY
    
    # Try to load from file
    secret_key_file = Path(__file__).parent / ".flask_secret_key"
    if secret_key_file.exists():
        return secret_key_file.read_text().strip()
    
    # Generate new key and save it
    new_key = secrets.token_hex(32)
    secret_key_file.write_text(new_key)
    logger.info("Generated new Flask secret key")
    return new_key

app.secret_key = get_or_create_secret_key()

# Initialize InfluxDB client with fallback
influx_client = None
write_api = None
query_api = None

if INFLUXDB_TOKEN:
    try:
        influx_client = InfluxDBClient(
            url=INFLUXDB_URL,
            token=INFLUXDB_TOKEN,
            org=INFLUXDB_ORG,
            timeout=60_000,  # 60s for large workout history queries
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


_workout_index_preloaded = False

# Simple in-memory cache for workouts, PMC, weight, and dashboard
_workout_cache = {"data": None, "expires": None}
_pmc_cache = {"data": None, "expires": None}
_weight_cache: dict[str, tuple[dict, datetime]] = {}  # (date -> (response, expires))
_dashboard_cache: dict[str, tuple[dict, datetime]] = {}  # (date -> (response, expires))
CACHE_TTL_SECONDS = 30  # 30 seconds - quick refresh after syncing
WORKOUT_INDEX_TTL_SECONDS = 600  # 10 minutes
WORKOUT_INDEX_RANGE_DAYS = 42
_workout_index: dict[str, object] = {
    "data": None,        # list of workouts
    "loading": False,
    "loaded_at": None,
    "loading_started_at": None,
}
_workout_index_lock = threading.Lock()

# Dashboard lookback windows (keep small for speed)
WORKOUT_LOOKBACK_DAYS = 42
HEALTH_LOOKBACK_DAYS = 42
PMC_MIN_LOOKBACK_DAYS = 120
WEIGHT_LOOKBACK_DAYS = 42  # Never load more than 42 days at a time

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


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    """Forgot password - send reset link to email"""
    if request.method == 'POST':
        data = request.get_json() if request.is_json else request.form
        email = data.get('email', '').strip().lower()
        
        if not email:
            return jsonify({"error": "Email is required"}), 400
        
        # Find user by email
        from auth import load_users
        users = load_users()
        username = None
        user = None
        for uname, udata in users.items():
            if udata.get('email', '').lower() == email:
                username = uname
                user = udata
                break
        
        if not user:
            # Don't reveal if email exists or not for security
            return jsonify({
                "success": True,
                "message": "If an account with that email exists, a password reset link has been sent."
            })
        
        # Generate reset token and send email
        token = generate_reset_token(username)
        reset_link = url_for('set_new_password', token=token, _external=True)
        
        email_sent = send_password_reset_email(
            to_email=user['email'],
            username=user['full_name'],
            reset_link=reset_link
        )
        
        if email_sent:
            return jsonify({
                "success": True,
                "message": "If an account with that email exists, a password reset link has been sent."
            })
        else:
            return jsonify({
                "success": True,
                "message": "If an account with that email exists, a password reset link has been sent.",
                "verification_link": reset_link
            })
    
    return render_template('forgot_password.html')


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
        
        if updates:
            update_user(session['user'], updates)
            user = get_current_user()  # Refresh user data
            if request.is_json:
                return jsonify({"success": True})
        
        if request.is_json:
            return jsonify({"success": True})
        return render_template('account.html', user=user, success="Account updated successfully")
    
    return render_template('account.html', user=user)


@app.route('/account/change-password', methods=['POST'])
@login_required
def request_password_change():
    """Request password change - sends verification email with link to set new password"""
    user = get_current_user()
    
    # Generate reset token (no password yet - user will set it after clicking link)
    token = generate_reset_token(session['user'])
    
    # Build verification link
    reset_link = url_for('set_new_password', token=token, _external=True)
    
    # Send email
    email_sent = send_password_reset_email(
        to_email=user['email'],
        username=user['full_name'],
        reset_link=reset_link
    )
    
    if email_sent:
        return jsonify({
            "success": True,
            "message": f"Password reset link sent to {user['email']}. Please check your inbox."
        })
    else:
        return jsonify({
            "success": True,
            "message": "Email service not configured. For testing, use this link:",
            "verification_link": reset_link
        })


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def set_new_password(token):
    """Show form to set new password after clicking email link"""
    from email_service import verify_reset_token
    
    # Verify token is valid (don't consume yet)
    token_data = verify_reset_token(token)
    
    if not token_data:
        return render_template('password_verified.html', 
                             success=False, 
                             message="Invalid or expired password reset link.")
    
    if request.method == 'GET':
        # Show the password reset form
        return render_template('set_password.html', token=token)
    
    # POST - process new password
    new_password = request.form.get('new_password')
    confirm_password = request.form.get('confirm_password')
    
    if not new_password or not confirm_password:
        return render_template('set_password.html', token=token, 
                             error="Please fill in both password fields.")
    
    if new_password != confirm_password:
        return render_template('set_password.html', token=token,
                             error="Passwords do not match.")
    
    if len(new_password) < 8:
        return render_template('set_password.html', token=token,
                             error="Password must be at least 8 characters.")
    
    # Now consume the token
    from email_service import consume_reset_token
    token_data = consume_reset_token(token)
    
    if not token_data:
        return render_template('password_verified.html', 
                             success=False, 
                             message="Invalid or expired password reset link.")
    
    # Hash and save the new password
    from auth import load_users, save_users
    password_hash, salt = hash_password(new_password)
    users = load_users()
    username = token_data['username']
    
    if username in users:
        users[username]['password_hash'] = password_hash
        users[username]['salt'] = salt
        save_users(users)
        
        return render_template('password_verified.html',
                             success=True,
                             message="Your password has been changed successfully. You can now log in with your new password.")
    
    return render_template('password_verified.html',
                         success=False,
                         message="User not found.")


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


@app.route('/api/cache/clear', methods=['POST'])
@login_required
def clear_cache():
    """Clear all in-memory caches to force fresh data fetch"""
    global _workout_cache, _pmc_cache, _weight_cache, _dashboard_cache, _workout_index
    _workout_cache = {"data": None, "expires": None}
    _pmc_cache = {"data": None, "expires": None}
    _weight_cache.clear()
    _dashboard_cache.clear()
    with _workout_index_lock:
        _workout_index = {"data": None, "loading": False, "loaded_at": None, "loading_started_at": None}
    logger.info("Cache cleared by user")
    return jsonify({"success": True, "message": "Cache cleared"})


@app.route('/api/health/today')
@login_required
def health_today():
    """Get health metrics for a specific date (default: today)"""
    target_date = request.args.get('date', datetime.now().strftime("%Y-%m-%d"))
    logger.debug(f"Fetching health metrics for {target_date}")
    if not query_api:
        return jsonify({"error": "InfluxDB not configured"}), 500
    
    try:
        target_dt = datetime.strptime(target_date, "%Y-%m-%d")
        start_dt = target_dt - timedelta(days=7)
        stop_dt = target_dt + timedelta(days=1)
        query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: {start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {stop_dt.strftime("%Y-%m-%dT00:00:00Z")})
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

        # Build one row per date
        if "date" not in result.columns:
            return jsonify({"error": "No date field in daily_health data"}), 404

        df = result.copy()
        numeric_cols = [
            c for c in ["sleep_duration_hours", "hrv_avg", "resting_hr", "steps", "recovery_score", "training_load", "weight"]
            if c in df.columns
        ]
        for c in numeric_cols:
            df[c] = pd.to_numeric(df[c], errors="coerce")

        if numeric_cols:
            df = df.groupby("date", as_index=False)[numeric_cols].mean()
        else:
            df = df[["date"]].drop_duplicates()

        df = df.sort_values("date")
        
        # Try to get data for the target date, fall back to latest
        target_row = df[df['date'] == target_date]
        if not target_row.empty:
            row = target_row.iloc[0]
        else:
            row = df.iloc[-1]

        def clean_number(val):
            return None if pd.isna(val) else float(val)

        def get_value(col):
            if col not in row.index:
                return None
            return clean_number(row[col])

        steps_val = get_value("steps")
        steps_clean = None if steps_val is None else int(float(steps_val))
        weight_val = get_value("weight")

        out = {
            "date": row.get("date", target_date),
            "sleep_hours": get_value("sleep_duration_hours"),
            "hrv": get_value("hrv_avg"),
            "resting_hr": get_value("resting_hr"),
            "steps": steps_clean,
            "recovery_score": get_value("recovery_score"),
            "training_load": get_value("training_load")
        }
        if weight_val is not None:
            out["weight"] = weight_val
        return jsonify(out)
    except Exception as e:
        logger.error(f"Error fetching health for {target_date}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/health/history')
@login_required
def health_history():
    """Get historical health data"""
    days = request.args.get('days', 30, type=int)
    end_date = request.args.get('end_date', datetime.now().strftime("%Y-%m-%d"))
    logger.debug(f"Fetching health history: {days} days ending {end_date}")
    
    if not query_api:
        # Return mock data for demo
        logger.info("Using mock data for history (no InfluxDB)")
        return jsonify({"error": "No data from InfluxDB"}), 404
    
    try:
        # Calculate start date based on end_date and days
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        start_dt = end_dt - timedelta(days=days + 7)  # Small buffer for data availability
        
        query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: {start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {(end_dt + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")})
          |> filter(fn: (r) => r._measurement == "daily_health")
          |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
        '''
        result = query_api.query_data_frame(query)

        # Generate date range for the requested period
        dates_list = [(end_dt - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days-1, -1, -1)]
        
        # Check if we have daily_health data
        has_daily_health = False
        if isinstance(result, list):
            if len(result) > 0:
                result = pd.concat(result, ignore_index=True)
                has_daily_health = not result.empty
        elif not result.empty:
            has_daily_health = True
        
        if has_daily_health and "date" in result.columns:
            # Process actual data from daily_health
            df = result.copy()
            numeric_cols = [
                c for c in ["hrv_avg", "resting_hr", "sleep_duration_hours", "recovery_score", "steps", "weight"]
                if c in df.columns
            ]
            if numeric_cols:
                for c in numeric_cols:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
                df = df.groupby("date", as_index=False)[numeric_cols].mean()
            else:
                df = df[["date"]].drop_duplicates()
            df = df.sort_values("date").tail(days)
            dates_list = df["date"].tolist()
        else:
            # No daily_health data - create empty dataframe with just dates
            df = pd.DataFrame({"date": dates_list})

        def clean_series(series, digits=2):
            return [None if pd.isna(v) else round(float(v), digits) for v in series.tolist()]

        # Also fetch manual values history (weight, hrv, sleep, etc.)
        manual_data = {field: {} for field in ['weight', 'hrv', 'sleep', 'resting_hr', 'steps']}
        try:
            manual_query = f'''
            from(bucket: "{INFLUXDB_BUCKET}")
              |> range(start: -{days}d)
              |> filter(fn: (r) => r._measurement == "manual_values")
              |> filter(fn: (r) => r._field == "weight" or r._field == "hrv" or r._field == "sleep" or r._field == "resting_hr" or r._field == "steps")
              |> filter(fn: (r) => r.deleted != "true")
            '''
            manual_result = query_api.query(manual_query)
            for table in manual_result:
                for record in table.records:
                    date = record.values.get('date', '')
                    field = record.get_field()
                    if date and field in manual_data:
                        manual_data[field][date] = float(record.get_value())
        except Exception as e:
            logger.warning(f"Could not fetch manual values history: {e}")

        # Helper to merge automated and manual data, preferring manual values
        def merge_with_manual(auto_series, manual_dict, dates_list):
            result = []
            for i, date in enumerate(dates_list):
                manual_val = manual_dict.get(date)
                if manual_val is not None:
                    result.append(manual_val)
                elif i < len(auto_series):
                    result.append(auto_series[i])
                else:
                    result.append(None)
            return result

        dates_list = df["date"].tolist()
        hrv_auto = clean_series(df["hrv_avg"], 2) if "hrv_avg" in df else [None] * len(dates_list)
        rhr_auto = clean_series(df["resting_hr"], 2) if "resting_hr" in df else [None] * len(dates_list)
        sleep_auto = clean_series(df["sleep_duration_hours"], 2) if "sleep_duration_hours" in df else [None] * len(dates_list)
        steps_auto = clean_series(df["steps"], 0) if "steps" in df else [None] * len(dates_list)
        weight_auto = clean_series(df["weight"], 2) if "weight" in df else [None] * len(dates_list)

        return jsonify({
            "dates": dates_list,
            "hrv": merge_with_manual(hrv_auto, manual_data['hrv'], dates_list),
            "resting_hr": merge_with_manual(rhr_auto, manual_data['resting_hr'], dates_list),
            "sleep": merge_with_manual(sleep_auto, manual_data['sleep'], dates_list),
            "recovery": clean_series(df["recovery_score"], 1) if "recovery_score" in df else [],
            "steps": merge_with_manual(steps_auto, manual_data['steps'], dates_list),
            "weight": merge_with_manual(weight_auto, manual_data['weight'], dates_list)
        })
    except Exception as e:
        logger.error(f"Error fetching health history: {e}")
        return jsonify({"error": str(e)}), 500


def _fetch_workouts_from_influx(before_date: str | None = None):
    """Fetch workouts from InfluxDB. Filter by date tag (not _time - points use write time)."""
    from collections import defaultdict

    now = datetime.now()
    days_back = WORKOUT_LOOKBACK_DAYS
    cutoff = (now - timedelta(days=days_back)).strftime('%Y-%m-%d')
    # Cap range for speed: only load last 42 days
    if before_date:
        try:
            target = datetime.strptime(before_date, "%Y-%m-%d").date()
            days_ago = (now.date() - target).days
            range_days = days_back
        except ValueError:
            range_days = days_back
    else:
        range_days = days_back
    date_filter = f'|> filter(fn: (r) => r.date >= "{cutoff}")'
    if before_date:
        date_filter = f'|> filter(fn: (r) => r.date <= "{before_date}")'

    # Try workout_cache first (optimized, fewer records)
    # Fall back to workouts measurement if cache doesn't exist
    for measurement in ["workout_cache", "workouts"]:
        query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: -{range_days}d)
          |> filter(fn: (r) => r._measurement == "{measurement}")
          {date_filter}
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
    return _dedupe_workouts(result)


def _workout_dedupe_key(workout: dict) -> str:
    """Return a stable key to de-duplicate workouts from multiple sources."""
    strava_id = workout.get("strava_id")
    if strava_id:
        return f"strava:{strava_id}"
    return "|".join([
        str(workout.get("date", "")),
        str(workout.get("start_time", "")),
        str(workout.get("name", "")),
        str(workout.get("type", "")),
    ])


def _dedupe_workouts(records: list[dict]) -> list[dict]:
    """De-duplicate workouts while keeping original order."""
    seen = set()
    out = []
    for w in records:
        key = _workout_dedupe_key(w)
        if key in seen:
            continue
        seen.add(key)
        out.append(w)
    return out


def _fetch_workouts_limited(before_date: str | None, limit: int) -> list[dict]:
    """Fetch only the most recent workouts (limited) using Flux pivot + limit."""
    if not query_api:
        return []

    fields = [
        "duration", "duration_minutes", "avg_hr", "max_hr", "calories",
        "suffer_score", "distance", "elevation_gain", "start_time", "time",
        "name", "strava_id", "feeling", "intensity"
    ]
    field_filter = " or ".join([f'r._field == "{f}"' for f in fields])
    date_filter = f'|> filter(fn: (r) => r.date <= "{before_date}")' if before_date else ""
    # Keep range minimal for speed; date tag filter enforces cutoff
    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -{WORKOUT_LOOKBACK_DAYS}d)
      |> filter(fn: (r) => r._measurement == "workout_cache" or r._measurement == "workouts")
      |> filter(fn: (r) => {field_filter})
      {date_filter}
      |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
      |> keep(columns: ["_time", "date", "type", {", ".join([f'"{f}"' for f in fields])}])
      |> sort(columns: ["date", "start_time"], desc: true)
      |> limit(n: {limit})
    '''
    result = query_api.query_data_frame(query)
    if isinstance(result, list):
        if len(result) == 0:
            return []
        result = pd.concat(result, ignore_index=True)
    if result.empty:
        return []

    records = result.to_dict(orient="records")
    # Clean NaN values
    cleaned = []
    for row in records:
        cleaned.append({k: (None if pd.isna(v) else v) for k, v in row.items()})
    deduped = _dedupe_workouts(cleaned)
    return deduped[:limit]


def _load_workout_index() -> None:
    """Background load of all workouts into memory for fast filtering."""
    if not query_api:
        with _workout_index_lock:
            _workout_index["loading"] = False
        return

    from collections import defaultdict

    fields = [
        "duration", "duration_minutes", "avg_hr", "max_hr", "calories",
        "suffer_score", "distance", "elevation_gain", "start_time", "time",
        "name", "strava_id", "feeling", "intensity"
    ]
    field_filter = " or ".join([f'r._field == "{f}"' for f in fields])
    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -{WORKOUT_INDEX_RANGE_DAYS}d)
      |> filter(fn: (r) => r._measurement == "workout_cache" or r._measurement == "workouts")
      |> filter(fn: (r) => {field_filter})
    '''

    workouts = defaultdict(dict)
    try:
        tables = query_api.query_stream(query)
        for record in tables:
            key = str(record.get_time())
            field = record.get_field()
            value = record.get_value()
            workouts[key][field] = value
            workouts[key]["date"] = record.values.get("date", "")
            workouts[key]["type"] = record.values.get("type", "")
    except Exception as e:
        logger.error(f"Error loading workout index: {e}")
        with _workout_index_lock:
            _workout_index["loading"] = False
        return

    # Sort by date and start_time descending once
    data = sorted(
        workouts.values(),
        key=lambda x: (x.get("date", ""), x.get("start_time", "")),
        reverse=True,
    )
    data = _dedupe_workouts(data)
    with _workout_index_lock:
        _workout_index["data"] = data
        _workout_index["loaded_at"] = datetime.now()
        _workout_index["loading"] = False
        _workout_index["loading_started_at"] = None


def _ensure_workout_index_loaded():
    """Return cached workout index or trigger background load."""
    now = datetime.now()
    with _workout_index_lock:
        data = _workout_index.get("data")
        loaded_at = _workout_index.get("loaded_at")
        loading = _workout_index.get("loading", False)
        loading_started_at = _workout_index.get("loading_started_at")

        if data and loaded_at and (now - loaded_at).total_seconds() < WORKOUT_INDEX_TTL_SECONDS:
            return data

        # If data exists but is stale, return it and refresh in background
        if not loading:
            _workout_index["loading"] = True
            _workout_index["loading_started_at"] = now
            threading.Thread(target=_load_workout_index, daemon=True).start()

        return data

@app.route('/api/workouts', methods=['GET', 'POST'])
@login_required
def workouts():
    """Get or log workouts"""
    if request.method == 'GET':
        filter_date = request.args.get('date')
        before_date = request.args.get('before_date')
        limit = request.args.get('limit', type=int)
        
        if not query_api:
            logger.info("Using mock workouts (no InfluxDB)")
            return jsonify({"error": "No workouts from InfluxDB"}), 404
        
        try:
            # Fast path: use in-memory index for date-filtered requests
            if before_date or filter_date:
                index = _ensure_workout_index_loaded()
                if index:
                    records = []
                    for w in index:
                        d = w.get('date', '')
                        if not d:
                            continue
                        if filter_date and d != filter_date:
                            continue
                        if before_date and d > before_date:
                            continue
                        records.append(w)
                        if limit and limit > 0 and len(records) >= limit:
                            break
                    return jsonify(records)

                # If index not ready, use limited query for dashboard requests
                if before_date and limit and limit <= 10:
                    records = _fetch_workouts_limited(before_date, limit)
                    return jsonify(records)

                if index is None:
                    # If index is still loading for too long, fallback to direct query
                    with _workout_index_lock:
                        loading_started_at = _workout_index.get("loading_started_at")
                    if loading_started_at and (datetime.now() - loading_started_at).total_seconds() > 15:
                        logger.warning("Workout index slow to load; falling back to direct query")
                        records = _fetch_workouts_from_influx(before_date=before_date)
                        if filter_date:
                            records = [w for w in records if w.get('date') == filter_date]
                        elif before_date:
                            records = [w for w in records if w.get('date', '') <= before_date]
                        if limit and limit > 0:
                            records = records[:limit]
                        return jsonify(records)

                    resp = jsonify({"loading": True})
                    resp.status_code = 503
                    resp.headers["Retry-After"] = "3"
                    return resp
                records = []
                for w in index:
                    d = w.get('date', '')
                    if not d:
                        continue
                    if filter_date and d != filter_date:
                        continue
                    if before_date and d > before_date:
                        continue
                    records.append(w)
                    if limit and limit > 0 and len(records) >= limit:
                        break
                return jsonify(records)

            # Check cache first (only if no filters)
            now = datetime.now()
            if not filter_date and not before_date and _workout_cache["data"] and _workout_cache["expires"] and now < _workout_cache["expires"]:
                records = _workout_cache["data"]
            else:
                logger.info(f"Fetching workouts from InfluxDB (date: {filter_date}, before: {before_date})")
                records = _fetch_workouts_from_influx(before_date=before_date)
            
            if not records:
                return jsonify({"error": "No workouts from InfluxDB"}), 404
            
            # Update cache (only if no filters)
            if not filter_date and not before_date:
                _workout_cache["data"] = records
                _workout_cache["expires"] = now + timedelta(seconds=CACHE_TTL_SECONDS)
            
            # Filter: before_date = workouts on or before that date (descending), limit
            if before_date:
                records = [w for w in records if w.get('date', '') <= before_date]
                if limit and limit > 0:
                    records = records[:limit]
            elif filter_date:
                records = [w for w in records if w.get('date') == filter_date]
            
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
            # Query latest manual values per metric (respect deleted markers)
            target_dt = datetime.strptime(date, "%Y-%m-%d")
            start_dt = target_dt - timedelta(days=7)
            stop_dt = target_dt + timedelta(days=1)
            query = f'''
            from(bucket: "{INFLUXDB_BUCKET}")
              |> range(start: {start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {stop_dt.strftime("%Y-%m-%dT00:00:00Z")})
              |> filter(fn: (r) => r._measurement == "manual_values")
              |> filter(fn: (r) => r.date == "{date}")
              |> group(columns: ["_field"])
              |> sort(columns: ["_time"], desc: true)
              |> limit(n: 1)
            '''
            result = query_api.query(query)
            
            metrics = ['sleep', 'hrv', 'resting_hr', 'steps', 'weight', 'calories', 'ctl', 'atl', 'tsb']
            values = {m: None for m in metrics}
            
            for table in result:
                for record in table.records:
                    metric = record.get_field()
                    if metric not in values:
                        continue
                    is_deleted = str(record.values.get('deleted', '')).lower() == 'true'
                    if is_deleted:
                        values[metric] = None
                    else:
                        val = record.get_value()
                        values[metric] = None if val is None else float(val)
            
            logger.info(f"Manual values for {date}: {values}")
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
            # Parse the target date and set timestamp to noon of that day
            target_dt = datetime.strptime(date, "%Y-%m-%d").replace(hour=12, minute=0, second=0)
            point = Point("manual_values")\
                .tag("date", date)\
                .field(metric, float(value))\
                .time(target_dt)
            
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
            # Parse the target date and set timestamp to noon of that day
            target_dt = datetime.strptime(date, "%Y-%m-%d").replace(hour=12, minute=0, second=0)
            point = Point("manual_values")\
                .tag("date", date)\
                .tag("deleted", "true")\
                .field(metric, 0.0)\
                .time(target_dt)
            
            write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
            if metric == "weight":
                _weight_cache.pop(date, None)
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
    """Get calories burned for today (default) or specified date.
    
    Sources (in priority order):
    1. daily_health.total_calories (from Apple Health import - basal + active)
    2. workout_cache/workouts calories (from Strava sync)
    """
    date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
    
    if not query_api:
        return jsonify({"calories": 0, "date": date})
    
    try:
        # First try to get total calories from daily_health (Apple Health import)
        target_dt = datetime.strptime(date, "%Y-%m-%d")
        start_dt = target_dt - timedelta(days=1)
        stop_dt = target_dt + timedelta(days=1)
        
        query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: {start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {stop_dt.strftime("%Y-%m-%dT23:59:59Z")})
          |> filter(fn: (r) => r._measurement == "daily_health")
          |> filter(fn: (r) => r.date == "{date}")
          |> filter(fn: (r) => r._field == "total_calories")
          |> last()
        '''
        result = query_api.query(query)
        
        for table in result:
            for record in table.records:
                val = record.get_value()
                if val:
                    return jsonify({"calories": int(val), "date": date, "source": "apple_health"})
        
        # Fall back to workout calories from Strava
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
        
        return jsonify({"calories": int(total_calories), "date": date, "source": "strava"})
    except Exception as e:
        logger.error(f"Error fetching calories: {e}")
        return jsonify({"calories": 0, "date": date, "error": str(e)})


@app.route('/api/weight', methods=['GET', 'POST'])
@login_required
def weight():
    """Get or set weight"""
    if request.method == 'GET':
        date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
        
        if not query_api:
            return jsonify({"weight": None})
        
        # Cache GET by date (30s TTL)
        now = datetime.now()
        if date in _weight_cache:
            cached, expires = _weight_cache[date]
            if now < expires:
                return jsonify(cached)
            del _weight_cache[date]

        try:
            target_dt = datetime.strptime(date, "%Y-%m-%d")
            start_dt = target_dt - timedelta(days=7)
            weight_start_dt = target_dt - timedelta(days=WEIGHT_LOOKBACK_DAYS)
            stop_dt = target_dt + timedelta(days=1)
            # 1. Manual override for this specific date (get latest, then exclude if deleted)
            query = f'''
            from(bucket: "{INFLUXDB_BUCKET}")
              |> range(start: {start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {stop_dt.strftime("%Y-%m-%dT00:00:00Z")})
              |> filter(fn: (r) => r._measurement == "manual_values")
              |> filter(fn: (r) => r._field == "weight")
              |> filter(fn: (r) => r.date == "{date}")
              |> sort(columns: ["_time"], desc: true)
              |> limit(n: 1)
              |> filter(fn: (r) => r.deleted != "true")
            '''
            result = query_api.query(query)
            for table in result:
                for record in table.records:
                    val = record.get_value()
                    if val:
                        resp = {"weight": float(val), "source": "manual", "date": date}
                        _weight_cache[date] = (resp, now + timedelta(seconds=CACHE_TTL_SECONDS))
                        return jsonify(resp)

            # 2. daily_health for this specific date (Fitbit, Apple Health, Suunto)
            query = f'''
            from(bucket: "{INFLUXDB_BUCKET}")
              |> range(start: {start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {stop_dt.strftime("%Y-%m-%dT00:00:00Z")})
              |> filter(fn: (r) => r._measurement == "daily_health")
              |> filter(fn: (r) => r._field == "weight")
              |> filter(fn: (r) => r.date == "{date}")
              |> last()
            '''
            result = query_api.query(query)
            for table in result:
                for record in table.records:
                    val = record.get_value()
                    if val:
                        resp = {"weight": float(val), "source": "auto", "date": date}
                        _weight_cache[date] = (resp, now + timedelta(seconds=CACHE_TTL_SECONDS))
                        return jsonify(resp)

            # 3. Most recent manual weight (any date) when no data for this date (exclude if latest is deleted)
            query = f'''
            from(bucket: "{INFLUXDB_BUCKET}")
              |> range(start: -{WEIGHT_LOOKBACK_DAYS}d)
              |> filter(fn: (r) => r._measurement == "manual_values")
              |> filter(fn: (r) => r._field == "weight")
              |> sort(columns: ["_time"], desc: true)
              |> limit(n: 1)
              |> filter(fn: (r) => r.deleted != "true")
            '''
            result = query_api.query(query)
            for table in result:
                for record in table.records:
                    val = record.get_value()
                    if val:
                        resp = {"weight": float(val), "source": "manual", "date": date}
                        _weight_cache[date] = (resp, now + timedelta(seconds=CACHE_TTL_SECONDS))
                        return jsonify(resp)

            # 4. Most recent daily_health weight when no data for this date
            query = f'''
            from(bucket: "{INFLUXDB_BUCKET}")
              |> range(start: -{WEIGHT_LOOKBACK_DAYS}d)
              |> filter(fn: (r) => r._measurement == "daily_health")
              |> filter(fn: (r) => r._field == "weight")
              |> last()
            '''
            result = query_api.query(query)
            for table in result:
                for record in table.records:
                    val = record.get_value()
                    if val:
                        resp = {"weight": float(val), "source": "auto", "date": date}
                        _weight_cache[date] = (resp, now + timedelta(seconds=CACHE_TTL_SECONDS))
                        return jsonify(resp)
            
            resp = {"weight": None, "date": date}
            _weight_cache[date] = (resp, now + timedelta(seconds=CACHE_TTL_SECONDS))
            return jsonify(resp)
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
            _weight_cache.pop(date, None)  # Invalidate cache
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


def _fetch_daily_loads_from_influx(query_days=120):
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


def _dash_fetch_health_today(target_date: str) -> dict:
    """Fetch health metrics for a date. Returns dict for dashboard. Thread-safe."""
    if not query_api:
        return {"error": "InfluxDB not configured"}
    try:
        target_dt = datetime.strptime(target_date, "%Y-%m-%d")
        start_dt = target_dt - timedelta(days=7)
        stop_dt = target_dt + timedelta(days=1)
        query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: {start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {stop_dt.strftime("%Y-%m-%dT00:00:00Z")})
          |> filter(fn: (r) => r._measurement == "daily_health")
          |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
        '''
        result = query_api.query_data_frame(query)
        if isinstance(result, list):
            if len(result) == 0:
                return {"error": "No data from InfluxDB"}
            result = pd.concat(result, ignore_index=True)
        if result.empty or "date" not in result.columns:
            return {"error": "No data from InfluxDB"}
        df = result.copy()
        numeric_cols = [c for c in ["sleep_duration_hours", "hrv_avg", "resting_hr", "steps", "recovery_score", "training_load", "weight"] if c in df.columns]
        for c in numeric_cols:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        if numeric_cols:
            df = df.groupby("date", as_index=False)[numeric_cols].mean()
        else:
            df = df[["date"]].drop_duplicates()
        df = df.sort_values("date")
        target_row = df[df['date'] == target_date]
        row = target_row.iloc[0] if not target_row.empty else df.iloc[-1]
        def clean(v):
            return None if pd.isna(v) else float(v)
        def get(col):
            return clean(row[col]) if col in row.index else None
        steps_val = get("steps")
        return {
            "date": row.get("date", target_date),
            "sleep_hours": get("sleep_duration_hours"),
            "hrv": get("hrv_avg"),
            "resting_hr": get("resting_hr"),
            "steps": None if steps_val is None else int(float(steps_val)),
            "recovery_score": get("recovery_score"),
            "training_load": get("training_load")
        }
    except Exception as e:
        logger.error(f"Dashboard health_today error: {e}")
        return {"error": str(e)}


def _dash_fetch_health_history(days: int, end_date: str) -> dict:
    """Fetch health history. Returns dict for dashboard. Thread-safe."""
    if not query_api:
        return {"error": "No data from InfluxDB"}
    try:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        start_dt = end_dt - timedelta(days=days + 7)
        query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: {start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {(end_dt + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")})
          |> filter(fn: (r) => r._measurement == "daily_health")
          |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
        '''
        result = query_api.query_data_frame(query)
        dates_list = [(end_dt - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days-1, -1, -1)]
        has_daily_health = False
        if isinstance(result, list):
            if len(result) > 0:
                result = pd.concat(result, ignore_index=True)
                has_daily_health = not result.empty
        elif not result.empty:
            has_daily_health = True
        if has_daily_health and "date" in result.columns:
            df = result.copy()
            numeric_cols = [c for c in ["hrv_avg", "resting_hr", "sleep_duration_hours", "recovery_score", "steps", "weight"] if c in df.columns]
            if numeric_cols:
                for c in numeric_cols:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
                df = df.groupby("date", as_index=False)[numeric_cols].mean()
            else:
                df = df[["date"]].drop_duplicates()
            df = df.sort_values("date").tail(days)
            dates_list = df["date"].tolist()
        else:
            df = pd.DataFrame({"date": dates_list})
        def clean_series(s, d=2):
            return [None if pd.isna(v) else round(float(v), d) for v in s.tolist()]
        manual_data = {f: {} for f in ['weight', 'hrv', 'sleep', 'resting_hr', 'steps']}
        try:
            manual_query = f'''
            from(bucket: "{INFLUXDB_BUCKET}")
              |> range(start: -{days}d)
              |> filter(fn: (r) => r._measurement == "manual_values")
              |> filter(fn: (r) => r._field == "weight" or r._field == "hrv" or r._field == "sleep" or r._field == "resting_hr" or r._field == "steps")
              |> filter(fn: (r) => r.deleted != "true")
            '''
            for table in query_api.query(manual_query):
                for rec in table.records:
                    d = rec.values.get('date', '')
                    f = rec.get_field()
                    if d and f in manual_data:
                        manual_data[f][d] = float(rec.get_value())
        except Exception:
            pass
        def merge(auto, manual_dict, dates):
            return [manual_dict.get(d) if manual_dict.get(d) is not None else (auto[i] if i < len(auto) else None) for i, d in enumerate(dates)]
        dates_list = df["date"].tolist()
        hrv_a = clean_series(df["hrv_avg"], 2) if "hrv_avg" in df else [None] * len(dates_list)
        rhr_a = clean_series(df["resting_hr"], 2) if "resting_hr" in df else [None] * len(dates_list)
        sleep_a = clean_series(df["sleep_duration_hours"], 2) if "sleep_duration_hours" in df else [None] * len(dates_list)
        steps_a = clean_series(df["steps"], 0) if "steps" in df else [None] * len(dates_list)
        weight_a = clean_series(df["weight"], 2) if "weight" in df else [None] * len(dates_list)
        return {
            "dates": dates_list,
            "hrv": merge(hrv_a, manual_data['hrv'], dates_list),
            "resting_hr": merge(rhr_a, manual_data['resting_hr'], dates_list),
            "sleep": merge(sleep_a, manual_data['sleep'], dates_list),
            "recovery": clean_series(df["recovery_score"], 1) if "recovery_score" in df else [],
            "steps": merge(steps_a, manual_data['steps'], dates_list),
            "weight": merge(weight_a, manual_data['weight'], dates_list)
        }
    except Exception as e:
        logger.error(f"Dashboard health_history error: {e}")
        return {"dates": [], "hrv": [], "resting_hr": [], "sleep": [], "recovery": [], "steps": [], "weight": []}


def _dash_fetch_recommendations(date: str) -> dict:
    """Fetch recommendations. Thread-safe."""
    try:
        health = get_mock_health_today()
        health["date"] = date
        return planner.get_recommendation(health)
    except Exception as e:
        logger.error(f"Dashboard recommendations error: {e}")
        return {"error": str(e)}


def _dash_fetch_pmc(days: int, end_date_str: str) -> dict:
    """Fetch PMC data. Thread-safe."""
    if not query_api:
        return {"error": "No training load data from InfluxDB"}
    try:
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
        query_days = max(days + 42, PMC_MIN_LOOKBACK_DAYS)
        daily_loads = _fetch_daily_loads_from_influx(query_days)
        if not daily_loads:
            return {"error": "No training load data from InfluxDB"}
        loads_map = {d["date"]: float(d.get("load", 0.0)) for d in daily_loads}
        start_date = end_date - timedelta(days=query_days - 1)
        full_series = []
        cur = start_date
        while cur <= end_date:
            full_series.append({"date": cur.isoformat(), "load": loads_map.get(cur.isoformat(), 0.0)})
            cur += timedelta(days=1)
        pmc_series = calculate_pmc_series(full_series)
        pmc_recent = pmc_series[-days:]
        latest = pmc_recent[-1] if pmc_recent else {"ctl": 0, "atl": 0, "tsb": 0}
        return {
            "ctl": latest["ctl"], "atl": latest["atl"], "tsb": latest["tsb"],
            "status": get_status_description(latest["tsb"]),
            "description": get_status_description(latest["tsb"]),
            "days_tracked": len(full_series),
            "chart": {
                "dates": [d["date"] for d in pmc_recent],
                "ctl": [d["ctl"] for d in pmc_recent],
                "atl": [d["atl"] for d in pmc_recent],
                "tsb": [d["tsb"] for d in pmc_recent],
            }
        }
    except Exception as e:
        logger.error(f"Dashboard PMC error: {e}")
        return {"error": str(e)}


def _dash_fetch_workouts(before_date: str, limit: int = 10) -> list | dict:
    """Fetch the 10 most recent workouts on or before the view date. Thread-safe."""
    if not query_api:
        return []
    try:
        records = _fetch_workouts_from_influx(before_date=before_date)
        if not records:
            return []
        records = [w for w in records if w.get('date', '') <= before_date]
        if limit and limit > 0:
            records = records[:limit]
        return records
    except Exception as e:
        logger.error(f"Dashboard workouts error: {e}")
        return []


def _dash_fetch_calories(date: str) -> dict:
    """Fetch calories. Thread-safe."""
    if not query_api:
        return {"calories": 0, "date": date}
    try:
        target_dt = datetime.strptime(date, "%Y-%m-%d")
        start_dt = target_dt - timedelta(days=1)
        stop_dt = target_dt + timedelta(days=1)
        query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: {start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {stop_dt.strftime("%Y-%m-%dT23:59:59Z")})
          |> filter(fn: (r) => r._measurement == "daily_health")
          |> filter(fn: (r) => r.date == "{date}")
          |> filter(fn: (r) => r._field == "total_calories")
          |> last()
        '''
        for table in query_api.query(query):
            for rec in table.records:
                val = rec.get_value()
                if val:
                    return {"calories": int(val), "date": date, "source": "apple_health"}
        query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: -30d)
          |> filter(fn: (r) => r._measurement == "workout_cache" or r._measurement == "workouts")
          |> filter(fn: (r) => r.date == "{date}")
          |> filter(fn: (r) => r._field == "calories")
          |> sum()
        '''
        total = 0
        for table in query_api.query(query):
            for rec in table.records:
                v = rec.get_value()
                if v:
                    total += float(v)
        return {"calories": int(total), "date": date, "source": "strava"}
    except Exception as e:
        logger.error(f"Dashboard calories error: {e}")
        return {"calories": 0, "date": date}


def _dash_fetch_weight(date: str) -> dict:
    """Fetch weight. Thread-safe."""
    if not query_api:
        return {"weight": None, "date": date}
    try:
        target_dt = datetime.strptime(date, "%Y-%m-%d")
        start_dt = target_dt - timedelta(days=7)
        weight_start_dt = target_dt - timedelta(days=WEIGHT_LOOKBACK_DAYS)
        stop_dt = target_dt + timedelta(days=1)
        # 1. Manual for this date
        query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: {weight_start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {stop_dt.strftime("%Y-%m-%dT00:00:00Z")})
          |> filter(fn: (r) => r._measurement == "manual_values")
          |> filter(fn: (r) => r._field == "weight")
          |> filter(fn: (r) => r.date == "{date}")
          |> sort(columns: ["_time"], desc: true)
          |> limit(n: 1)
          |> filter(fn: (r) => r.deleted != "true")
        '''
        for table in query_api.query(query):
            for rec in table.records:
                v = rec.get_value()
                if v:
                    return {"weight": float(v), "source": "manual", "date": date}
        # 2. daily_health for this date
        query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: {weight_start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {stop_dt.strftime("%Y-%m-%dT00:00:00Z")})
          |> filter(fn: (r) => r._measurement == "daily_health")
          |> filter(fn: (r) => r._field == "weight")
          |> filter(fn: (r) => r.date == "{date}")
          |> last()
        '''
        for table in query_api.query(query):
            for rec in table.records:
                v = rec.get_value()
                if v:
                    return {"weight": float(v), "source": "auto", "date": date}
        # 3. Most recent daily_health on/before this date (within lookback window)
        query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: {weight_start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {stop_dt.strftime("%Y-%m-%dT00:00:00Z")})
          |> filter(fn: (r) => r._measurement == "daily_health")
          |> filter(fn: (r) => r._field == "weight")
          |> filter(fn: (r) => r.date <= "{date}")
          |> group()
          |> sort(columns: ["_time"], desc: true)
          |> limit(n: 1)
        '''
        for table in query_api.query(query):
            for rec in table.records:
                v = rec.get_value()
                if v:
                    return {"weight": float(v), "source": "auto", "date": rec.values.get("date", date)}

        # 4. Most recent manual on/before this date (within lookback window)
        query = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: {weight_start_dt.strftime("%Y-%m-%dT00:00:00Z")}, stop: {stop_dt.strftime("%Y-%m-%dT00:00:00Z")})
          |> filter(fn: (r) => r._measurement == "manual_values")
          |> filter(fn: (r) => r._field == "weight")
          |> filter(fn: (r) => r.date <= "{date}")
          |> group()
          |> sort(columns: ["_time"], desc: true)
          |> limit(n: 1)
          |> filter(fn: (r) => r.deleted != "true")
        '''
        for table in query_api.query(query):
            for rec in table.records:
                v = rec.get_value()
                if v:
                    return {"weight": float(v), "source": "manual", "date": rec.values.get("date", date)}
        return {"weight": None, "date": date}
    except Exception as e:
        logger.error(f"Dashboard weight error: {e}")
        return {"weight": None, "date": date}


@app.route('/api/dashboard/quick')
@login_required
def api_dashboard_quick():
    """Phase 1: fast data - health, recommendation, calories, weight. Renders first."""
    date = request.args.get('date', datetime.now().strftime("%Y-%m-%d"))
    now = datetime.now()
    cache_key = f"quick:{date}"
    if cache_key in _dashboard_cache:
        cached, expires = _dashboard_cache[cache_key]
        if now < expires:
            return jsonify(cached)
        del _dashboard_cache[cache_key]
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {
            ex.submit(_dash_fetch_health_today, date): "health",
            ex.submit(_dash_fetch_recommendations, date): "recommendation",
            ex.submit(_dash_fetch_calories, date): "calories",
            ex.submit(_dash_fetch_weight, date): "weight",
        }
        out = {}
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                out[key] = fut.result()
            except Exception as e:
                logger.error(f"Dashboard quick {key} error: {e}")
                out[key] = {"error": str(e)}
    _dashboard_cache[cache_key] = (out, now + timedelta(seconds=CACHE_TTL_SECONDS))
    return jsonify(out)


@app.route('/api/dashboard/charts')
@login_required
def api_dashboard_charts():
    """Phase 2: charts - health history, PMC. Loads after quick."""
    date = request.args.get('date', datetime.now().strftime("%Y-%m-%d"))
    days = request.args.get('days', 10, type=int)
    now = datetime.now()
    cache_key = f"charts:{date}:{days}"
    if cache_key in _dashboard_cache:
        cached, expires = _dashboard_cache[cache_key]
        if now < expires:
            return jsonify(cached)
        del _dashboard_cache[cache_key]
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = {
            ex.submit(_dash_fetch_health_history, days, date): "history",
            ex.submit(_dash_fetch_pmc, days, date): "pmc",
        }
        out = {}
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                out[key] = fut.result()
            except Exception as e:
                logger.error(f"Dashboard charts {key} error: {e}")
                out[key] = {"error": str(e)}
    _dashboard_cache[cache_key] = (out, now + timedelta(seconds=CACHE_TTL_SECONDS))
    return jsonify(out)


@app.route('/api/dashboard')
@login_required
def api_dashboard():
    """Combined endpoint: all dashboard data in one response. Queries run in parallel."""
    date = request.args.get('date', datetime.now().strftime("%Y-%m-%d"))
    days = request.args.get('days', 10, type=int)  # 10-day window for fast loads
    now = datetime.now()
    cache_key = f"{date}:{days}"
    if cache_key in _dashboard_cache:
        cached, expires = _dashboard_cache[cache_key]
        if now < expires:
            return jsonify(cached)
        del _dashboard_cache[cache_key]
    out = {}
    with ThreadPoolExecutor(max_workers=7) as ex:
        futures = {
            ex.submit(_dash_fetch_health_today, date): "health",
            ex.submit(_dash_fetch_health_history, days, date): "history",
            ex.submit(_dash_fetch_recommendations, date): "recommendation",
            ex.submit(_dash_fetch_pmc, days, date): "pmc",
            ex.submit(_dash_fetch_workouts, date, 10): "workouts",
            ex.submit(_dash_fetch_calories, date): "calories",
            ex.submit(_dash_fetch_weight, date): "weight",
        }
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                out[key] = fut.result()
            except Exception as e:
                logger.error(f"Dashboard {key} error: {e}")
                out[key] = {"error": str(e)} if key != "workouts" else []
    _dashboard_cache[cache_key] = (out, now + timedelta(seconds=CACHE_TTL_SECONDS))
    return jsonify(out)


@app.route('/api/pmc')
@login_required
def pmc():
    """
    Get Performance Management Chart data (CTL, ATL, TSB)
    This calculates fitness, strain, and form from training load
    """
    days = request.args.get('days', 90, type=int)
    end_date_str = request.args.get('end_date', datetime.now().strftime("%Y-%m-%d"))
    query_days = max(days + 42, PMC_MIN_LOOKBACK_DAYS)  # Smaller window for speed
    
    # Parse end_date
    try:
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    except ValueError:
        end_date = datetime.now().date()
    
    is_today = end_date == datetime.now().date()
    
    # Check cache first (only use cache if querying for today)
    now = datetime.now()
    if is_today and _pmc_cache["data"] and _pmc_cache["expires"] and now < _pmc_cache["expires"]:
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
    start_date = end_date - timedelta(days=query_days - 1)

    full_series = []
    cur = start_date
    while cur <= end_date:
        ds = cur.isoformat()
        full_series.append({"date": ds, "load": loads_map.get(ds, 0.0)})
        cur += timedelta(days=1)

    pmc_series = calculate_pmc_series(full_series)
    
    # Update cache only if querying for today
    if is_today:
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
